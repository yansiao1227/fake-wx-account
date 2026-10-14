"""PDF 读取子进程，仅依赖本地库；每页完成后保存有界、可恢复的结果。"""

import json
import math
from pathlib import Path
import sys


def _has_text(text):
    return isinstance(text, str) and any(character.isalnum() for character in text)


def _local_ocr_engine():
    """显式指定已经存在的模型，避免 RapidOCR 在处理消息时联网下载模型。"""
    import rapidocr
    from rapidocr import RapidOCR
    from rapidocr.inference_engine.base import FileInfo, InferSession
    from rapidocr.utils.parse_parameters import ParseParams

    root = Path(rapidocr.__file__).resolve().parent
    config = ParseParams.load(root / "config.yaml")
    params = {"Global.log_level": "error", "EngineConfig.onnxruntime.intra_op_num_threads": 2,
              "EngineConfig.onnxruntime.inter_op_num_threads": 1}
    for name in ("Det", "Cls", "Rec"):
        item = config[name]
        info = InferSession.get_model_url(FileInfo(
            engine_type=item.engine_type, ocr_version=item.ocr_version,
            task_type=item.task_type, lang_type=item.lang_type, model_type=item.model_type,
        ))
        model = root / "models" / Path(info["model_dir"]).name
        if not model.is_file():
            raise FileNotFoundError("本地 OCR 模型未就绪")
        params[f"{name}.model_path"] = str(model)
    return RapidOCR(params=params)


def _ocr_text(result):
    texts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if texts is None or scores is None:
        return ""
    output = []
    for text, score in zip(texts, scores):
        try:
            confidence = float(score)
        except (TypeError, ValueError):
            continue
        if math.isfinite(confidence) and confidence >= 0.5 and _has_text(text):
            output.append(text.replace("\x00", "").strip())
    return "\n".join(output)


def _render(page, pymupdf, limits, remaining_pixels):
    import numpy as np

    width, height = float(page.rect.width), float(page.rect.height)
    if not all(math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError("invalid page size")
    pixels = min(limits["max_page_pixels"], remaining_pixels)
    if pixels < 1:
        raise ValueError("pixel budget exhausted")
    scale = min(limits["dpi"] / 72, math.sqrt(pixels / (width * height)),
                limits["max_image_edge"] / max(width, height)) * 0.99
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False)
    count = pixmap.width * pixmap.height
    if count > pixels or max(pixmap.width, pixmap.height) > limits["max_image_edge"]:
        raise ValueError("rendered image exceeds pixel budget")
    image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
    # MuPDF 输出 RGB，RapidOCR/OpenCV 的 numpy 输入为 BGR。
    return image[:, :, ::-1].copy(), count


def read_pdf(request, save=lambda payload: None, open_document=None, ocr_factory=None):
    import pymupdf

    limits = request["limits"]
    result = {
        "type": "pdf_text", "reading_status": "unavailable", "content": "",
        "source": "pdf_native_text_and_rapidocr", "total_pages": 0, "processed_pages": 0,
        "read_pages": [], "ocr_pages": [], "native_text_pages": [], "unreadable_pages": [],
        "omitted_pages": 0, "truncated": False, "limits_reached": [], "limits": limits,
        "message": "没有读到 PDF 正文。",
    }
    save(result)

    def checkpoint():
        result["omitted_pages"] = result["total_pages"] - len(result["read_pages"])
        result["truncated"] = bool(result["omitted_pages"] or result["limits_reached"] or result["unreadable_pages"])
        result["reading_status"] = ("partial" if result["truncated"] else "success") if result["content"] else "unavailable"
        result["message"] = {"success": "PDF 正文已读取。", "partial": "仅取得部分 PDF 正文，请按已读取页分析。",
                             "unavailable": "没有读到 PDF 正文，请提供清晰的文本或可读页面；本次读取已结束。"}[result["reading_status"]]
        save(result)

    opener = open_document or pymupdf.open
    try:
        document = opener(request["path"])
    except Exception:
        result["message"] = "PDF 损坏或格式不可读取；本次读取已结束。"
        save(result)
        return result
    try:
        if not document.is_pdf or document.needs_pass:
            result["message"] = "PDF 需要密码或格式不可读取；本次读取已结束。"
            save(result)
            return result
        result["total_pages"] = len(document)
        start = request["start_page"] - 1
        if start >= len(document):
            result["message"] = "起始页超出 PDF 页面范围。"
            result["omitted_pages"] = len(document)
            save(result)
            return result
        end = min(len(document), start + limits["max_pages"])
        if end < len(document):
            result["limits_reached"].append("max_pages")
        engine = None
        ocr_unavailable = False
        total_pixels = 0
        for index in range(start, end):
            number = index + 1
            try:
                page = document[index]
                text = page.get_text("text", sort=True).replace("\x00", "").strip()
                native = _has_text(text)
                # 只有页码/短标题的扫描页仍需 OCR，不能据此宣称读取了整页正文。
                needs_ocr = not native or (sum(character.isalnum() for character in text) < 20 and bool(page.get_images()))
                if needs_ocr:
                    if ocr_unavailable:
                        raise RuntimeError("ocr unavailable")
                    if total_pixels >= limits["max_total_pixels"]:
                        result["limits_reached"].append("max_total_pixels")
                        break
                    result["ocr_pages"].append(number)
                    image, pixel_count = _render(page, pymupdf, limits, limits["max_total_pixels"] - total_pixels)
                    total_pixels += pixel_count
                    if engine is None:
                        try:
                            engine = (ocr_factory or _local_ocr_engine)()
                        except Exception:
                            ocr_unavailable = True
                            raise
                    text = _ocr_text(engine(image))
                    if not _has_text(text):
                        result["unreadable_pages"].append({"page": number, "reason": "empty_or_unrecognized"})
                        result["processed_pages"] += 1
                        checkpoint()
                        continue
                    method = "OCR"
                else:
                    result["native_text_pages"].append(number)
                    method = "原生文本"
                separator = "\n\n" if result["content"] else ""
                prefix = f"{separator}[第 {number} 页，{method}]\n"
                remaining = limits["max_chars"] - len(result["content"])
                if remaining <= len(prefix):
                    result["limits_reached"].append("max_chars")
                    break
                piece = text[:remaining - len(prefix)]
                if not _has_text(piece):
                    result["limits_reached"].append("max_chars")
                    break
                result["content"] += prefix + piece
                result["read_pages"].append(number)
                result["processed_pages"] += 1
                if len(piece) < len(text):
                    result["limits_reached"].append("max_chars")
                    checkpoint()
                    break
            except Exception:
                result["processed_pages"] += 1
                result["unreadable_pages"].append({"page": number, "reason": "ocr_unavailable" if ocr_unavailable else "page_read_failed"})
            checkpoint()
        checkpoint()
        return result
    finally:
        document.close()


def main():
    destination = Path(sys.argv[1])

    def save(payload):
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(destination)

    # 父进程明确按 UTF-8 写入；Windows 默认 GBK 会将中文附件路径解码成错名。
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    read_pdf(request, save=save)


if __name__ == "__main__":
    main()
