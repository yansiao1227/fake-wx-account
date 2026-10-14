"""引用能力的纯判断；数据库解析、UIA 操作和 Agent 阅读各自执行。"""

from __future__ import annotations


def is_chat_record_title(value: str) -> bool:
    """识别原生应用消息的合并转发标题，不能据此声称已读取正文。"""
    title = str(value or "").strip()
    if title.startswith("[聊天记录]"):
        return True
    return len(title) > len("的聊天记录") and title.endswith("的聊天记录")


def chat_record_ready(payload: dict | None) -> bool:
    """标题、旧 resolved 标记或单独的成功状态均不能证明取得记录正文。"""
    return bool(
        isinstance(payload, dict)
        and payload.get("status") in {"success", "partial"}
        and str(payload.get("content") or "").strip()
        and isinstance(payload.get("entries"), list)
        and payload["entries"]
    )


def event_chat_record(event) -> dict | None:
    """返回当前待处理记录；兼容旧事件中被错误升级为文本的应用卡片。"""
    reference = event.reference or {}
    if reference:
        kind = str(reference.get("content_type") or "").lower()
        legacy_card = (
            str(reference.get("native_type") or "") == "49"
            and str(reference.get("native_app_type") or "") in {"", "0", "19"}
            and reference.get("source_content_type") != "text"
            and is_chat_record_title(reference.get("title") or reference.get("content"))
        )
        if kind == "chat_record" or legacy_card:
            payload = reference.get("chat_record")
            return payload if isinstance(payload, dict) else {}
        return None
    if event.content_type == "chat_record":
        payload = getattr(event, "chat_record", None)
        return payload if isinstance(payload, dict) else {}
    return None


def event_native_xml_unavailable(event) -> bool:
    """原生应用 XML 读取失败不能退回原始 XML 或让 Agent 自行补文。"""
    return bool(
        event.attachment_status == "native_xml_unavailable"
        or (event.reference or {}).get("native_xml_status") in {"unavailable", "invalid"}
    )


def standalone_attachment_requires_reference(event) -> bool:
    """特殊原消息只登记或缓存；明确引用后才允许读取和自动回复。"""
    return not event.reference and (
        event.content_type in {"image", "file", "share_card", "chat_record"}
        or event_native_xml_unavailable(event)
    )


def reference_requires_uia(reference: dict | None) -> bool:
    """已完整文字或已有分享链接不占微信窗口；缺失原文才按需定位。"""
    if not reference:
        return False
    if reference.get("native_xml_status") in {"unavailable", "invalid"}:
        return False
    kind = str(reference.get("content_type") or "text").lower()
    if kind == "chat_record":
        # 合并转发正文由已解密原生 XML 前置解析。缺失时由 FIFO 一次终结，
        # 定位气泡取得的标题不能替代正文，也不让 Agent 再查询其他会话。
        return False
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
