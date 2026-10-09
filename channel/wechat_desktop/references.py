"""引用能力的纯判断；数据库解析、UIA 操作和 Agent 阅读各自执行。"""

from __future__ import annotations


def reference_requires_uia(reference: dict | None) -> bool:
    """已完整文字或已有分享链接不占微信窗口；缺失原文才按需定位。"""
    if not reference:
        return False
    kind = str(reference.get("content_type") or "text").lower()
    if kind in {"image", "file"}:
        return True
    if kind == "share_card":
        browser_body = bool(str(reference.get("browser_content") or "").strip()) and str(
            reference.get("browser_status") or "").lower() in {"", "success", "direct_browser"}
        fetched_body = (reference.get("fetch_status") == "success"
                        and bool(str(reference.get("fetched_content") or "").strip()))
        return not (str(reference.get("url") or "").strip() or browser_body or fetched_body)
    # 原文类型未识别或文字预览缺失时，保留原定位能力。已知语音、视频等
    # 仅保留明确类型，不以其他文件或文本冒充它们的内容。
    return kind in {"text", "app_message", "unsupported", "unknown"} and not reference.get("resolved", False)
