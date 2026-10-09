"""轻量微信 UIA 发送网关；不持有接收扫描、消息去重或 shell hook。"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Optional

from channel.wechat_desktop.contracts import SendResult, SendStatus
from channel.wechat_desktop.models import ConversationInfo, HeaderInfo, OwnerInfo
from channel.wechat_desktop.send_control import SendCancelled, check_send_allowed, extend_send_scope
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.operations import (
    WechatConversationSelector,
    WechatSendOperations,
    resolve_conversation_selector,
)


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
            if validate is not None:
                validate()
            self._check_open()
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
            info = self.client.get_owner_info()
            if pid <= 0 or pid != int(self.client.get_owner_window_process_id()):
                raise RuntimeError("WeChat account window changed during identity inspection")
            return pid, info

    def list_conversations(self) -> list[ConversationInfo]:
        return self.scan(self.client.get_visible_conversations)

    def read_current_title(self) -> HeaderInfo:
        with self.operation():
            return self.client.get_title()

    def bind_target(self, conversation_id: str, row: ConversationInfo) -> None:
        identity = str(conversation_id or "").strip()
        if not identity or not str(row.conversation_title or "").strip():
            raise ValueError("conversation binding requires an identity and title")
        with self._lifecycle_lock:
            self._check_open()
            with self._bindings_lock:
                self._bindings[identity] = row

    def _resolve_bound_selector(self, conversation_id: str) -> WechatConversationSelector:
        """显式绑定优先；默认 UIA Driver 的动态缓存只作为回退。"""
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
            with self._bindings_lock:
                self._bindings.clear()

    def resume(self) -> None:
        """仅在通道明确启动时恢复，扫描自身不能撤销关闭。"""
        with self._lifecycle_lock:
            resume = getattr(self.client, "resume_waits", None)
            if callable(resume):
                resume()
            self._closed = False
