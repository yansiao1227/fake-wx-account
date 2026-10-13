"""扫描 PDF 正文、预算与硬超时回归；不读取微信真实文件。"""

import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from agent.tools.pdf_ocr.pdf_ocr import DEFAULT_LIMITS, PdfOcr
from agent.tools.pdf_ocr import worker


pymupdf = pytest.importorskip("pymupdf")


def make_pdf(path, pages):
    from PIL import Image

    document = pymupdf.open()
    for content in pages:
        page = document.new_page(width=400, height=300)
        if content == "scan":
            buffer = io.BytesIO()
            Image.new("RGB", (400, 300), "white").save(buffer, format="PNG")
            page.insert_image(page.rect, stream=buffer.getvalue())
        elif content:
            page.insert_text((40, 60), content, fontsize=14)
    document.save(path)
    document.close()
    return path


def test_worker_subprocess_preserves_chinese_attachment_path(tmp_path):
    path = make_pdf(tmp_path / "中文附件_扫描版.pdf", ["Unicode attachment path sample"])
    result = PdfOcr().execute({"path": str(path)})
    assert result.status == "success"
    assert result.result["reading_status"] == "success"
    assert "Unicode attachment path sample" in result.result["content"]
    assert result.result["read_pages"] == [1]


def request(path, **limits):
    return {"path": str(path), "start_page": 1, "limits": {**DEFAULT_LIMITS, **limits}}


def output(text="合成扫描文件正文", score=0.99):
    return SimpleNamespace(txts=[text], scores=[score])


def test_native_pdf_returns_real_text_without_ocr(tmp_path):
    path = make_pdf(tmp_path / "native.pdf", ["Synthetic native PDF sample"])

    def forbidden():
        raise AssertionError("原生文字页不得初始化 OCR")

    result = worker.read_pdf(request(path), ocr_factory=forbidden)
    assert result["reading_status"] == "success"
    assert "Synthetic native PDF sample" in result["content"]
    assert result["native_text_pages"] == [1]
    assert result["ocr_pages"] == []


def test_scan_pdf_renders_and_recognizes_official_rapidocr_output(tmp_path):
    path = make_pdf(tmp_path / "scan.pdf", ["scan"])
    images = []

    def recognize(image):
        images.append(image)
        return output()

    result = worker.read_pdf(request(path), ocr_factory=lambda: recognize)
    assert result["reading_status"] == "success"
    assert "合成扫描文件正文" in result["content"]
    assert result["ocr_pages"] == result["read_pages"] == [1]
    assert result["native_text_pages"] == []
    assert images[0].ndim == 3 and images[0].shape[2] == 3
    assert images[0].shape[0] * images[0].shape[1] <= DEFAULT_LIMITS["max_page_pixels"]


@pytest.mark.parametrize("text,score", [("", 1), ("   ", 1), ("!?!", 1), ("错误识别", 0.2), ("未知", float("nan"))])
def test_no_readable_ocr_text_never_reports_success(tmp_path, text, score):
    path = make_pdf(tmp_path / "empty.pdf", ["scan"])
    result = worker.read_pdf(request(path), ocr_factory=lambda: lambda image: output(text, score))
    assert result["reading_status"] == "unavailable"
    assert result["content"] == ""
    assert result["read_pages"] == []
    assert result["unreadable_pages"] == [{"page": 1, "reason": "empty_or_unrecognized"}]


def test_short_native_header_on_scan_still_requires_ocr(tmp_path):
    path = make_pdf(tmp_path / "header.pdf", ["scan"])
    with pymupdf.open(path) as document:
        document[0].insert_text((40, 60), "1", fontsize=14)
        document.saveIncr()
    result = worker.read_pdf(request(path), ocr_factory=lambda: lambda image: output())
    assert "合成扫描文件正文" in result["content"]
    assert result["ocr_pages"] == [1]
    assert result["native_text_pages"] == []


def test_mixed_pdf_reports_both_native_and_scan_pages(tmp_path):
    path = make_pdf(tmp_path / "mixed.pdf", ["Synthetic native PDF sample", "scan"])
    result = worker.read_pdf(request(path), ocr_factory=lambda: lambda image: output())
    assert result["reading_status"] == "success"
    assert result["read_pages"] == [1, 2]
    assert result["native_text_pages"] == [1]
    assert result["ocr_pages"] == [2]


def test_model_initialization_failure_is_attempted_once(tmp_path):
    path = make_pdf(tmp_path / "missing-models.pdf", ["scan", "scan", "Synthetic native PDF sample"])
    attempts = []

    def unavailable():
        attempts.append(1)
        raise RuntimeError("模型不可用")

    result = worker.read_pdf(request(path), ocr_factory=unavailable)
    assert attempts == [1]
    assert result["reading_status"] == "partial"
    assert result["read_pages"] == [3]
    assert [item["reason"] for item in result["unreadable_pages"]] == ["ocr_unavailable", "ocr_unavailable"]


def test_unreadable_page_preserves_prior_body_as_partial(tmp_path):
    path = make_pdf(tmp_path / "partial.pdf", ["Synthetic native PDF sample", "scan"])
    result = worker.read_pdf(request(path), ocr_factory=lambda: lambda image: output(""))
    assert result["reading_status"] == "partial"
    assert "Synthetic native PDF sample" in result["content"]
    assert result["omitted_pages"] == 1


def test_page_budget_and_start_page_are_explicit(tmp_path):
    path = make_pdf(tmp_path / "pages.pdf", ["First native PDF page text", "Second native PDF page text", "Third native PDF page text"])
    data = request(path, max_pages=1)
    data["start_page"] = 2
    result = worker.read_pdf(data)
    assert result["reading_status"] == "partial"
    assert result["read_pages"] == [2]
    assert "First" not in result["content"]
    assert "Second" in result["content"]
    assert result["omitted_pages"] == 2
    assert "max_pages" in result["limits_reached"]


def test_character_budget_includes_page_markers(tmp_path):
    path = make_pdf(tmp_path / "long-scan.pdf", ["scan"])
    result = worker.read_pdf(request(path, max_chars=40), ocr_factory=lambda: lambda image: output("正文" * 100))
    assert result["reading_status"] == "partial"
    assert len(result["content"]) == 40
    assert result["limits_reached"] == ["max_chars"]


def test_budget_cannot_return_only_a_page_heading(tmp_path):
    path = make_pdf(tmp_path / "tiny-budget.pdf", ["scan"])
    result = worker.read_pdf(request(path, max_chars=5), ocr_factory=lambda: lambda image: output())
    assert result["reading_status"] == "unavailable"
    assert result["content"] == ""


def test_render_pixel_and_edge_budgets_are_hard_limits(tmp_path):
    path = make_pdf(tmp_path / "pixels.pdf", ["scan"])
    images = []

    def recognize(image):
        images.append(image)
        return output()

    result = worker.read_pdf(request(path, max_page_pixels=4096, max_image_edge=64), ocr_factory=lambda: recognize)
    assert result["reading_status"] == "success"
    assert images[0].shape[0] * images[0].shape[1] <= 4096
    assert max(images[0].shape[:2]) <= 64


def test_shared_pixel_budget_stops_before_another_page(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "pixels-total.pdf", ["scan", "scan"])
    render_calls = []

    def render(page, module, limits, remaining):
        render_calls.append(remaining)
        return object(), remaining

    monkeypatch.setattr(worker, "_render", render)
    result = worker.read_pdf(request(path, max_total_pixels=1000), ocr_factory=lambda: lambda image: output())
    assert render_calls == [1000]
    assert result["reading_status"] == "partial"
    assert result["read_pages"] == [1]
    assert "max_total_pixels" in result["limits_reached"]


def test_real_tool_executes_isolated_native_reader(tmp_path):
    path = make_pdf(tmp_path / "isolated.pdf", ["Synthetic native PDF sample"])
    result = PdfOcr().execute({"path": str(path)})
    assert result.status == "success"
    assert result.result["reading_status"] == "success"
    assert "Synthetic native PDF sample" in result.result["content"]
    assert result.result["path"] == str(path.resolve())


@pytest.mark.parametrize("data", [{}, {"path": "https://example.org/test.pdf"}, {"path": "\\\\server\\test.pdf"},
                                  {"path": "missing.pdf"}, {"path": "local.txt"}])
def test_invalid_local_paths_never_launch_reader(monkeypatch, data):
    monkeypatch.setattr(PdfOcr, "_run_worker", lambda *args: pytest.fail("非法路径不得启动 OCR"))
    result = PdfOcr().execute(data)
    assert result.status == "error"
    assert result.result["reading_status"] == "unavailable"
    assert not result.result["content"]


def test_credential_pdf_alias_is_rejected_before_open(monkeypatch, tmp_path):
    alias = tmp_path / "secrets.pdf"
    from agent.tools.pdf_ocr import pdf_ocr

    monkeypatch.setattr(pdf_ocr.os.path, "realpath", lambda path: str(alias))
    monkeypatch.setattr(pdf_ocr.Read, "_is_credential_path", lambda self, path: True)
    monkeypatch.setattr(PdfOcr, "_run_worker", lambda *args: pytest.fail("凭据链接不得启动 OCR"))
    result = PdfOcr().execute({"path": str(alias)})
    assert result.status == "error"
    assert "凭据" in result.result["message"]


def test_disguised_pdf_is_not_parsed(tmp_path, monkeypatch):
    path = tmp_path / "not-pdf.pdf"
    path.write_text("not a PDF", encoding="utf-8")
    monkeypatch.setattr(PdfOcr, "_run_worker", lambda *args: pytest.fail("伪 PDF 不得启动 OCR"))
    result = PdfOcr().execute({"path": str(path)})
    assert result.status == "error"
    assert "PDF 标识" in result.result["message"]


@pytest.mark.parametrize("start,max_pages", [(0, 1), (True, 1), (1, "5"), (1, 0)])
def test_invalid_page_args_are_rejected(tmp_path, monkeypatch, start, max_pages):
    path = make_pdf(tmp_path / "args.pdf", ["Synthetic native PDF sample"])
    monkeypatch.setattr(PdfOcr, "_run_worker", lambda *args: pytest.fail("非法预算不得启动 OCR"))
    assert PdfOcr().execute({"path": str(path), "start_page": start, "max_pages": max_pages}).status == "error"


def test_configured_page_and_file_budgets_are_enforced(tmp_path, monkeypatch):
    path = make_pdf(tmp_path / "budget.pdf", ["Synthetic native PDF sample"])
    requests = []

    def run(self, data):
        requests.append(data)
        return {"reading_status": "success", "content": "合成正文"}

    monkeypatch.setattr(PdfOcr, "_run_worker", run)
    assert PdfOcr({"max_pages": 2}).execute({"path": str(path), "max_pages": 500}).status == "success"
    assert requests[0]["limits"]["max_pages"] == 2
    assert PdfOcr({"max_file_bytes": 1}).execute({"path": str(path)}).status == "error"
    assert len(requests) == 1


@pytest.mark.parametrize("partial", [True, False])
def test_timeout_retains_only_completed_real_body(monkeypatch, tmp_path, partial):
    tool = PdfOcr()
    data = request(tmp_path / "file.pdf")

    def timeout(command, **kwargs):
        assert command[0] == __import__("sys").executable
        assert kwargs["timeout"] == 90
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        payload = tool._unavailable("读取中", DEFAULT_LIMITS)
        payload.update(content="已完成第一页真实正文" if partial else "", total_pages=3,
                       processed_pages=1, read_pages=[1] if partial else [], omitted_pages=2)
        Path(command[-1]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        raise subprocess.TimeoutExpired(command, 90)

    monkeypatch.setattr(subprocess, "run", timeout)
    result = tool._run_worker(data)
    assert result["reading_status"] == ("partial" if partial else "unavailable")
    assert result["limits_reached"] == ["timeout"]
    assert "超时" in result["message"]


def test_corrupt_and_encrypted_pdf_do_not_report_body(tmp_path):
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.7\ncorrupt")
    assert worker.read_pdf(request(corrupt))["reading_status"] == "unavailable"
    path = tmp_path / "encrypted.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((40, 60), "Sensitive text")
    document.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="synthetic_password")
    document.close()
    result = worker.read_pdf(request(path))
    assert result["reading_status"] == "unavailable"
    assert not result["content"]
    assert "密码" in result["message"]


def test_tool_manager_discovers_pdf_ocr_without_initializing_models():
    import agent.tools as tools_package

    assert "PdfOcr" in tools_package.__all__
    assert tools_package.PdfOcr is PdfOcr
    assert tools_package.PdfOcr().name == "pdf_ocr"
