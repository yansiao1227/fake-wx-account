"""已验证原生消息的 UIA 附件物化；不发现消息或按文件名复用附件。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from dataclasses import replace

from channel.wechat_desktop.models import UiaChatMessage, WechatDesktopEvent


class WechatUiaMaterializer:
    """只有已绑定账号和数据库来源的事件可以操作 UIA。"""

    def __init__(self, config: dict, *, gateway):
        self.config = config
        self.gateway = gateway
        self.client = gateway.client
        self._attachment_cache_lock = threading.RLock()
        self._attachment_path_cache = OrderedDict()

    def close(self):
        with self._attachment_cache_lock:
            self._attachment_path_cache.clear()

    @staticmethod
    def _require_valid(validate):
        if validate is not None and validate() is False:
            raise RuntimeError("attachment_target_changed")

    @staticmethod
    def _can_refine_reference(reference):
        kind = str(reference.get("content_type") or "unknown").lower()
        return kind in {"unknown", "app_message", "unsupported"} or (
            kind == "text" and not reference.get("resolved", False)
        )

    @classmethod
    def _check_reference_type(cls, event, message):
        if not event.reference:
            return
        if message.reference is None:
            raise RuntimeError("attachment_reference_type_changed")
        expected = str(event.reference.get("content_type") or "unknown").lower()
        if not cls._can_refine_reference(event.reference) and expected != message.reference.message_type:
            raise RuntimeError("attachment_reference_type_changed")

    def materialize_event(self, event: WechatDesktopEvent, *, target_message: UiaChatMessage,
                          validate=None, validate_target=None):
        """保留数据库身份和元数据，只补充已验证消息的附件与引用正文。"""
        if not isinstance(target_message, UiaChatMessage):
            raise TypeError("附件物化需要已验证的 UiaChatMessage 快照")
        if not event.account_id or not event.source_message_id or not event.conversation_id:
            raise RuntimeError("attachment_native_identity_required")
        self._check_reference_type(event, target_message)
        if (event.content_type in {"image", "file"}
                and target_message.message_type != event.content_type):
            raise RuntimeError("attachment_message_type_changed")
        invalidated = False

        def require_context():
            nonlocal invalidated
            if invalidated:
                raise RuntimeError("attachment_target_changed")
            try:
                self._require_valid(validate_target)
            except Exception:
                invalidated = True
                raise

        with self.gateway.operation(reply=False):
            self._require_valid(validate)
            require_context()
            resolved, resolve_count = self._resolve_target(event, target_message, require_context)
            require_context()
            self._check_reference_type(event, resolved)
            self._apply_resolved_message(event, resolved)
        return event, resolve_count

    @classmethod
    def _apply_resolved_message(cls, event: WechatDesktopEvent, resolved: UiaChatMessage):
        cls._check_reference_type(event, resolved)
        if resolved.message_type in {"file", "image"}:
            if resolved.file_path and os.path.isfile(resolved.file_path):
                event.content = resolved.file_path
                event.attachment_status = "materialized"
                if resolved.message_type == "image":
                    event.evidence_path = resolved.file_path
            else:
                event.attachment_status = "unavailable"
        if resolved.reference is None:
            return
        native = event.reference
        reference = resolved.reference
        if reference.message_type in {"image", "file"}:
            available = bool(reference.file_path and os.path.isfile(reference.file_path))
            reference = replace(reference, file_path=reference.file_path if available else "",
                                resolved=available, degraded=not available)
        elif reference.message_type == "share_card":
            browser_content = str(reference.browser_content or native.get("browser_content") or "").strip()
            browser_status = str(reference.browser_status or native.get("browser_status") or "")
            if not browser_content and browser_status in {"", "success"}:
                browser_status = "unavailable"
            body_available = bool(browser_content) and browser_status.lower() in {"", "success", "direct_browser"}
            url = str(native.get("url") or reference.url or "").strip()
            reference = replace(
                reference, url=url, browser_content=browser_content,
                browser_status="success" if body_available else browser_status,
                fetched_content="", fetch_status=("direct_browser" if body_available else
                    "link_available" if url else "link_unavailable"),
                resolved=body_available, degraded=not body_available,
            )
        can_refine = cls._can_refine_reference(native)
        event.reference = {
            **native,
            "sender_name": native.get("sender_name", reference.sender_name),
            "content": (reference.content if can_refine else native.get("content", reference.content)),
            "content_type": (reference.message_type if can_refine else
                             native.get("content_type", reference.message_type)),
            "file_path": reference.file_path,
            "resolved": reference.resolved,
            "degraded": reference.degraded,
            "strategy": reference.strategy,
            "original_content": reference.original_content or native.get("original_content", ""),
            "url": native.get("url") or reference.url,
            "platform": native.get("platform") or reference.platform,
            "browser_content": reference.browser_content,
            "browser_status": reference.browser_status,
            "fetched_content": reference.fetched_content,
            "fetch_status": reference.fetch_status,
            "depth": 1,
        }
        if reference.message_type == "share_card":
            event.reference["fetch_source"] = (
                "direct_browser" if reference.fetch_status == "direct_browser" else ""
            )
        if reference.file_path:
            event.evidence_path = reference.file_path
        reference_content = event.reference["content"]
        if reference.file_path:
            marker = "文件" if reference.message_type == "file" else "图片"
            reference_content = f"[{marker}: {reference.file_path}]"
        previous = next((item for item in event.history if item.get("is_reference")), {})
        fragment = {
            **previous, "sender_name": event.reference["sender_name"] if event.is_group else "",
            "content": reference_content, "content_type": event.reference["content_type"],
            "is_reference": True, "resolved": reference.resolved, "degraded": reference.degraded,
            "strategy": reference.strategy,
        }
        event.history = [item for item in event.history if not item.get("is_reference")]
        event.history.append(fragment)
        event.attachment_status = "materialized" if reference.resolved else "unavailable"

    @staticmethod
    def _content_signature(message):
        reference = message.reference
        payload = {"type": message.message_type, "content": message.content,
                   "reference": ({"type": reference.message_type, "content": reference.content}
                                 if reference is not None else None)}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def _resolve_target(self, event, message, validate_target):
        cache_key = (event.account_id, event.source_message_id, event.conversation_id,
                     message.runtime_id, event.content_signature, self._content_signature(message))
        # RuntimeId 缺失时不允许通过缓存绕过具体附件 API 的身份检查。
        if message.runtime_id:
            with self._attachment_cache_lock:
                path = self._attachment_path_cache.get(cache_key, "")
                if path and os.path.isfile(path):
                    self._attachment_path_cache.move_to_end(cache_key)
                    if message.reference is not None:
                        return replace(message, reference=replace(
                            message.reference, file_path=path, resolved=True, degraded=False)), 0
                    return replace(message, file_path=path), 0
                self._attachment_path_cache.pop(cache_key, None)
        count = 0
        if message.reference is not None:
            if message.reference.message_type == "image":
                count = 1
                path = self.client.fetch_referenced_message_image(
                    message, strict=True, validate_target=validate_target)
                message = replace(message, reference=replace(
                    message.reference, file_path=path, resolved=bool(path), degraded=not bool(path),
                    strategy="wechat_reference_image_viewer"))
            elif bool(self.config.get("resolve_message_references", True)):
                count = 1
                message = self.client.resolve_message_reference(
                    message, allow_filename_cache=False, validate_target=validate_target)
            else:
                message = replace(message, reference=replace(
                    message.reference, resolved=False, degraded=True, strategy="uia_reference_preview_only"))
        elif message.message_type == "image":
            count = 1
            path = self.client.fetch_message_image(
                message, prefer_viewer=True, strict=True, validate_target=validate_target)
            message = replace(message, file_path=path)
        elif message.message_type == "file":
            count = 1
            path = self.client.fetch_message_file(
                message, allow_filename_cache=False, validate_target=validate_target)
            message = replace(message, file_path=path)
        self._require_valid(validate_target)
        self._check_reference_type(event, message)
        path = str(message.reference.file_path if message.reference is not None else message.file_path or "")
        if message.runtime_id and path and os.path.isfile(path):
            with self._attachment_cache_lock:
                self._attachment_path_cache[cache_key] = path
                self._attachment_path_cache.move_to_end(cache_key)
                while len(self._attachment_path_cache) > 256:
                    self._attachment_path_cache.popitem(last=False)
        return message, count
