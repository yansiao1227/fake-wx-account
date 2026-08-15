"""Event driver for the WeChat 4.1.9.30 UIA client."""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from common.log import file_logger, logger
from channel.wechat_desktop.models import (
    ReplyTargetValidation,
    UNKNOWN_SENDER_NAME,
    UiaChatMessage,
    WechatDesktopEvent,
)
from channel.wechat_desktop.backend import WechatDesktopBackend
from channel.wechat_desktop.operations import (
    WechatConversationSelector,
    WechatSendOperations,
    resolve_conversation_selector,
)
from channel.wechat_desktop.shell_hook import WindowsShellHook
from channel.wechat_desktop.uia_client import WechatUiaClient


class _UiaPriorityCoordinator:
    """Let one bounded UIA operation finish, then prefer waiting replies."""

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._owner: Optional[int] = None
        self._depth = 0
        self._reply_waiters = 0

    @contextmanager
    def lease(self, *, reply: bool):
        thread_id = threading.get_ident()
        registered_waiter = False
        with self._condition:
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
                        self._condition.wait()
                    self._owner = thread_id
                    self._depth = 1
                finally:
                    if registered_waiter:
                        self._reply_waiters -= 1
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


class WechatUiaDriver(WechatDesktopBackend):
    """微信 UIA 后端：负责事件转换、状态缓存和操作优先级协调。"""

    def __init__(
        self,
        config: dict,
        client: Optional[WechatUiaClient] = None,
        shell_hook: Optional[WindowsShellHook] = None,
        web_fetcher=None,
        knowledge_extractor=None,
    ):
        self.config = config
        self.client = client or WechatUiaClient(config)
        self._web_fetcher = web_fetcher
        self._knowledge_extractor = knowledge_extractor
        # Driver state is serialized independently from the UIA client's
        # operation lock. Client methods therefore release UIA between bounded
        # reads instead of one scan monopolizing it end-to-end.
        self._operation_lock = threading.RLock()
        self._scan_loop_lock = threading.RLock()
        self._hook = shell_hook or WindowsShellHook(
            self.client.allowed_process_ids,
            debounce_ms=int(config.get("shell_hook_debounce_ms", 250)),
        )
        self._hook_started = False
        self._last_hook_at = 0.0
        self._rows = {}
        self._conversation_selectors = {}
        self._emitted_targets: dict[str, str] = {}
        self._message_snapshots: dict[str, list[UiaChatMessage]] = {}
        self._known_groups = {str(x) for x in config.get("auto_reply_groups", [])}
        self._known_group_keys: set[str] = set()
        self._reply_in_flight = threading.Event()
        self._reply_conversation = ""
        self._reply_conversation_id = ""
        self._last_reply_monitor_at = 0.0
        self._interim_texts: dict[str, set[str]] = {}
        self._attachment_cache_lock = threading.RLock()
        self._attachment_path_cache: OrderedDict[
            tuple[str, str, str, str, str], str
        ] = OrderedDict()
        self._file_path_cache_by_name: OrderedDict[
            tuple[str, str], str
        ] = OrderedDict()
        self._reply_ui_pending = threading.Event()
        self._uia_priority = _UiaPriorityCoordinator()
        self._owner_name = ""
        self._owner_source = "unknown"
        self._scan_focus_retry_after = 0.0
        self._send_operations = WechatSendOperations(
            self.client,
            self._resolve_selector,
            self._reply_uia,
            lambda: self._observation(read_owner=False),
        )

    def preload_group_sender_ocr(self) -> bool:
        """Eagerly load RapidOCR models used for group sender resolution."""
        preload = getattr(self.client, "preload_group_sender_ocr", None)
        if not callable(preload):
            return False
        return bool(preload())

    def _scan_uia(self, operation, *args, **kwargs):
        with self._uia_priority.lease(reply=False):
            return operation(*args, **kwargs)

    @contextmanager
    def _reply_uia(self):
        self._reply_ui_pending.set()
        try:
            with self._uia_priority.lease(reply=True):
                yield
        finally:
            self._reply_ui_pending.clear()

    def _trace(self, stage: str, message: str, *args) -> None:
        """Session-scan diagnostics: file only (run.log), never the console."""
        if bool(self.config.get("diagnostic_logging", False)):
            file_logger.info(
                "[WechatDesktop][trace:%s] " + message,
                stage,
                *args,
            )

    @property
    def reply_in_flight(self) -> bool:
        return self._reply_in_flight.is_set()

    def begin_reply_cycle(self, conversation: str, conversation_id: str = ""):
        with self._operation_lock:
            self._reply_conversation = str(conversation or "")
            self._reply_conversation_id = str(conversation_id or "")
            self._last_reply_monitor_at = 0.0
            self._reply_in_flight.set()

    def end_reply_cycle(self):
        with self._operation_lock:
            conversation_id = self._reply_conversation_id
            self._reply_conversation = ""
            self._reply_conversation_id = ""
            self._last_reply_monitor_at = 0.0
            self._reply_in_flight.clear()
            if conversation_id:
                self._interim_texts.pop(conversation_id, None)

    def register_interim_text(self, conversation_id: str, text: str):
        """Keep an Agent progress notice from becoming a new reply target."""
        conversation_id = str(conversation_id or "")
        text = str(text or "").strip()
        if conversation_id and text:
            with self._operation_lock:
                self._interim_texts.setdefault(conversation_id, set()).add(text)

    def forget_interim_text(self, conversation_id: str, text: str):
        with self._operation_lock:
            texts = self._interim_texts.get(str(conversation_id or ""))
            if not texts:
                return
            texts.discard(str(text or "").strip())
            if not texts:
                self._interim_texts.pop(str(conversation_id or ""), None)

    def _is_interim_message(
        self, conversation_id: str, message: UiaChatMessage
    ) -> bool:
        with self._operation_lock:
            return str(message.content or "").strip() in self._interim_texts.get(
                str(conversation_id or ""), set()
            )

    def _is_reply_conversation(self, row_key: str, row) -> bool:
        with self._operation_lock:
            from channel.wechat_desktop.operations import conversation_titles_match

            return self.reply_in_flight and (
                row_key == self._reply_conversation_id
                or conversation_titles_match(
                    row.conversation_title, self._reply_conversation
                )
            )

    def start(self) -> bool:
        resume_waits = getattr(self.client, "resume_waits", None)
        if resume_waits:
            resume_waits()
        if not self._hook_started:
            self._hook_started = self._hook.start()
        return self._hook_started

    def ensure_foreground(self) -> bool:
        return self.client.ensure_foreground_window()

    def close(self):
        self._hook.close()
        self._hook_started = False
        cancel_waits = getattr(self.client, "cancel_waits", None)
        if cancel_waits:
            cancel_waits()

    def _settle_hook_signal(self, stop_event: threading.Event) -> bool:
        minimum = max(0, int(self.config.get("uia_hook_settle_ms_min", 300)))
        maximum = max(minimum, int(self.config.get("uia_hook_settle_ms_max", 800)))
        delay = random.randint(minimum, maximum) if maximum > minimum else minimum
        return bool(delay and stop_event.wait(delay / 1000.0))

    def wait_for_changes(self, stop_event: threading.Event) -> str:
        """Wait for a flash or the low-frequency reconciliation deadline."""
        self.start()
        reconcile_enabled = bool(
            self.config.get("shell_hook_reconcile_enabled", False)
        )
        timeout = max(1.0, float(self.config.get("shell_hook_reconcile_seconds", 15)))
        deadline = time.monotonic() + timeout if reconcile_enabled else None
        while not stop_event.is_set():
            now = time.monotonic()
            if self.reply_in_flight:
                monitor_interval = max(
                    0.25,
                    min(
                        float(
                            self.config.get(
                                "reply_monitor_interval_seconds", 1.0
                            )
                        ),
                        5.0,
                    ),
                )
                if now - self._last_reply_monitor_at >= monitor_interval:
                    self._last_reply_monitor_at = now
                    return "reply-monitor"
            remaining = deadline - time.monotonic() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                return "reconcile"
            hook_wait = 0.25 if remaining is None else min(0.25, remaining)
            if self._hook.wait(hook_wait):
                signal = self._hook.consume(self._last_hook_at)
                if signal.signaled:
                    self._last_hook_at = signal.created_at
                    if self._settle_hook_signal(stop_event):
                        return "stopped"
                    return signal.reason
                if self._hook.error and reconcile_enabled:
                    return "reconcile"
        return "stopped"

    def _observation(self, *, read_owner: bool = True, **updates) -> dict:
        if read_owner:
            try:
                info = self._scan_uia(self.client.get_owner_info)
                self._owner_name = info.nick_name
                self._owner_source = info.source
            except Exception:
                pass
        result = {
            "error": "",
            "automation_backend": "uia",
            "uia_available": True,
            "shell_hook_active": self._hook.available,
            "shell_hook_error": self._hook.error,
            "owner_name": self._owner_name,
            "owner_source": self._owner_source,
            "message_source": "uia_visible_bubbles",
            "message_limit": self._message_limit(),
            "reply_in_flight": self.reply_in_flight,
            "reply_conversation": self._reply_conversation,
        }
        result.update(updates)
        return result

    def observe(self, focus: bool = False) -> dict:
        try:
            if focus:
                self.client.focus_window()
            return self._observation(
                process_id=self.client.get_owner_window_process_id(),
                node_count=self.client.probe_tree(),
            )
        except Exception as exc:
            return self._observation(error=f"WeChat UI Automation unavailable: {exc}", uia_available=False)

    def _message_limit(self) -> int:
        # Zero asks the UIA client for every message node currently visible.
        return 0

    @staticmethod
    def _row_key(row) -> str:
        if row.runtime_id:
            return f"uia-session:{row.runtime_id}"
        stable = f"{row.automation_id}\0{row.row_index}"
        return "uia-session:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()

    @classmethod
    def _target_key(cls, message: UiaChatMessage, index: int) -> str:
        # Bounds and visible indexes drift as the chat scrolls, while runtime
        # IDs belong to recyclable controls.  ``stable_id`` is reconciled
        # against the preceding visible snapshot by the driver.
        stable = repr(
            (
                message.message_type,
                message.content,
                message.stable_id or "",
            )
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    @staticmethod
    def _message_signature(message: UiaChatMessage) -> tuple[str, str]:
        return message.message_type, str(message.content or "")

    @staticmethod
    def _content_signature(message: UiaChatMessage) -> str:
        reference = message.reference
        payload = {
            "type": str(message.message_type or ""),
            "content": str(message.content or ""),
            "reference": (
                {
                    "sender": str(reference.sender_name or ""),
                    "type": str(reference.message_type or ""),
                    "content": str(reference.content or ""),
                }
                if reference is not None
                else None
            ),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def _stabilize_messages(
        self,
        conversation_id: str,
        messages: list[UiaChatMessage],
    ) -> list[UiaChatMessage]:
        with self._operation_lock:
            return self._stabilize_messages_locked(conversation_id, messages)

    def _stabilize_messages_locked(
        self,
        conversation_id: str,
        messages: list[UiaChatMessage],
    ) -> list[UiaChatMessage]:
        """Carry message identities across scrolling UIA snapshots."""
        previous = list(self._message_snapshots.get(conversation_id, []))
        stable_ids = ["" for _ in messages]
        matched_previous_indexes = [None for _ in messages]
        used_previous: set[int] = set()

        runtime_matches: dict[tuple[str, tuple[str, str]], list[int]] = {}
        for previous_index, message in enumerate(previous):
            if not message.runtime_id or not message.stable_id:
                continue
            key = (message.runtime_id, self._message_signature(message))
            runtime_matches.setdefault(key, []).append(previous_index)
        for current_index, message in enumerate(messages):
            if not message.runtime_id:
                continue
            key = (message.runtime_id, self._message_signature(message))
            candidates = runtime_matches.get(key, [])
            previous_index = next(
                (index for index in candidates if index not in used_previous),
                None,
            )
            if previous_index is not None:
                stable_ids[current_index] = previous[previous_index].stable_id
                matched_previous_indexes[current_index] = previous_index
                used_previous.add(previous_index)

        # Runtime IDs may change when WeChat recreates controls. Match the
        # remaining ordered overlap by payload without using absolute indexes.
        search_start = 0
        for current_index, message in enumerate(messages):
            if stable_ids[current_index]:
                matched_previous = next(
                    (
                        index
                        for index, old in enumerate(previous)
                        if old.stable_id == stable_ids[current_index]
                    ),
                    None,
                )
                if matched_previous is not None:
                    search_start = max(search_start, matched_previous + 1)
                continue
            signature = self._message_signature(message)
            previous_index = next(
                (
                    index
                    for index in range(search_start, len(previous))
                    if index not in used_previous
                    and self._message_signature(previous[index]) == signature
                    and previous[index].stable_id
                ),
                None,
            )
            if previous_index is not None:
                stable_ids[current_index] = previous[previous_index].stable_id
                matched_previous_indexes[current_index] = previous_index
                used_previous.add(previous_index)
                search_start = previous_index + 1

        stabilized = []
        for index, message in enumerate(messages):
            previous_index = matched_previous_indexes[index]
            previous_sender = (
                previous[previous_index].sender_name
                if previous_index is not None
                else ""
            )
            current_sender = message.sender_name
            if current_sender in {"", UNKNOWN_SENDER_NAME} and previous_sender not in {
                "",
                UNKNOWN_SENDER_NAME,
            }:
                current_sender = previous_sender
            stabilized.append(
                replace(
                    message,
                    stable_id=stable_ids[index] or uuid.uuid4().hex,
                    sender_name=current_sender or UNKNOWN_SENDER_NAME,
                )
            )
        self._message_snapshots[conversation_id] = stabilized
        return stabilized

    def _select_reply_target(
        self,
        messages: list[UiaChatMessage],
        is_group: bool,
        owner: str,
        conversation_id: str = "",
        conversation_name: str = "",
    ) -> tuple[int, Optional[UiaChatMessage]]:
        """Return the newest visible message that should trigger a reply.

        Skips, in order:
        1. Interim tool-notice bubbles registered by this channel.
        2. Messages known to be our own outgoing text (verified or
           sent-but-unverified, both registered by send_message).
        3. For private chats: messages whose OCR direction is unambiguously
           ``outgoing`` — a backstop that catches bubbles that appeared after
           an unverified send and were not yet registered in the outgoing
           cache (e.g. because the channel restarted between send and scan).
           ``direction == "unknown"`` is kept so that OCR failures never
           silently suppress real incoming messages.
        4. For group chats: messages that do not mention the bot owner.
        """
        if not messages:
            return -1, None
        is_known_outgoing = getattr(self.client, "is_known_outgoing_message", None)
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if self._is_interim_message(conversation_id, message):
                continue
            if is_known_outgoing and is_known_outgoing(conversation_name, message):
                continue
            # Backstop for private chats: skip bubbles that OCR confidently
            # determined are outgoing even when the cache has no record of them.
            # direction=="unknown" is intentionally not skipped — an OCR miss
            # must not suppress a real incoming message.
            if not is_group and message.direction == "outgoing":
                continue
            if not is_group or self._mentions_owner(message.content, owner):
                return index, message
        return -1, None

    def _exclude_private_outgoing_messages(
        self,
        messages: list[UiaChatMessage],
        conversation_name: str,
        is_group: bool,
    ) -> list[UiaChatMessage]:
        """Remove verified self-sent bubbles before private-chat processing."""
        if is_group:
            return messages
        checker = getattr(self.client, "is_known_outgoing_message", None)
        if checker is None:
            return messages
        return [
            message
            for message in messages
            if not checker(conversation_name, message)
        ]

    def _history_snapshot(
        self,
        messages: list[UiaChatMessage],
        target_index: int,
        is_group: bool,
        conversation_id: str = "",
    ) -> list[dict]:
        target = messages[target_index] if 0 <= target_index < len(messages) else None
        if target is not None and target.reference is not None:
            reference = target.reference
            content = reference.content
            if reference.file_path:
                marker = "文件" if reference.message_type == "file" else "图片"
                content = f"[{marker}: {reference.file_path}]"
            return [
                {
                    "sender_name": reference.sender_name if is_group else "",
                    "content": content,
                    "content_type": reference.message_type,
                    "is_reference": True,
                    "resolved": reference.resolved,
                    "degraded": reference.degraded,
                    "strategy": reference.strategy,
                }
            ]
        start = 0
        end = len(messages)
        if target is not None and target.message_type in {"file", "image"}:
            # Attachment events never absorb following messages. They are
            # cached independently while later text becomes its own event.
            end = target_index
        context_messages = [
            (index, message)
            for index, message in enumerate(messages)
            if start <= index < end and index != target_index
            and not self._is_interim_message(conversation_id, message)
        ]
        history = []
        for message_index, message in context_messages:
            content = message.content
            if message.message_type == "file" and message.file_path:
                content = f"[文件: {message.file_path}]"
            elif message.message_type == "image" and message.file_path:
                content = f"[图片: {message.file_path}]"
            history.append(
                {
                    "sender_name": message.sender_name if is_group else "",
                    "content": content,
                    "content_type": message.message_type,
                    "_message_runtime_id": message.runtime_id,
                    "_message_stable_id": message.stable_id,
                    "_content_signature": self._content_signature(message),
                    "_message_index": message_index,
                }
            )
        return history

    def _resolve_message_files(
        self,
        messages: list[UiaChatMessage],
        prefer_image_viewer: bool = False,
    ) -> list[UiaChatMessage]:
        resolved_messages = []
        for visible_message in messages:
            if visible_message.message_type == "file":
                file_path = self.client.fetch_message_file(visible_message)
                if file_path:
                    visible_message = replace(visible_message, file_path=file_path)
            elif visible_message.message_type == "image":
                fetch_image = getattr(self.client, "fetch_message_image", None)
                image_path = ""
                if fetch_image:
                    try:
                        image_path = fetch_image(
                            visible_message,
                            prefer_viewer=prefer_image_viewer,
                        )
                    except TypeError:
                        image_path = fetch_image(visible_message)
                if image_path:
                    visible_message = replace(visible_message, file_path=image_path)
            resolved_messages.append(visible_message)
        return resolved_messages

    def _resolve_reply_target_message(
        self,
        conversation_id: str,
        message: UiaChatMessage,
    ) -> tuple[UiaChatMessage, int]:
        cache_key = (
            str(conversation_id or ""),
            str(message.message_type or ""),
            str(message.stable_id or ""),
            str(message.runtime_id or ""),
            self._content_signature(message),
        )
        with self._attachment_cache_lock:
            cached_path = self._attachment_path_cache.get(cache_key, "")
            if cached_path and os.path.isfile(cached_path):
                self._attachment_path_cache.move_to_end(cache_key)
                if message.reference is not None:
                    return (
                        replace(
                            message,
                            reference=replace(
                                message.reference,
                                file_path=cached_path,
                                resolved=True,
                                degraded=False,
                            ),
                        ),
                        0,
                    )
                return replace(message, file_path=cached_path), 0
            if cached_path:
                self._attachment_path_cache.pop(cache_key, None)

        if (
            message.reference is not None
            and message.reference.message_type == "file"
        ):
            cached_file = self._cached_file_by_name(
                conversation_id, message.reference.content
            )
            if cached_file:
                return (
                    replace(
                        message,
                        reference=replace(
                            message.reference,
                            file_path=cached_file,
                            resolved=True,
                            degraded=False,
                        ),
                    ),
                    0,
                )

        resolve_count = 0
        reference_type = (
            str(message.reference.message_type or "").lower()
            if message.reference is not None
            else ""
        )
        if reference_type == "image":
            fetch_referenced_image = getattr(
                self.client, "fetch_referenced_message_image", None
            )
            image_path = ""
            if fetch_referenced_image:
                resolve_count += 1
                image_path = fetch_referenced_image(message)
            message = replace(
                message,
                reference=replace(
                    message.reference,
                    file_path=image_path,
                    resolved=bool(image_path),
                    degraded=not bool(image_path),
                    strategy="wechat_reference_image_viewer",
                ),
            )
        elif message.reference is not None and bool(
            self.config.get("resolve_message_references", True)
        ):
            resolver = getattr(self.client, "resolve_message_reference", None)
            if resolver:
                resolve_count += 1
                message = resolver(message)
        elif message.reference is not None:
            message = replace(
                message,
                reference=replace(
                    message.reference,
                    resolved=False,
                    degraded=True,
                    strategy="uia_reference_preview_only",
                ),
            )

        if message.reference is None and message.message_type == "image":
            fetch_image = getattr(self.client, "fetch_message_image", None)
            image_path = ""
            if fetch_image:
                resolve_count += 1
                try:
                    image_path = fetch_image(message, prefer_viewer=True)
                except TypeError:
                    image_path = message.file_path or fetch_image(message)
            if image_path:
                message = replace(message, file_path=image_path)
        elif (
            message.reference is None
            and message.message_type == "file"
            and not message.file_path
        ):
            resolve_count += 1
            file_path = self.client.fetch_message_file(message)
            if file_path:
                message = replace(message, file_path=file_path)

        resolved_path = str(message.file_path or "")
        if message.reference is not None and message.reference.file_path:
            resolved_path = str(message.reference.file_path)
        if resolved_path and os.path.isfile(resolved_path):
            with self._attachment_cache_lock:
                self._attachment_path_cache[cache_key] = resolved_path
                self._attachment_path_cache.move_to_end(cache_key)
                while len(self._attachment_path_cache) > 256:
                    self._attachment_path_cache.popitem(last=False)
            resolved_type = (
                message.reference.message_type
                if message.reference is not None
                else message.message_type
            )
            if resolved_type == "file":
                reference_content = (
                    message.reference.content
                    if message.reference is not None
                    else message.content
                )
                self._remember_file_by_name(
                    conversation_id, reference_content, resolved_path
                )
        return message, resolve_count

    @staticmethod
    def _file_name_candidates(value: str) -> list[str]:
        candidates = []
        for raw in [str(value or ""), *str(value or "").splitlines()]:
            name = Path(raw.strip()).name.strip()
            if "." not in name or name.startswith("["):
                continue
            normalized = unicodedata.normalize("NFKC", name).casefold()
            if normalized and normalized not in candidates:
                candidates.append(normalized)
        return candidates

    def _remember_file_by_name(
        self, conversation_id: str, content: str, path: str
    ):
        names = self._file_name_candidates(content)
        path_name = unicodedata.normalize(
            "NFKC", Path(path).name
        ).casefold()
        if path_name and path_name not in names:
            names.append(path_name)
        with self._attachment_cache_lock:
            for name in names:
                key = (str(conversation_id or ""), name)
                self._file_path_cache_by_name[key] = path
                self._file_path_cache_by_name.move_to_end(key)
            while len(self._file_path_cache_by_name) > 256:
                self._file_path_cache_by_name.popitem(last=False)

    def _cached_file_by_name(
        self, conversation_id: str, content: str
    ) -> str:
        conversation_key = str(conversation_id or "")
        candidates = self._file_name_candidates(content)
        with self._attachment_cache_lock:
            for name in candidates:
                key = (conversation_key, name)
                path = self._file_path_cache_by_name.get(key, "")
                if path and os.path.isfile(path):
                    self._file_path_cache_by_name.move_to_end(key)
                    return path
                if path:
                    self._file_path_cache_by_name.pop(key, None)

            # 微信引用预览与本地重复下载文件可能出现 ``报告.pdf`` /
            # ``报告(1).pdf`` 之类差异。精确匹配失败后，仅在同扩展名下做
            # stem 前缀匹配，避免把不同文档类型串到一起。
            cached_items = list(reversed(self._file_path_cache_by_name.items()))
            for candidate in candidates:
                candidate_path = Path(candidate)
                for key, path in cached_items:
                    cached_conversation, cached_name = key
                    cached_path = Path(cached_name)
                    if (
                        cached_conversation != conversation_key
                        or cached_path.suffix != candidate_path.suffix
                        or not cached_path.stem.startswith(candidate_path.stem)
                    ):
                        continue
                    if path and os.path.isfile(path):
                        self._file_path_cache_by_name.move_to_end(key)
                        return path
                    if path:
                        self._file_path_cache_by_name.pop(key, None)
        return ""

    @staticmethod
    def _mentions_owner(content: str, owner: str) -> bool:
        if not owner:
            return False
        value = unicodedata.normalize("NFKC", str(content or ""))
        target = unicodedata.normalize("NFKC", str(owner)).strip()
        compact = value.replace("\u2005", " ").replace("\u00a0", " ")
        return f"@{target}" in compact

    def _event(
        self,
        conversation_id: str,
        conversation: str,
        message: UiaChatMessage,
        history: list[dict],
        is_group: bool,
        is_at: bool,
        ordinal: int,
        target_key: str,
        session_unread_count: int = 0,
    ) -> Optional[WechatDesktopEvent]:
        event_id = uuid.uuid4().hex
        sender = message.sender_name or ("群成员" if is_group else conversation)
        event = WechatDesktopEvent(
            kind="message",
            conversation_id=conversation_id,
            conversation_name=conversation,
            sender_id=sender,
            sender_name=sender,
            content_type=message.message_type,
            content=message.file_path or message.content,
            is_group=is_group,
            is_at=is_at,
            source_type="group" if is_group else "private",
            evidence_path=(
                message.reference.file_path
                if message.reference is not None
                and message.reference.file_path
                else message.file_path
                if message.message_type == "image"
                else ""
            ),
            bounds=message.bounds,
            history=history,
            reference=(
                {
                    "sender_name": message.reference.sender_name,
                    "content": message.reference.content,
                    "content_type": message.reference.message_type,
                    "file_path": message.reference.file_path,
                    "resolved": message.reference.resolved,
                    "degraded": message.reference.degraded,
                    "strategy": message.reference.strategy,
                    "original_content": message.reference.original_content,
                    "url": message.reference.url,
                    "platform": message.reference.platform,
                    "fetched_content": message.reference.fetched_content,
                    "fetch_status": message.reference.fetch_status,
                    "knowledge_content": message.reference.knowledge_content,
                    "knowledge_status": message.reference.knowledge_status,
                    "depth": 1,
                }
                if message.reference is not None
                else {}
            ),
            target_key=target_key,
            message_runtime_id=message.runtime_id,
            message_stable_id=message.stable_id,
            content_signature=self._content_signature(message),
            session_unread_count=max(0, int(session_unread_count)),
            observed_at=time.time() + ordinal / 1_000_000.0,
            event_id=event_id,
        )
        setattr(event, "_fingerprint_content", target_key)
        return event

    def observe_events(self) -> tuple[dict, list[WechatDesktopEvent]]:
        # Only serialize concurrent scans here. Shared driver state uses
        # ``_operation_lock`` in short sections; never hold that state lock
        # while waiting for a scan-priority UIA lease.
        with self._scan_loop_lock:
            try:
                if self._reply_ui_pending.is_set():
                    return self._observation(
                        read_owner=False,
                        scan_yielded_to_reply=True,
                    ), []
                rows = self._scan_uia(self.client.get_visible_conversations)
                self._trace(
                    "01-sessions",
                    "visible=%s unread=%s mentioned=%s",
                    len(rows),
                    sum(row.not_read_number > 0 for row in rows),
                    sum(bool(row.mentions_self) for row in rows),
                )
                current_rows = {self._row_key(row): row for row in rows}
                with self._operation_lock:
                    self._conversation_selectors = dict(current_rows)
                    first_scan = not self._rows
                    previous_rows = self._rows
                    self._rows = current_rows
                process_startup_unread = bool(
                    self.config.get("process_startup_unread_messages", True)
                )
                if (
                    first_scan
                    and not bool(
                        self.config.get("bootstrap_existing_messages", False)
                    )
                    and not process_startup_unread
                ):
                    self._trace(
                        "02-baseline",
                        "existing sessions recorded without reply candidates",
                    )
                    return self._observation(
                        visible_conversations=len(rows), unread_conversations=sum(row.not_read_number > 0 for row in rows)
                    ), []
                if first_scan and process_startup_unread:
                    self._trace(
                        "02-baseline",
                        "startup unread sessions eligible from session-list UI tree unread=%s",
                        sum(row.not_read_number > 0 for row in rows),
                    )

                candidates = []
                for row_key, row in current_rows.items():
                    previous = previous_rows.get(row_key)
                    changed = previous is None or previous.row_signature != row.row_signature
                    active_reply_conversation = self._is_reply_conversation(
                        row_key, row
                    )
                    # A private conversation is only a discovery candidate
                    # while its session row says it has unread messages. The
                    # message itself is read only after entering the chat.
                    if row.not_read_number > 0 or (
                        row.mentions_self and changed
                    ) or active_reply_conversation:
                        candidates.append((row_key, row))
                        self._trace(
                            "02-candidate",
                            "conversation=%s unread=%s mentioned=%s changed=%s reply_monitor=%s runtime=%s row=%s",
                            row.conversation_title,
                            row.not_read_number,
                            bool(row.mentions_self),
                            changed,
                            active_reply_conversation,
                            row.runtime_id or "<empty>",
                            row.row_index,
                        )
                discovery_candidates = [
                    (row_key, row)
                    for row_key, row in candidates
                    if not self._is_reply_conversation(row_key, row)
                ]
                discovery_focus_ready = True
                if discovery_candidates:
                    now = time.monotonic()
                    if now < self._scan_focus_retry_after:
                        discovery_focus_ready = False
                        self._trace(
                            "02-focus",
                            "foreground activation deferred retry_in_ms=%s",
                            int((self._scan_focus_retry_after - now) * 1000),
                        )
                    else:
                        try:
                            self._scan_uia(self.client.focus_window)
                            self._scan_focus_retry_after = 0.0
                        except Exception as exc:
                            discovery_focus_ready = False
                            self._scan_focus_retry_after = time.monotonic() + 2.0
                            self._trace(
                                "02-focus",
                                "foreground activation failed; backing off error=%s",
                                exc,
                            )

                events: list[WechatDesktopEvent] = []
                ordinal = 0
                for row_key, row in candidates:
                    if self._reply_ui_pending.is_set():
                        self._trace(
                            "02-yield",
                            "reply UI pending; remaining candidates deferred",
                        )
                        break
                    name = row.conversation_title
                    active_reply_conversation = self._is_reply_conversation(
                        row_key, row
                    )
                    if not active_reply_conversation and not discovery_focus_ready:
                        self._trace(
                            "02-skip",
                            "conversation=%s reason=foreground_activation_deferred",
                            name,
                        )
                        continue
                    with self._operation_lock:
                        is_group = (
                            row_key in self._known_group_keys
                            or name in self._known_groups
                        )
                    if (
                        is_group
                        and not row.mentions_self
                        and not active_reply_conversation
                    ):
                        self._trace(
                            "02-skip",
                            "conversation=%s reason=known_group_without_session_mention",
                            name,
                        )
                        continue
                    if active_reply_conversation:
                        from channel.wechat_desktop.operations import (
                            conversation_titles_match,
                        )

                        header = self._scan_uia(self.client.get_title)
                        if not conversation_titles_match(header.title, name):
                            self._trace(
                                "02-skip",
                                "conversation=%s reason=reply_monitor_not_active current=%s",
                                name,
                                header.title or "<empty>",
                            )
                            continue
                        is_group = header.header_type == "group"
                        self._trace(
                            "03-header",
                            "conversation=%s title=%s type=%s members=%s monitor=background",
                            name,
                            header.title,
                            header.header_type,
                            header.chat_number,
                        )
                    elif not is_group:
                        located = self._scan_uia(
                            self.client.locate_conversation,
                            name, row.runtime_id, row.row_index
                        )
                        self._trace(
                            "03-enter",
                            "conversation=%s located=%s runtime=%s row=%s",
                            name,
                            located,
                            row.runtime_id or "<empty>",
                            row.row_index,
                        )
                        if not located:
                            continue
                        header = self._scan_uia(self.client.get_title)
                        is_group = header.header_type == "group"
                        self._trace(
                            "03-header",
                            "conversation=%s title=%s type=%s members=%s",
                            name,
                            header.title,
                            header.header_type,
                            header.chat_number,
                        )
                        if is_group:
                            with self._operation_lock:
                                self._known_group_keys.add(row_key)
                            if (
                                not row.mentions_self
                                and not active_reply_conversation
                            ):
                                self._trace(
                                    "02-skip",
                                    "conversation=%s reason=group_without_session_mention",
                                    name,
                                )
                                continue

                    messages = self._scan_uia(
                        self.client.get_chat_history,
                        name,
                        self._message_limit(),
                        runtime_id=row.runtime_id,
                        row_index=row.row_index,
                        ensure_conversation=not active_reply_conversation,
                    )
                    messages = self._exclude_private_outgoing_messages(
                        messages, name, is_group
                    )
                    messages = self._stabilize_messages(row_key, messages)
                    self._trace(
                        "04-history",
                        "conversation=%s group=%s visible_messages=%s",
                        name,
                        is_group,
                        len(messages),
                    )
                    if active_reply_conversation:
                        with self._operation_lock:
                            owner = self._owner_name
                        owner_source = "driver-cache"
                    else:
                        owner_info = self._scan_uia(self.client.get_owner_info)
                        owner = owner_info.nick_name
                        owner_source = owner_info.source
                    self._trace(
                        "05-owner",
                        "conversation=%s available=%s source=%s name=%s",
                        name,
                        bool(owner),
                        owner_source,
                        owner or "<empty>",
                    )
                    target_index, message = self._select_reply_target(
                        messages, is_group, owner, row_key, name
                    )
                    if message is None:
                        reason = (
                            "owner_name_unavailable"
                            if is_group and not owner
                            else "no_visible_owner_mention"
                            if is_group
                            else "no_visible_message"
                        )
                        self._trace(
                            "06-target",
                            "conversation=%s selected=False reason=%s visible_messages=%s",
                            name,
                            reason,
                            len(messages),
                        )
                        with self._operation_lock:
                            self._emitted_targets.pop(row_key, None)
                        continue
                    self._trace(
                        "06-target",
                        "conversation=%s selected=True index=%s type=%s chars=%s",
                        name,
                        target_index,
                        message.message_type,
                        len(str(message.content or "")),
                    )
                    target_key = self._target_key(message, target_index)
                    with self._operation_lock:
                        already_emitted = (
                            self._emitted_targets.get(row_key) == target_key
                        )
                    if already_emitted:
                        continue
                    history = self._history_snapshot(
                        messages, target_index, is_group, row_key
                    )
                    ordinal += 1
                    event = self._event(
                        row_key,
                        name,
                        message,
                        history,
                        is_group,
                        bool(is_group),
                        ordinal,
                        target_key,
                        row.not_read_number,
                    )
                    if event is not None:
                        with self._operation_lock:
                            self._emitted_targets[row_key] = target_key
                        events.append(event)
                return self._observation(
                    visible_conversations=len(rows),
                    unread_conversations=sum(row.not_read_number > 0 for row in rows),
                ), events
            except Exception as exc:
                self._trace("06-error", "observation_failed error=%s", exc)
                return self._observation(
                    error=f"WeChat UI Automation unavailable: {exc}",
                    uia_available=False,
                ), []

    def _resolve_selector(self, conversation: str) -> WechatConversationSelector:
        """在状态锁内读取会话缓存，再交给纯函数生成定位参数。"""

        with self._operation_lock:
            selectors = dict(self._conversation_selectors)
        return resolve_conversation_selector(selectors, conversation)

    def _selector(self, conversation: str):
        """兼容旧的三元组调用；新代码应使用 ``_resolve_selector``。"""

        selector = self._resolve_selector(conversation)
        return selector.title, selector.runtime_id, selector.row_index

    def validate_reply_target(self, event: WechatDesktopEvent) -> ReplyTargetValidation:
        """Re-read the UI and return a replacement event when the target changed."""
        try:
            with self._reply_uia():
                with self._operation_lock:
                    return self._validate_reply_target_locked(event)
        except Exception as exc:
            return ReplyTargetValidation(False, str(exc))

    def _validate_reply_target_locked(
        self, event: WechatDesktopEvent
    ) -> ReplyTargetValidation:
        if event.conversation_id not in self._conversation_selectors:
            return ReplyTargetValidation(
                False, "conversation row is no longer visible"
            )
        if not event.is_group:
            # Strict private FIFO: a newer bubble must not invalidate the
            # already-running reply. The stored UIA session selector is enough
            # to verify the destination still exists.
            return ReplyTargetValidation(True)
        title, runtime_id, row_index = self._selector(event.conversation_id)
        messages = self.client.get_chat_history(
            title,
            self._message_limit(),
            runtime_id=runtime_id,
            row_index=row_index,
        )
        messages = self._exclude_private_outgoing_messages(
            messages, title, event.is_group
        )
        messages = self._stabilize_messages(event.conversation_id, messages)
        if event.is_group and any(
            self._target_key(message, message_index) == event.target_key
            for message_index, message in enumerate(messages)
        ):
            # A later @ message must not invalidate an earlier group reply that
            # is still queued in the active conversation's local FIFO.
            return ReplyTargetValidation(True)
        owner = self.client.get_owner_info().nick_name
        index, target = self._select_reply_target(
            messages,
            event.is_group,
            owner,
            event.conversation_id,
            title,
        )
        if target is None:
            return ReplyTargetValidation(False, "reply target is no longer visible")
        target_key = self._target_key(target, index)
        if target_key == event.target_key:
            return ReplyTargetValidation(True)

        replacement_event = None
        if self._emitted_targets.get(event.conversation_id) != target_key:
            replacement_event = self._event(
                event.conversation_id,
                title,
                target,
                self._history_snapshot(
                    messages, index, event.is_group, event.conversation_id
                ),
                event.is_group,
                bool(event.is_group),
                0,
                target_key,
            )
            if replacement_event is not None:
                self._emitted_targets[event.conversation_id] = target_key
        return ReplyTargetValidation(
            False,
            "a newer reply target replaced this message",
            replacement_event,
        )

    def materialize_event(
        self, event: WechatDesktopEvent, *, before_share_fetch=None
    ) -> tuple[WechatDesktopEvent, int]:
        # UIA priority must always be acquired before shared driver state.
        # Reply validation follows the same order, preventing lock inversion.
        with self._uia_priority.lease(reply=False):
            with self._operation_lock:
                materialized, resolve_count = self._materialize_event_locked(event)
        # Network fetches must never retain a UIA priority lease or the driver
        # state lock. A slow site cannot block WeChat scanning or sending.
        return (
            self._fetch_share_reference(
                materialized, before_share_fetch=before_share_fetch
            ),
            resolve_count,
        )

    def _fetch_share_reference(
        self, event: WechatDesktopEvent, *, before_share_fetch=None
    ) -> WechatDesktopEvent:
        reference = event.reference
        if str(reference.get("content_type") or "").lower() != "share_card":
            return event
        url = str(reference.get("url") or "").strip()
        if not url:
            reference["fetch_status"] = "link_unavailable"
            reference["fetched_content"] = ""
            reference["knowledge_status"] = "link_unavailable"
            reference["knowledge_content"] = ""
            return event

        from agent.tools.utils.url_safety import validate_url_safe

        try:
            validate_url_safe(url)
        except ValueError as exc:
            logger.warning("[WechatDesktop] unsafe share URL rejected: %s", exc)
            reference["fetch_status"] = "error"
            reference["fetched_content"] = ""
            reference["knowledge_status"] = "error"
            reference["knowledge_content"] = ""
            return event

        if before_share_fetch is not None:
            try:
                before_share_fetch()
            except Exception as exc:
                logger.warning("[WechatDesktop] share fetch notice failed: %s", exc)

        def run_web_fetch():
            fetcher = self._web_fetcher
            if fetcher is None:
                from agent.tools.web_fetch.web_fetch import WebFetch

                fetcher = WebFetch(
                    {"cwd": str(Path(__file__).resolve().parents[2])}
                )
                self._web_fetcher = fetcher
            return fetcher.execute({"url": url})

        def run_knowledge_acquisition():
            extractor = self._knowledge_extractor
            if extractor is None:
                from channel.wechat_desktop.knowledge_acquisition import (
                    KnowledgeAcquisitionExtractor,
                )

                extractor = KnowledgeAcquisitionExtractor(self.config)
                self._knowledge_extractor = extractor
            execute = getattr(extractor, "extract", extractor)
            return execute(url)

        knowledge_enabled = bool(
            self.config.get("knowledge_acquisition_enabled", False)
        )
        jobs = {"web_fetch": run_web_fetch}
        if knowledge_enabled:
            jobs["knowledge_acquisition"] = run_knowledge_acquisition

        results = {}
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = {
                name: executor.submit(operation)
                for name, operation in jobs.items()
            }
            for name, future in futures.items():
                try:
                    results[name] = ("success", future.result())
                except Exception as exc:
                    logger.warning(
                        "[WechatDesktop] share %s failed: %s", name, exc
                    )
                    results[name] = ("error", "")

        web_status, web_result = results["web_fetch"]
        if (
            web_status == "success"
            and getattr(web_result, "status", "") == "success"
        ):
            reference["fetch_status"] = "success"
            reference["fetched_content"] = str(web_result.result or "")
        else:
            reference["fetch_status"] = "error"
            reference["fetched_content"] = str(
                getattr(web_result, "result", "") or ""
            )

        if knowledge_enabled:
            knowledge_status, knowledge_result = results["knowledge_acquisition"]
            reference["knowledge_status"] = knowledge_status
            reference["knowledge_content"] = str(knowledge_result or "")
        else:
            reference["knowledge_status"] = "disabled"
            reference["knowledge_content"] = ""
        return event

    def _materialize_event_locked(
        self, event: WechatDesktopEvent
    ) -> tuple[WechatDesktopEvent, int]:
        """Resolve only the selected target attachment, never the visible list."""
        messages = self._message_snapshots.get(event.conversation_id, [])
        target_index = next(
            (
                index
                for index, message in enumerate(messages)
                if event.message_stable_id
                and message.stable_id == event.message_stable_id
            ),
            -1,
        )
        if target_index < 0:
            target_index = next(
                (
                    index
                    for index, message in enumerate(messages)
                    if event.message_runtime_id
                    and message.runtime_id == event.message_runtime_id
                    and self._content_signature(message) == event.content_signature
                ),
                -1,
            )
        if target_index < 0:
            return event, 0

        resolved, resolve_count = self._resolve_reply_target_message(
            event.conversation_id,
            messages[target_index],
        )
        messages = list(messages)
        messages[target_index] = resolved
        self._message_snapshots[event.conversation_id] = messages

        if resolved.message_type in {"file", "image"} and resolved.file_path:
            event.content = resolved.file_path
            if resolved.message_type == "image":
                event.evidence_path = resolved.file_path
        if resolved.reference is not None:
            reference = resolved.reference
            event.reference = {
                "sender_name": reference.sender_name,
                "content": reference.content,
                "content_type": reference.message_type,
                "file_path": reference.file_path,
                "resolved": reference.resolved,
                "degraded": reference.degraded,
                "strategy": reference.strategy,
                "original_content": reference.original_content,
                "url": reference.url,
                "platform": reference.platform,
                "fetched_content": reference.fetched_content,
                "fetch_status": reference.fetch_status,
                "knowledge_content": reference.knowledge_content,
                "knowledge_status": reference.knowledge_status,
                "depth": 1,
            }
            if reference.file_path:
                event.evidence_path = reference.file_path
            event.history = self._history_snapshot(
                messages,
                target_index,
                event.is_group,
                event.conversation_id,
            )
        return event, resolve_count

    def send_text(self, conversation: str, text: str) -> dict:
        """通过独立发送组件发送普通回复。"""

        return self._send_operations.send_text(conversation, text)

    def send_interim_text(self, conversation: str, text: str) -> dict:
        """发送进度提示，不等待普通回复的拟人化节流间隔。"""

        return self._send_operations.send_text(conversation, text, expedited=True)

    def send_image(self, conversation: str, image_path: str) -> dict:
        """通过独立发送组件发送图片。"""

        return self._send_operations.send_image(conversation, image_path)
