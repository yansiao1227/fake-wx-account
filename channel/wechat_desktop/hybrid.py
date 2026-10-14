"""数据库接收与 UIA 操作的组合后端；跨来源协调留在通道层。"""

from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
import threading
import time
import unicodedata
import re
from pathlib import Path
from channel.wechat_desktop.config import DEFAULT_CONFIG

from channel.wechat_desktop.backend import WechatDesktopBackend
from channel.wechat_desktop.binding import DatabaseUiaTargetBinder
from channel.wechat_desktop.contracts import ConversationTarget, SendResult, SendStatus, TargetStatus
from channel.wechat_desktop.conversation import conversation_header_matches_target
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.source import WechatDatabaseSource
from channel.wechat_desktop.models import ReplyTargetValidation, WechatHistoryReadError
from channel.wechat_desktop.references import (
    chat_record_ready, event_chat_record, event_native_xml_unavailable, reference_requires_uia,
)
from channel.wechat_desktop.send_control import SendNotSubmitted


class WechatDatabaseBackend(WechatDesktopBackend):
    capabilities = {"background_observation": True, "database_history": True, "contact_search": True}

    def __init__(self, config, *, db_reader=None, store=None, client=None, uia_gateway=None):
        self.config = config
        self._store = store
        self._client = client
        self._gateway = uia_gateway
        self._actions_lock = threading.RLock()
        self._closed = False
        self.source = WechatDatabaseSource(config, reader=db_reader, checkpoint_store=store,
                                           checkpoint_store_provider=self._ledger)
        self._binder = DatabaseUiaTargetBinder(self.source, self._actions)

    def _ledger(self):
        if self._store is None:
            from channel.wechat_desktop.storage.service import get_wechat_desktop_service
            self._store = get_wechat_desktop_service().store
        return self._store

    @property
    def uia_initialized(self):
        return self._gateway is not None

    def _actions(self):
        with self._actions_lock:
            if self._closed:
                raise DatabaseReadError("backend_closed", "微信组合后端已关闭")
            if self._gateway is None:
                from channel.wechat_desktop.uia.gateway import WechatUiaGateway
                self._gateway = WechatUiaGateway(self.config, client=self._client)
            return self._gateway

    def _status(self, status=None):
        return {**(self.source.status() if status is None else status),
                "mode": "db_uia", "desktop_backend": "db_uia", "automation_backend": "db_uia",
                "uia_available": self.uia_initialized and not self._closed,
                "target_identity_verification": self._binder.identity_verification}

    def observe_events(self):
        status, events = self.source.observe_events()
        for event in events:
            self._mark_attachment_capability(event)
        return self._status(status), events

    def acknowledge_events(self, event_ids):
        self.source.acknowledge_events(event_ids)

    def wait_for_changes(self, stop_event):
        return self.source.wait_for_changes(stop_event)

    def ensure_foreground(self):
        return self._actions().ensure_foreground()

    def resolve_target(self, conversation):
        return self._binder.resolve_target(conversation)

    def resolve_send_target(self, conversation):
        # 授权阶段只读取数据库身份；策略、额度和幂等检查通过后才绑定窗口。
        return self._binder.resolve_send_target(conversation)

    def validate_reply_target(self, event):
        try:
            valid, reason = self.source.validate_event(event)
            return ReplyTargetValidation(valid, reason)
        except Exception as exc:
            return ReplyTargetValidation(False, getattr(exc, "code", "source_validation_failed"))

    def _send(self, method, conversation, payload, *, authorized_target=None):
        epoch = self.source.session_epoch
        result = self.resolve_target(conversation)
        if result.status != TargetStatus.RESOLVED or result.target is None:
            return SendResult(SendStatus.NOT_SENT, result.reason)
        if authorized_target is not None and result.target != authorized_target:
            return SendResult(SendStatus.NOT_SENT, "authorized_target_changed")
        if epoch != self.source.session_epoch:
            return SendResult(SendStatus.NOT_SENT, "account_binding_changed")
        cid = result.target.conversation_id
        response = SendResult.from_backend(getattr(self._actions(), method)(
            cid, payload, validate=lambda: self._binder.require_target(
                cid, session_epoch=epoch, authorized_target=authorized_target)))
        return replace(response, accepted_by="db_uia", observation={**response.observation, **self._status()})

    def send_text(self, conversation, text, *, authorized_target=None):
        return self._send("send_text", conversation, text, authorized_target=authorized_target)

    def send_interim_text(self, conversation, text, *, authorized_target=None):
        return self._send("send_interim_text", conversation, text, authorized_target=authorized_target)

    def send_image(self, conversation, image_path, *, authorized_target=None):
        return self._send("send_image", conversation, image_path, authorized_target=authorized_target)

    @staticmethod
    def _mark_attachment_capability(event):
        # 独立图片/分享卡片只观察，不主动打开；文件与明确引用按需关联 UIA。
        if event_native_xml_unavailable(event):
            return
        if event.content_type not in {"text", "share_card", "chat_record"}:
            event.attachment_status = "uia_identity_unavailable"
            if event.reference:
                event.reference["degraded"] = True

    @staticmethod
    def _needs_attachment(event):
        return event.content_type == "file" or reference_requires_uia(event.reference)

    @staticmethod
    def _correlation_token(message):
        kind = str(getattr(message, "content_type", getattr(message, "message_type", "")))
        content = unicodedata.normalize("NFKC", str(message.content or "")).replace("\r\n", "\n").strip()
        if kind == "file":
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            if lines and lines[0].casefold() in {"文件", "file"}:
                content = lines[1] if len(lines) > 1 else ""
            if content in {"[文件]", "[File]"}:
                content = ""
        elif kind == "share_card":
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            content = lines[0] if lines else ""
            for prefix in ("[链接]", "[link]"):
                if content.casefold().startswith(prefix.casefold()):
                    content = content[len(prefix):].strip() or (lines[1] if len(lines) > 1 else "")
                    break
        elif kind == "image" and content in {"[图片]", "[Image]", "图片", "image"}:
            content = "[图片]"
        elif kind == "chat_record":
            # 原生标题和 UIA 卡片的标题/首屏摘要比较；媒体摘要不是正文。
            match = re.match(r"^(?:\[聊天记录\]|聊天记录)?(.+?的聊天记录)", content)
            if match:
                content = match.group(1)
        return kind, content

    @classmethod
    def _messages_match(cls, native, visible):
        if cls._correlation_token(native) != cls._correlation_token(visible):
            return False
        if visible.direction in {"incoming", "outgoing"} and native.direction != visible.direction:
            return False
        # 私聊已由账号及会话绑定限制；群发送者若 UIA 提供身份则必须一致。
        if visible.sender_name not in {"", "unknown", "自己"} and native.sender_name not in {"", "unknown", "自己"}:
            if visible.sender_name != native.sender_name:
                return False
        return True

    @classmethod
    def _attachment_sequence_start(cls, history, messages, *, allow_non_tail=False):
        native = [message for message in history.messages if message.direction != "system"]
        visible = list(messages)
        if len(visible) < 2 or len(visible) > 20 or len(native) < len(visible):
            raise SendNotSubmitted("attachment_sequence_insufficient")
        tokens = [cls._correlation_token(message) for message in visible]
        if any(not token[1] for token in tokens) or len(set(tokens)) < 2:
            raise SendNotSubmitted("attachment_sequence_ambiguous")
        candidates = [start for start in range(len(native) - len(visible) + 1)
                      if all(cls._messages_match(left, right)
                             for left, right in zip(native[start:start + len(visible)], visible))]
        if len(candidates) != 1 or (not allow_non_tail and candidates[0] != len(native) - len(visible)):
            raise SendNotSubmitted("attachment_sequence_ambiguous")
        return candidates[0]

    @classmethod
    def _correlate_attachment(cls, event, history, messages, *, allow_non_tail=False):
        start = cls._attachment_sequence_start(history, messages, allow_non_tail=allow_non_tail)
        native = [message for message in history.messages if message.direction != "system"]
        visible = list(messages)
        if sum(message.source_message_id == event.source_message_id for message in native) != 1:
            raise SendNotSubmitted("attachment_native_message_ambiguous")
        aligned = native[start:start + len(visible)]
        positions = [index for index, message in enumerate(aligned)
                     if message.source_message_id == event.source_message_id]
        if len(positions) != 1:
            raise SendNotSubmitted("attachment_message_not_visible")
        matched = visible[positions[0]]
        # UIA 不提供原生时间/主键；相同附件标题或同文引用可能在跨分片同秒消息中
        # 交换顺序。即使两端序列外观一致，也不能替其中任意一条声称原生身份。
        target_token = cls._correlation_token(matched)
        if (sum(cls._correlation_token(item) == target_token for item in native) != 1
                or sum(cls._correlation_token(item) == target_token for item in visible) != 1):
            raise SendNotSubmitted("attachment_message_ambiguous")
        if not matched.runtime_id or sum(item.runtime_id == matched.runtime_id for item in visible) != 1:
            raise SendNotSubmitted("attachment_control_identity_unavailable")
        # UIA 只能补全原生引用，不能把无引用的原生文件升级成引用任务。
        if bool(matched.reference) != bool(event.reference):
            raise SendNotSubmitted("attachment_reference_mismatch")
        if event.reference:
            reference = matched.reference
            expected = unicodedata.normalize("NFKC", str(event.reference.get(
                "preview_content", event.reference.get("content", "")))).strip()
            actual = unicodedata.normalize("NFKC", str(reference.content or "")).strip()
            if event.reference.get("content_type") == "chat_record":
                expected = expected.removeprefix("[聊天记录]").strip()
                actual = actual.removeprefix("[聊天记录]").strip()
            if event.reference.get("content_type") == "image":
                expected = "[图片]" if expected in {"[图片]", "图片", "image", "[Image]"} else expected
                actual = "[图片]" if actual in {"[图片]", "图片", "image", "[Image]"} else actual
            if expected and actual != expected:
                raise SendNotSubmitted("attachment_reference_mismatch")
            sender = str(event.reference.get("sender_name", ""))
            if sender and sender != "unknown" and reference.sender_name and sender != reference.sender_name:
                raise SendNotSubmitted("attachment_reference_sender_mismatch")
            # UIA 预览不总能区分分享卡片与文本，类型来自已校验的数据库记录。
            expected_type = str(event.reference.get("content_type") or "text")
            if expected_type in {"image", "file", "share_card", "chat_record"}:
                matched = replace(matched, reference=replace(reference, message_type=expected_type))
        return matched

    def _locate_attachment(self, original, history, messages, gateway, *, validate, validate_viewport=None):
        """先证明原生尾部，再在同一消息列表中有限上滚寻找近期附件。"""
        viewport_check = validate_viewport or validate
        viewport_check()
        list_identity = gateway.inspect_attachment_message_list(original.conversation_id)
        try:
            matched = self._correlate_attachment(original, history, messages)
        except SendNotSubmitted as exc:
            if str(exc) != "attachment_message_not_visible":
                raise
        else:
            if gateway.attachment_message_fully_visible(original.conversation_id, matched,
                    expected_list_identity=list_identity):
                return matched, messages, False, list_identity
        # 上面的匹配已证明唯一原生尾部且目标在这份历史中唯一存在。
        history_signature = self._history_signature(history)
        epoch = self.source.session_epoch

        def require_history():
            refreshed = self.source.read_chat_history(original.conversation_id, 50,
                                                      session_epoch=epoch)
            validate()
            if self._history_signature(refreshed) != history_signature:
                raise SendNotSubmitted("attachment_database_sequence_changed")

        # 初始历史和尾部快照已在调用方完整验证。滚动内部只读 UI 身份，
        # 每页开始/结束各校验一次完整原生源和数据库历史，避免每个原子步骤
        # 反复全库刷新而耗尽有界定位时间。
        max_scrolls = max(1, min(30, int(self.config.get(
            "uia_attachment_scroll_wheel_times", DEFAULT_CONFIG["uia_attachment_scroll_wheel_times"]))))
        timeout = max(.1, min(30., float(self.config.get(
            "uia_attachment_history_timeout_seconds", DEFAULT_CONFIG["uia_attachment_history_timeout_seconds"]))))
        deadline = time.monotonic() + timeout
        previous = messages
        for _ in range(max_scrolls):
            if time.monotonic() >= deadline:
                raise SendNotSubmitted("attachment_locator_timeout")
            require_history()
            if time.monotonic() >= deadline:
                raise SendNotSubmitted("attachment_locator_timeout")
            gateway.scroll_attachment_history(original.conversation_id,
                expected_list_identity=list_identity, validate=viewport_check)
            current = gateway.read_attachment_snapshot(original.conversation_id, allow_non_bottom=True)
            require_history()
            if gateway.inspect_attachment_message_list(original.conversation_id) != list_identity:
                raise SendNotSubmitted("attachment_message_list_changed")
            if time.monotonic() >= deadline:
                raise SendNotSubmitted("attachment_locator_timeout")
            # Qt 可能复用 runtime_id；位置和内容均未变化才是停止前进。
            if current == previous:
                raise SendNotSubmitted("attachment_locator_stuck")
            try:
                matched = self._correlate_attachment(original, history, current, allow_non_tail=True)
            except SendNotSubmitted as exc:
                if str(exc) != "attachment_message_not_visible":
                    raise
            else:
                if gateway.attachment_message_fully_visible(original.conversation_id, matched,
                        expected_list_identity=list_identity):
                    return matched, current, True, list_identity
            previous = current
        raise SendNotSubmitted("attachment_locator_exhausted")

    @staticmethod
    def _history_signature(history):
        return tuple((message.source_message_id, message.native_timestamp, message.direction,
                      message.content_type, message.sender_name, message.content) for message in history.messages)

    def materialize_event(self, event):
        original = deepcopy(event)
        epoch = self.source.session_epoch
        try:
            valid, reason = self.source.validate_event(original)
            if not valid:
                raise SendNotSubmitted(reason)
            if epoch != self.source.session_epoch:
                raise SendNotSubmitted("account_binding_changed")
            if event.reference and not event.reference.get("source_native_message_id"):
                event = self.source.enrich_reference(event, session_epoch=epoch)
            original = deepcopy(event)
        except Exception as exc:
            return self._source_invalid(event, exc), 0
        if event_native_xml_unavailable(event):
            event.attachment_status = "native_xml_unavailable"
            return event, 0
        record = event_chat_record(event)
        record_media = bool(record is not None and chat_record_ready(record)
            and event.reference.get("content_type") == "chat_record"
            and any(entry.get("content_type") == "image" and not entry.get("resolved")
                    for entry in record.get("entries", []))
            and self.config.get("chat_record_uia_media_enabled", DEFAULT_CONFIG["chat_record_uia_media_enabled"]))
        if record is not None:
            # 数据库已经在原生快照中解析合并转发正文，UIA 卡片标题不是后备正文。
            # 缺正文也明确返回，交由 FIFO 一次回复后收尾，不让 Agent 自行解密或找会话。
            ready = chat_record_ready(record)
            event.attachment_status = "materialized" if ready else "unavailable"
            if event.reference:
                event.reference.update(resolved=ready, degraded=not ready)
            if not record_media:
                return event, 0
        if not record_media and not self._needs_attachment(event) and str(
                (event.reference or {}).get("content_type") or "").lower() != "share_card":
            self._mark_attachment_capability(event)
            return event, 0
        try:
            browser_content = str(event.reference.get("browser_content") or "").strip()
            browser_read = bool(browser_content) and str(
                event.reference.get("browser_status") or "").lower() in {"", "success", "direct_browser"}
            fetched_read = (event.reference.get("fetch_status") == "success"
                            and bool(str(event.reference.get("fetched_content") or "").strip()))
            if (event.reference.get("content_type") == "share_card"
                    and (str(event.reference.get("url") or "").strip()
                         or browser_read or fetched_read)):
                # 原生 URL 不依赖气泡是否可见。通道只提供链接与引用上下文，
                # 网页/文件内容由 Agent 的通用技能和工具读取。
                reference = event.reference
                if browser_read:
                    reference.update(fetch_status="direct_browser", fetch_source="direct_browser",
                                     resolved=True, degraded=False)
                    event.attachment_status = "materialized"
                elif fetched_read:
                    reference.update(resolved=True, degraded=False)
                    event.attachment_status = "materialized"
                elif not (reference.get("fetch_status") == "success"
                          and str(reference.get("fetched_content") or "").strip()):
                    reference.update(fetch_status="pending_tool", fetch_source="agent_tools",
                                     resolved=False, degraded=True)
                    event.attachment_status = "link_available"
                return event, 0
            target = self._binder.require_attachment_target(original.conversation_id, session_epoch=epoch)
            gateway = self._actions()
            window_identity = gateway.inspect_window_identity()
            binding_selector = gateway.inspect_attachment_binding(original.conversation_id)

            def require_attachment_window(*, validate_source=None):
                # 首次绑定已完成交互核验。滚动或辅助窗口打开后只读检查，
                # 再次 verify_target 会重选会话并重置当前消息列表位置。
                self._binder.require_attachment_target_passive(original.conversation_id,
                    session_epoch=epoch, authorized_target=target, window_identity=window_identity,
                    validate_source=validate_source)

            def require_source_target():
                # 按 UI 租约 -> 绑定 -> 来源的锁顺序，在同一生命周期段共享
                # validate_event 刚刷新的快照，继续完整检查所有身份和目标。
                require_attachment_window(validate_source=lambda: self.source.validate_event(original))

            def require_attachment_viewport():
                # 上滚是可逆动作。原子 UI 步骤只核验稳定身份；每页前后和
                # 附件捕获前另外进行完整来源/历史校验，拒绝变化后的结果。
                if epoch != self.source.session_epoch or gateway.inspect_window_identity() != window_identity:
                    raise SendNotSubmitted("attachment_target_changed")
                with self.source.reader_session() as reader:
                    if reader.account_id != original.account_id or reader.binding.pid != window_identity[1]:
                        raise SendNotSubmitted("attachment_target_changed")
                if gateway.inspect_attachment_binding(original.conversation_id) != binding_selector:
                    raise SendNotSubmitted("attachment_target_changed")
                if not conversation_header_matches_target(gateway.read_current_title(), target.display_name,
                                                           is_group=target.is_group):
                    raise SendNotSubmitted("attachment_target_changed")
                if epoch != self.source.session_epoch or gateway.inspect_window_identity() != window_identity:
                    raise SendNotSubmitted("attachment_target_changed")

            gateway.prepare_attachment_target(original.conversation_id, validate=require_source_target)
            with gateway.operation(reply=False):
                require_source_target()
                history = self.source.read_chat_history(original.conversation_id, 50, session_epoch=epoch)
                messages = gateway.read_attachment_snapshot(original.conversation_id)
                matched, messages, located_history, message_list_identity = self._locate_attachment(
                    original, history, messages, gateway, validate=require_source_target,
                    validate_viewport=require_attachment_viewport)
                history_signature = self._history_signature(history)

            def require_attachment_identity():
                require_source_target()
                refreshed = self.source.read_chat_history(original.conversation_id, 50, session_epoch=epoch)
                if self._history_signature(refreshed) != history_signature:
                    raise SendNotSubmitted("attachment_database_sequence_changed")
                if gateway.inspect_attachment_message_list(
                        original.conversation_id) != message_list_identity:
                    raise SendNotSubmitted("attachment_message_list_changed")
                current = gateway.read_attachment_snapshot(original.conversation_id,
                    **({"allow_non_bottom": True} if located_history else {}))
                if current != messages:
                    raise SendNotSubmitted("attachment_visible_sequence_changed")
                if self._correlate_attachment(original, refreshed, current,
                        allow_non_tail=located_history) != matched:
                    raise SendNotSubmitted("attachment_message_identity_changed")
                if not gateway.attachment_message_fully_visible(original.conversation_id, matched,
                        expected_list_identity=message_list_identity):
                    raise SendNotSubmitted("attachment_message_not_fully_visible")

            def require_attachment_context():
                require_source_target()

            if record_media:
                require_attachment_identity()
                payload = {**record, "source_message_id": event.reference.get("source_message_id", "")}
                with gateway.operation(reply=False):
                    evidence = gateway.client.fetch_referenced_chat_record(
                        matched, payload, tmp_root=Path(__file__).resolve().parent.parent / "tmp" / "wechat_records",
                        validate_target=require_attachment_context)
                require_attachment_context()
                record["media_evidence"] = {key: evidence[key] for key in
                    ("status", "strategy", "visible_text", "evidence_paths", "image_evidence_paths", "image_evidence", "page_count", "truncated", "media_status", "read_diagnostics")
                    if key in evidence}
                event.attachment_status = "materialized"
                return event, 1
            extra = {}
            if (event.reference.get("content_type") == "file"
                    and event.reference.get("source_native_message_id")):
                with self.source.reader_session() as reader:
                    file_snapshot = reader.snapshot_reference_file(original)

                def accept_saved_file(path):
                    # 原卡片的 Save As 返回实际路径之后才许可一次路径状态变更；
                    # 身份和正文仍由 reader 对保存前后的完整原生行逐字段复核。
                    require_attachment_window()
                    with self.source.reader_session() as current_reader:
                        accepted, reason = current_reader.accept_reference_file_materialization(
                            original, file_snapshot, path)
                    if not accepted:
                        raise SendNotSubmitted(reason)
                    event.reference["content_signature"] = original.reference["content_signature"]
                    require_attachment_context()
                    return True

                extra["on_file_materialized"] = accept_saved_file
            return gateway.materialize_event(
                event, target_message=matched,
                validate=require_attachment_identity, validate_target=require_attachment_context, **extra)
        except Exception as exc:
            validation = self.validate_reply_target(original)
            if not validation.valid:
                return self._source_invalid(event, SendNotSubmitted(validation.reason)), 0
            if record_media:
                # 媒体后备失败不丢失已经取得的数据库文字，也不交给模型重新查库。
                record["media_evidence"] = {"status": "unavailable", "evidence_paths": [],
                    "strategy": str(exc) if isinstance(exc, SendNotSubmitted) else "uia_chat_record_failed"}
                event.attachment_status = "materialized"
                return event, 0
            event.attachment_status = "attachment_identity_unavailable"
            if event.reference:
                event.reference.update(resolved=False, degraded=True,
                                       resolution_error=getattr(exc, "code", str(exc) if isinstance(exc, SendNotSubmitted)
                                                                else "attachment_binding_failed"))
            return event, 0

    @staticmethod
    def _source_invalid(event, exc):
        event.attachment_status = "source_invalid"
        if event.reference:
            event.reference.update(resolved=False, degraded=True, fetch_status="source_invalid",
                                   resolution_error=getattr(exc, "code", str(exc) if isinstance(exc, SendNotSubmitted)
                                                            else "source_validation_failed"))
        return event

    def search_contacts(self, query="", limit=20):
        return {"contacts": self.source.search_contacts(query, limit), "source": "wechat_database"}

    def list_group_conversations(self) -> list[ConversationTarget]:
        return self.source.list_group_conversations()

    def read_chat_history(self, conversation_id, limit=20):
        try:
            return self.source.read_chat_history(conversation_id, limit)
        except DatabaseReadError as exc:
            raise WechatHistoryReadError(exc.code, str(exc)) from exc

    def read_current_chat_history(self, limit=20):
        try:
            conversation_id, epoch = self._binder.current_conversation()
            return self.source.read_chat_history(conversation_id, limit, session_epoch=epoch)
        except DatabaseReadError as exc:
            raise WechatHistoryReadError(exc.code, str(exc)) from exc

    def close(self):
        with self._actions_lock:
            if self._closed:
                return
            self._closed = True
            if self._gateway is not None:
                self._gateway.close()
            self.source.close()

    def resume(self):
        with self._actions_lock:
            self.source.resume()
            if self._gateway is not None:
                self._gateway.resume()
            self._closed = False
