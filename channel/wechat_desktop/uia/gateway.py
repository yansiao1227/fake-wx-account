"""轻量微信 UIA 发送网关；不持有接收扫描、消息去重或 shell hook。"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Optional

from common.log import logger
from channel.wechat_desktop.contracts import ConversationTarget, SendResult, SendStatus
from channel.wechat_desktop.conversation import conversation_header_matches_target
from channel.wechat_desktop.models import ConversationInfo, HeaderInfo, OwnerInfo
from channel.wechat_desktop.send_control import (
    SendCancelled, SendNotSubmitted, check_send_allowed, extend_send_scope, send_target_scope,
)
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.operations import (
    WechatConversationSearchOperations,
    WechatConversationSelector,
    WechatSendOperations,
    resolve_conversation_selector,
)


class WechatUiaRecoveryError(RuntimeError):
    code = "uia_target_lookup_recovery_failed"


class _UiaPriorityCoordinator:
    """单次有界 UI 操作结束后优先让等待中的回复进入。"""

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._owner: Optional[int] = None
        self._depth = 0
        self._reply_waiters = 0

    @contextmanager
    def lease(self, *, reply: bool, check: Optional[Callable] = None):
        thread_id = threading.get_ident()
        registered_waiter = False
        with self._condition:
            if check is not None:
                check()
            if self._owner == thread_id:
                self._depth += 1
            else:
                if reply:
                    self._reply_waiters += 1
                    registered_waiter = True
                try:
                    while self._owner is not None or (
                        not reply and self._reply_waiters > 0
                    ):
                        if check is not None:
                            check()
                        if reply:
                            check_send_allowed()
                        self._condition.wait(timeout=0.05)
                    if reply:
                        check_send_allowed()
                    if check is not None:
                        check()
                    self._owner = thread_id
                    self._depth = 1
                finally:
                    if registered_waiter:
                        self._reply_waiters -= 1
                        self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                if self._owner != thread_id:
                    raise RuntimeError("UIA lease released by a non-owner thread")
                self._depth -= 1
                if self._depth == 0:
                    self._owner = None
                    self._condition.notify_all()


class WechatUiaGateway:
    """共享 UI 租约、绑定定位参数和发送操作的 UIA 网关。

    发送节拍在租约之外等待；只有客户端实际访问 UI 的有界段使用
    ``send_section``。每次发送独立保存自己的验证回调，避免并发串目标。
    """

    def __init__(
        self,
        config: dict,
        *,
        client=None,
        selector_resolver: Optional[Callable] = None,
        observation_reader: Optional[Callable] = None,
        priority=None,
        reply_pending=None,
    ):
        self.config = config
        self.client = client or WechatUiaClient(config)
        self.priority = priority or _UiaPriorityCoordinator()
        self.reply_pending = reply_pending or threading.Event()
        self._pending_lock = threading.RLock()
        self._pending_count = 0
        self._bindings_lock = threading.RLock()
        self._bindings: dict[str, ConversationInfo] = {}
        self._fallback_selector_resolver = selector_resolver
        self._selector_resolver = self._resolve_bound_selector
        self._send_local = threading.local()
        self._operation_local = threading.local()
        self._lifecycle_lock = threading.RLock()
        self._closed = False
        self._generation = 0
        self._materializer = None
        self.send_operations = WechatSendOperations(
            self.client,
            self._selector_resolver,
            self.send_section,
            observation_reader or self._observation,
        )

    @contextmanager
    def operation(self, *, reply: bool = True):
        generation = self._capture_generation(self._context_generation())
        previous = getattr(self._operation_local, "generation", None)
        if reply:
            with self._pending_lock:
                self._pending_count += 1
                self.reply_pending.set()
        try:
            check = lambda: self._capture_generation(generation)
            with self.priority.lease(reply=reply, check=check):
                check()
                self._operation_local.generation = generation
                try:
                    yield
                finally:
                    self._operation_local.generation = previous
        finally:
            if reply:
                with self._pending_lock:
                    self._pending_count -= 1
                    if not self._pending_count:
                        self.reply_pending.clear()

    def scan(self, operation, *args, **kwargs):
        with self.operation(reply=False):
            return operation(*args, **kwargs)

    @contextmanager
    def send_section(self):
        """在每个实际发送 UI 段中原子复核本次调用的账号和目标。"""
        with self.operation():
            validate = getattr(self._send_local, "validate", None)
            target = validate() if validate is not None else None
            self._check_open()
            with send_target_scope(target if isinstance(target, ConversationTarget) else None):
                yield

    @property
    def closed(self) -> bool:
        with self._lifecycle_lock:
            return self._closed

    def _check_open(self) -> None:
        self._capture_generation(self._context_generation())

    def _context_generation(self) -> Optional[int]:
        generation = getattr(self._send_local, "generation", None)
        return generation if generation is not None else getattr(self._operation_local, "generation", None)

    def _capture_generation(self, expected: Optional[int] = None) -> int:
        with self._lifecycle_lock:
            if self._closed:
                raise SendCancelled("WeChat UI Automation gateway is closed")
            if expected is not None and expected != self._generation:
                raise SendCancelled("WeChat UI Automation gateway lifecycle changed")
            return self._generation

    @contextmanager
    def _validation(self, validate):
        generation = self._capture_generation(self._context_generation())
        previous = getattr(self._send_local, "validate", None)
        previous_generation = getattr(self._send_local, "generation", None)
        self._send_local.validate = validate
        self._send_local.generation = generation
        try:
            with extend_send_scope(lambda: self._capture_generation(generation)):
                yield
        finally:
            self._send_local.validate = previous
            self._send_local.generation = previous_generation

    def inspect_account(self) -> tuple[int, OwnerInfo]:
        with self.operation():
            pid = int(self.client.get_owner_window_process_id())
            # 账号核验必须只读；不能为预检查点击头像、打开资料或激活窗口。
            info = self.client.get_owner_info_passive()
            if pid <= 0 or pid != int(self.client.get_owner_window_process_id()):
                raise RuntimeError("WeChat account window changed during identity inspection")
            return pid, info

    def list_conversations(self) -> list[ConversationInfo]:
        return self.scan(self.client.get_visible_conversations)

    def ensure_conversation_visible(self, display_name: str, *, is_group: bool, expected_pid: int) -> bool:
        """仅供数据库唯一身份通过后补开会话；结果仍需重新绑定验证。"""
        with self.operation(reply=False):
            if not callable(getattr(self.client, "_uia_root", None)):
                return False
            return WechatConversationSearchOperations(self.client).ensure_conversation_visible(
                display_name, is_group=is_group, expected_pid=expected_pid)

    def prepare_target_lookup(self) -> None:
        """可信目标的实际操作阶段才允许激活并恢复空 UIA 树。"""
        with self.operation():
            try:
                self.client.focus_window()
            except SendCancelled:
                raise
            except Exception as exc:
                logger.warning("[WechatDesktop] UIA target lookup recovery failed: %s", exc)
                raise WechatUiaRecoveryError("微信 UIA 会话控件恢复失败") from exc

    def read_current_title(self) -> HeaderInfo:
        with self.operation():
            return self.client.get_title()

    def verify_target(self, target: ConversationTarget, row: ConversationInfo) -> None:
        """按稳定行定位后复核真实会话类型，不能把群名后缀误认成私聊。"""
        with self.operation():
            if target.is_group is None:
                raise SendNotSubmitted("conversation type could not be verified")
            self.client.focus_window()
            if not self.client.locate_conversation(row.conversation_title, row.runtime_id, row.row_index):
                raise SendNotSubmitted("conversation is no longer visible")
            header = self.client.get_title()
            if not conversation_header_matches_target(header, target.display_name, is_group=target.is_group):
                raise SendNotSubmitted("conversation header or type does not match database target")

    def inspect_window_identity(self) -> tuple[int, int]:
        """窗口句柄和进程身份只读检查，不恢复或激活微信。"""
        with self.operation(reply=False):
            return (int(self.client.get_owner_window_handle()),
                    int(self.client.get_owner_window_process_id()))

    def prepare_attachment_target(self, conversation_id: str, *, validate) -> None:
        """仅在确有附件待物化且数据库身份已核验后定位会话。"""
        with self.operation(reply=False):
            validate()
            selector = self._resolve_bound_selector(conversation_id)
            if not selector.title or not selector.runtime_id:
                raise SendNotSubmitted("attachment_target_unavailable")
            # 返回值表示本次是否执行激活；已经在前台会返回 False。
            # 激活失败由客户端抛异常，不能把无需激活误当作失败。
            self.client.ensure_foreground_window()
            validate()
            if not self.client.locate_conversation(
                    selector.title, runtime_id=selector.runtime_id, row_index=selector.row_index):
                raise SendNotSubmitted("attachment_target_changed")
            validate()
            scroll = getattr(self.client, "scroll_chat_to_latest_for_attachment", None)
            if callable(scroll):
                scroll(validate=validate)
                validate()

    def read_attachment_snapshot(self, conversation_id: str, *, allow_non_bottom: bool = False):
        """附件关联专用气泡序列；未知滚动位置由数据库唯一尾部序列证明。

        Qt 版本可能不暴露 ScrollPattern。调用方必须继续执行原生序列关联，
        不能仅凭本快照认定在底部。有限历史定位可显式读取非底部；
        调用方仍须证明唯一原生连续序列，读取期间位置改变始终拒绝。
        """
        with self.operation(reply=False):
            selector = self._resolve_bound_selector(conversation_id)
            if not selector.title or not selector.runtime_id:
                raise SendNotSubmitted("attachment_target_unavailable")
            header = self.client.get_title()
            from channel.wechat_desktop.conversation import conversation_titles_match
            if not conversation_titles_match(header.title, selector.title):
                raise SendNotSubmitted("attachment_target_changed")
            before = self.client.get_chat_scroll_position_passive()
            messages = self.client.get_send_bubble_snapshot(selector.title, limit=20)
            after = self.client.get_chat_scroll_position_passive()
            if before != after or (not allow_non_bottom and (before is False or after is False)):
                raise SendNotSubmitted("attachment_bottom_unverified")
            if allow_non_bottom:
                # 非底部的布尔 ScrollPattern 状态可能一直为 False；复读完整
                # 控件快照，以免把读取中发生的另一轮滚动误当作稳定历史位置。
                confirmed = self.client.get_send_bubble_snapshot(selector.title, limit=20)
                if (tuple(confirmed) != tuple(messages) or
                        self.client.get_chat_scroll_position_passive() != after):
                    raise SendNotSubmitted("attachment_visible_sequence_changed")
            return tuple(messages)

    def inspect_attachment_message_list(self, conversation_id: str) -> str:
        """不导航会话，只读取此次附件定位所用列表的稳定控件身份。"""
        with self.operation(reply=False):
            selector = self._resolve_bound_selector(conversation_id)
            from channel.wechat_desktop.conversation import conversation_titles_match
            if (not selector.title or not selector.runtime_id or
                    not conversation_titles_match(self.client.get_title().title, selector.title)):
                raise SendNotSubmitted("attachment_target_changed")
            inspect = getattr(self.client, "get_attachment_message_list_identity", None)
            identity = inspect() if callable(inspect) else ""
            if not identity:
                raise SendNotSubmitted("attachment_message_list_unavailable")
            return identity

    def inspect_attachment_binding(self, conversation_id: str) -> tuple[str, str]:
        """只读已绑定选择器，禁止经后备解析器创建或替换附件绑定。"""
        with self.operation(reply=False), self._bindings_lock:
            row = self._bindings.get(str(conversation_id or "").strip())
            if row is None or not row.runtime_id or not row.conversation_title:
                raise SendNotSubmitted("attachment_binding_unavailable")
            return row.runtime_id, row.conversation_title

    def attachment_message_fully_visible(self, conversation_id: str, message, *, expected_list_identity: str) -> bool:
        """绑定列表中的完整可见目标才许可进入附件点击或菜单操作。"""
        with self.operation(reply=False):
            if self.inspect_attachment_message_list(conversation_id) != expected_list_identity:
                raise SendNotSubmitted("attachment_message_list_changed")
            inspect = getattr(self.client, "is_attachment_message_fully_visible", None)
            if not callable(inspect):
                raise SendNotSubmitted("attachment_viewport_unavailable")
            return bool(inspect(message, expected_list_identity=expected_list_identity))

    def scroll_attachment_history(self, conversation_id: str, *, expected_list_identity: str, validate) -> None:
        """对已验证的同一列表上滚一小步，不点击会话或输入框。"""
        with self.operation(reply=False):
            validate()
            if self.inspect_attachment_message_list(conversation_id) != expected_list_identity:
                raise SendNotSubmitted("attachment_message_list_changed")
            scroll = getattr(self.client, "scroll_chat_history_for_attachment", None)
            if not callable(scroll):
                raise SendNotSubmitted("attachment_history_scroll_unavailable")
            scroll(expected_list_identity=expected_list_identity, validate=validate)
            validate()
            if self.inspect_attachment_message_list(conversation_id) != expected_list_identity:
                raise SendNotSubmitted("attachment_message_list_changed")

    def materialize_event(self, event, *, target_message, validate=None, validate_target=None,
                          on_file_materialized=None):
        if self._materializer is None:
            from channel.wechat_desktop.uia.materializer import WechatUiaMaterializer
            self._materializer = WechatUiaMaterializer(self.config, gateway=self)
        return self._materializer.materialize_event(
            event, target_message=target_message,
            validate=validate, validate_target=validate_target,
            **({"on_file_materialized": on_file_materialized} if on_file_materialized else {}))

    def bind_target(self, conversation_id: str, row: ConversationInfo) -> None:
        identity = str(conversation_id or "").strip()
        if not identity or not str(row.conversation_title or "").strip():
            raise ValueError("conversation binding requires an identity and title")
        with self._lifecycle_lock:
            self._check_open()
            with self._bindings_lock:
                self._bindings[identity] = row

    def _resolve_bound_selector(self, conversation_id: str) -> WechatConversationSelector:
        """显式绑定优先；可注入解析器用于独立界面操作。"""
        with self._bindings_lock:
            row = self._bindings.get(str(conversation_id or ""))
        if row is not None:
            return resolve_conversation_selector({conversation_id: row}, conversation_id)
        if self._fallback_selector_resolver is not None:
            return self._fallback_selector_resolver(conversation_id)
        return WechatConversationSelector("")

    def _target_available(self, conversation_id: str) -> bool:
        return bool(self._selector_resolver(conversation_id).title.strip())

    @staticmethod
    def _observation() -> dict:
        return {"automation_backend": "uia", "uia_available": True}

    def send_text(self, conversation: str, text: str, *, validate=None) -> SendResult:
        with self._validation(validate):
            if not self._target_available(conversation):
                return SendResult(SendStatus.NOT_SENT, "conversation binding is unavailable")
            return self.send_operations.send_text(conversation, text)

    def send_interim_text(self, conversation: str, text: str, *, validate=None) -> SendResult:
        with self._validation(validate):
            if not self._target_available(conversation):
                return SendResult(SendStatus.NOT_SENT, "conversation binding is unavailable")
            return self.send_operations.send_text(conversation, text, expedited=True)

    def send_image(self, conversation: str, image_path: str, *, validate=None) -> SendResult:
        with self._validation(validate):
            if not self._target_available(conversation):
                return SendResult(SendStatus.NOT_SENT, "conversation binding is unavailable")
            return self.send_operations.send_image(conversation, image_path)

    def ensure_foreground(self) -> bool:
        return bool(self.scan(self.client.ensure_foreground_window))

    def close(self) -> None:
        """先取消等待；关闭过程不等待正在执行的 UI 租约。"""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            cancel = getattr(self.client, "cancel_waits", None)
            if callable(cancel):
                cancel()
            if self._materializer is not None:
                self._materializer.close()
            with self._bindings_lock:
                self._bindings.clear()

    def resume(self) -> None:
        """仅在通道明确启动时恢复，扫描自身不能撤销关闭。"""
        with self._lifecycle_lock:
            resume = getattr(self.client, "resume_waits", None)
            if callable(resume):
                resume()
            self._closed = False
