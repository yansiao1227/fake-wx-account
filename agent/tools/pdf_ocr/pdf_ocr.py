"""扫描 PDF 的一次有界读取；耗时解析与 OCR 在可终止子进程内执行。"""

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from agent.tools.base_tool import BaseTool, ToolResult
from agent.tools.read.read import Read
from common.utils import expand_path


DEFAULT_LIMITS = {
    "max_pages": 8,
    "max_chars": 20000,
    "max_file_bytes": 100 * 1024 * 1024,
    "max_page_pixels": 4000000,
    "max_total_pixels": 20000000,
    "max_image_edge": 2500,
    "dpi": 144,
    "timeout_seconds": 90,
}
HARD_LIMITS = {
    "max_pages": 30,
    "max_chars": 50000,
    "max_file_bytes": 200 * 1024 * 1024,
    "max_page_pixels": 8000000,
    "max_total_pixels": 60000000,
    "max_image_edge": 4000,
    "dpi": 200,
    "timeout_seconds": 180,
}


class PdfOcr(BaseTool):
    name = "pdf_ocr"
    description = (
        "读取本地 PDF 正文：优先原生文本，扫描页使用本地 RapidOCR。"
        "一次调用有页面、像素、字符及总耗时限制。必须检查 reading_status 和 content；"
        "partial 只能分析已读取的页，unavailable 没有读到正文。"
        "失败后告知用户，不得换参数循环 OCR，也不得用文件名或元信息代替正文。"
    )
    params = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "已取得的本地 PDF 路径，优先绝对路径"},
            "start_page": {"type": "integer", "minimum": 1, "default": 1,
                           "description": "从第几页开始（从 1 起）"},
            "max_pages": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8,
                          "description": "本次最多读取的页面数，不超过运行配置预算"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, config=None):
        self.config = config or {}

    def _limits(self):
        limits = {}
        for name, default in DEFAULT_LIMITS.items():
            value = self.config.get(name, default)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                value = default
            limits[name] = max(1, min(int(value), HARD_LIMITS[name]))
        return limits

    @staticmethod
    def _unavailable(reason, limits=None):
        return {
            "type": "pdf_text", "reading_status": "unavailable", "content": "",
            "total_pages": 0, "processed_pages": 0, "read_pages": [], "ocr_pages": [],
            "native_text_pages": [], "unreadable_pages": [], "omitted_pages": 0,
            "truncated": False, "limits_reached": [], "limits": limits or {},
            "message": reason,
        }

    def execute(self, args):
        limits = self._limits()
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolResult.fail(self._unavailable("缺少本地 PDF 路径。", limits))
        raw_path = raw_path.strip()
        if "://" in raw_path or raw_path.startswith("\\\\"):
            return ToolResult.fail(self._unavailable("只接受已取得的本地 PDF 文件，不接受网址或网络共享。", limits))
        reader = Read({"cwd": self.config.get("cwd", os.getcwd())})
        path = os.path.realpath(reader._resolve_path(raw_path))
        env_path = os.path.normcase(os.path.realpath(expand_path("~/.cow/.env")))
        if reader._is_credential_path(path) or os.path.normcase(path) == env_path:
            return ToolResult.fail(self._unavailable("不允许读取凭据文件或其链接。", limits))
        if Path(path).suffix.lower() != ".pdf" or not os.path.isfile(path):
            return ToolResult.fail(self._unavailable("路径必须指向存在的本地 PDF 文件。", limits))
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as source:
                header = source.read(1024)
        except OSError:
            return ToolResult.fail(self._unavailable("PDF 文件不可读。", limits))
        if size > limits["max_file_bytes"]:
            return ToolResult.fail(self._unavailable("PDF 超过本次文件大小限制。", limits))
        if b"%PDF-" not in header:
            return ToolResult.fail(self._unavailable("文件没有有效的 PDF 标识，未执行 OCR。", limits))
        start_page = args.get("start_page", 1)
        max_pages = args.get("max_pages", limits["max_pages"])
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
               for value in (start_page, max_pages)):
            return ToolResult.fail(self._unavailable("start_page 和 max_pages 必须是正整数。", limits))
        request = {"path": path, "start_page": start_page,
                   "limits": {**limits, "max_pages": min(max_pages, limits["max_pages"])}}
        payload = self._run_worker(request)
        payload["path"] = path
        payload["limits"] = request["limits"]
        if payload.get("reading_status") in ("success", "partial") and payload.get("content", "").strip():
            return ToolResult.success(payload)
        return ToolResult.fail(payload)

    def _run_worker(self, request):
        # 不使用 shell，不向日志打印附件正文；初始化、解析和推理共用一个硬超时。
        with tempfile.TemporaryDirectory(prefix="cowagent-pdf-ocr-") as directory:
            output = Path(directory) / "result.json"
            command = [sys.executable, "-X", "utf8", str(Path(__file__).with_name("worker.py")), str(output)]
            timed_out = False
            try:
                process = subprocess.run(
                    command, input=json.dumps(request, ensure_ascii=False),
                    text=True, encoding="utf-8", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=request["limits"]["timeout_seconds"], check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                # subprocess.run 会杀死并等待子进程；读取它最后一页完成后的原子快照。
                timed_out = True
                process = None
            except OSError:
                return self._unavailable("无法启动 PDF 读取进程；本次读取已结束。", request["limits"])
            try:
                if output.stat().st_size > request["limits"]["max_chars"] * 6 + 100000:
                    raise ValueError("oversized output")
                payload = json.loads(output.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("invalid output")
            except (OSError, ValueError):
                payload = self._unavailable("PDF 读取进程未取得正文；本次读取已结束。", request["limits"])
            if timed_out or (process is not None and process.returncode):
                reason = "timeout" if timed_out else "worker_failed"
                payload["limits_reached"] = list(dict.fromkeys([*payload.get("limits_reached", []), reason]))
                payload["truncated"] = True
                payload["reading_status"] = "partial" if payload.get("content", "").strip() else "unavailable"
                payload["message"] = "PDF 读取超时，已终止 OCR；只能分析已取得的正文。" if timed_out else "PDF 读取进程失败，本次读取已结束。"
            return payload
