"""Event driver for the WeChat 4.1.9.30 UIA client."""

from __future__ import annotations

import hashlib
import random
import threading
import time
import unicodedata
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Optional

from common.log import logger
from channel.wechat_desktop.models import (
    ReplyTargetValidation,
    UiaChatMessage,
    WechatDesktopEvent,
)
from channel.wechat_desktop.shell_hook import WindowsShellHook
from channel.wechat_desktop.uia_client import WechatUiaClient


class WechatUiaDriver:
    def __init__(
        self,
        config: dict,
        client: Optional[WechatUiaClient] = None,
        shell_hook: Optional[WindowsShellHook] = None,
    ):
        self.config = config
        self.client = client or WechatUiaClient(config)
        self._operation_lock = self.client.operation_lock
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

    def _trace(self, stage: str, message: str, *args) -> None:
        if bool(self.config.get("diagnostic_logging", False)):
            logger.info(
                "[WechatDesktop][trace:%s] " + message,
                stage,
                *args,
            )

    @property
    def reply_in_flight(self) -> bool:
        return self._reply_in_flight.is_set()

    def begin_reply_cycle(self, conversation: str, conversation_id: str = ""):
        self._reply_conversation = str(conversation or "")
        self._reply_conversation_id = str(conversation_id or "")
        self._last_reply_monitor_at = 0.0
        self._reply_in_flight.set()

    def end_reply_cycle(self):
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
            self._interim_texts.setdefault(conversation_id, set()).add(text)

    def forget_interim_text(self, conversation_id: str, text: str):
        texts = self._interim_texts.get(str(conversation_id or ""))
        if not texts:
            return
        texts.discard(str(text or "").strip())
        if not texts:
            self._interim_texts.pop(str(conversation_id or ""), None)

    def _is_interim_message(
        self, conversation_id: str, message: UiaChatMessage
    ) -> bool:
        return str(message.content or "").strip() in self._interim_texts.get(
            str(conversation_id or ""), set()
        )

    def _is_reply_conversation(self, row_key: str, row) -> bool:
        return self.reply_in_flight and (
            row_key == self._reply_conversation_id
            or row.conversation_title == self._reply_conversation
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

    def _observation(self, **updates) -> dict:
        owner = ""
        owner_source = "unknown"
        try:
            info = self.client.get_owner_info()
            owner, owner_source = info.nick_name, info.source
        except Exception:
            pass
        result = {
            "error": "",
            "automation_backend": "uia",
            "uia_available": True,
            "shell_hook_active": self._hook.available,
            "shell_hook_error": self._hook.error,
            "owner_name": owner,
            "owner_source": owner_source,
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

    def _stabilize_messages(
        self,
        conversation_id: str,
        messages: list[UiaChatMessage],
    ) -> list[UiaChatMessage]:
        """Carry message identities across scrolling UIA snapshots."""
        previous = self._message_snapshots.get(conversation_id, [])
        stable_ids = ["" for _ in messages]
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
                used_previous.add(previous_index)
                search_start = previous_index + 1

        stabilized = [
            replace(message, stable_id=stable_ids[index] or uuid.uuid4().hex)
            for index, message in enumerate(messages)
        ]
        self._message_snapshots[conversation_id] = stabilized
        return stabilized

    def _select_reply_target(
        self,
        messages: list[UiaChatMessage],
        is_group: bool,
        owner: str,
        conversation_id: str = "",
    ) -> tuple[int, Optional[UiaChatMessage]]:
        """Classify visible nodes newest-first without using message direction."""
        if not messages:
            return -1, None
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if self._is_interim_message(conversation_id, message):
                continue
            if not is_group or self._mentions_owner(message.content, owner):
                return index, message
        return -1, None

    def _history_snapshot(
        self,
        messages: list[UiaChatMessage],
        target_index: int,
        is_group: bool,
        conversation_id: str = "",
    ) -> list[dict]:
        target = messages[target_index] if 0 <= target_index < len(messages) else None
        followup_limit = max(
            0,
            min(int(self.config.get("attachment_context_window_messages", 2)), 10),
        )
        start = 0
        end = len(messages)
        if target is not None and target.message_type in {"file", "image"}:
            # Previous messages are already useful conversation context and
            # remain unbounded within the visible snapshot. Only absorb a
            # small number of messages sent after the attachment; later ones
            # stay outside this event and are handled as new queue targets.
            end = min(len(messages), target_index + followup_limit + 1)
        context_messages = [
            message
            for index, message in enumerate(messages)
            if start <= index < end and index != target_index
            and not self._is_interim_message(conversation_id, message)
        ]
        history = []
        for message in context_messages:
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
                }
            )
        return history

    def _resolve_message_files(
        self, messages: list[UiaChatMessage]
    ) -> list[UiaChatMessage]:
        resolved_messages = []
        for visible_message in messages:
            if visible_message.message_type == "file":
                file_path = self.client.fetch_message_file(visible_message)
                if file_path:
                    visible_message = replace(visible_message, file_path=file_path)
            elif visible_message.message_type == "image":
                fetch_image = getattr(self.client, "fetch_message_image", None)
                image_path = fetch_image(visible_message) if fetch_image else ""
                if image_path:
                    visible_message = replace(visible_message, file_path=image_path)
            resolved_messages.append(visible_message)
        return resolved_messages

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
                message.file_path if message.message_type == "image" else ""
            ),
            bounds=message.bounds,
            history=history,
            target_key=target_key,
            session_unread_count=max(0, int(session_unread_count)),
            observed_at=time.time() + ordinal / 1_000_000.0,
            event_id=event_id,
        )
        setattr(event, "_fingerprint_content", target_key)
        return event

    def observe_events(self) -> tuple[dict, list[WechatDesktopEvent]]:
        with self._operation_lock:
            try:
                rows = self.client.get_visible_conversations()
                self._trace(
                    "01-sessions",
                    "visible=%s unread=%s mentioned=%s",
                    len(rows),
                    sum(row.not_read_number > 0 for row in rows),
                    sum(bool(row.mentions_self) for row in rows),
                )
                current_rows = {self._row_key(row): row for row in rows}
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
                if candidates:
                    self.client.focus_window()

                events: list[WechatDesktopEvent] = []
                ordinal = 0
                for row_key, row in candidates:
                    name = row.conversation_title
                    active_reply_conversation = self._is_reply_conversation(
                        row_key, row
                    )
                    is_group = row_key in self._known_group_keys or name in self._known_groups
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
                    if not is_group:
                        located = self.client.locate_conversation(
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
                        header = self.client.get_title()
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

                    messages = self.client.get_chat_history(
                        name,
                        self._message_limit(),
                        runtime_id=row.runtime_id,
                        row_index=row.row_index,
                    )
                    messages = self._resolve_message_files(messages)
                    messages = self._stabilize_messages(row_key, messages)
                    self._trace(
                        "04-history",
                        "conversation=%s group=%s visible_messages=%s",
                        name,
                        is_group,
                        len(messages),
                    )
                    owner_info = self.client.get_owner_info()
                    owner = owner_info.nick_name
                    self._trace(
                        "05-owner",
                        "conversation=%s available=%s source=%s name=%s",
                        name,
                        bool(owner),
                        owner_info.source,
                        owner or "<empty>",
                    )
                    target_index, message = self._select_reply_target(
                        messages, is_group, owner, row_key
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
                    if self._emitted_targets.get(row_key) == target_key:
                        continue
                    if (
                        is_group
                        and not message.sender_name
                        and row.preview_has_sender_prefix
                    ):
                        message = replace(message, sender_name=row.preview_sender)
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

    def _selector(self, conversation: str):
        row = self._conversation_selectors.get(conversation)
        if row is None:
            return conversation, "", -1
        return row.conversation_title, row.runtime_id, row.row_index

    def validate_reply_target(self, event: WechatDesktopEvent) -> ReplyTargetValidation:
        """Re-read the UI and return a replacement event when the target changed."""
        try:
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
        title, runtime_id, row_index = self._selector(event.conversation_id)
        messages = self.client.get_chat_history(
            title,
            self._message_limit(),
            runtime_id=runtime_id,
            row_index=row_index,
        )
        messages = self._resolve_message_files(messages)
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
            messages, event.is_group, owner, event.conversation_id
        )
        if target is None:
            return ReplyTargetValidation(False, "reply target is no longer visible")
        target_key = self._target_key(target, index)
        if target_key == event.target_key:
            return ReplyTargetValidation(True)

        replacement_event = None
        if self._emitted_targets.get(event.conversation_id) != target_key:
            row = self._conversation_selectors[event.conversation_id]
            if (
                event.is_group
                and not target.sender_name
                and row.preview_has_sender_prefix
            ):
                target = replace(target, sender_name=row.preview_sender)
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

    def send_text(self, conversation: str, text: str) -> dict:
        title, runtime_id, row_index = self._selector(conversation)
        result = self.client.send_message(
            title, text, runtime_id=runtime_id, row_index=row_index
        )
        return {
            **result,
            "accepted_by": "uia",
            "verification": "outgoing_uia_bubble" if result.get("verified") else "unverified",
            "observation": self._observation(),
        }

    def send_image(self, conversation: str, image_path: str) -> dict:
        path = str(Path(image_path).resolve())
        title, runtime_id, row_index = self._selector(conversation)
        result = self.client.send_file(
            title, [path], runtime_id=runtime_id, row_index=row_index
        )
        return {
            **result,
            "accepted_by": "uia",
            "verification": "outgoing_uia_bubble" if result.get("verified") else "unverified",
            "observation": self._observation(),
        }
