"""Event driver for the WeChat 4.x UIA client."""

from __future__ import annotations

import hashlib
import random
import threading
import time
import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Optional

from channel.wechat_desktop.models import UiaChatMessage, WechatDesktopEvent
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
        self._message_tails: dict[str, list[UiaChatMessage]] = {}
        self._conversation_selectors = {}
        self._emitted_targets: dict[str, str] = {}
        self._seen_ids: set[str] = set()
        self._seen_order: list[str] = []
        self._known_groups = {str(x) for x in config.get("auto_reply_groups", [])}
        self._known_group_keys: set[str] = set()
        self._reply_in_flight = threading.Event()
        self._reply_conversation = ""

    @property
    def reply_in_flight(self) -> bool:
        return self._reply_in_flight.is_set()

    def begin_reply_cycle(self, conversation: str):
        self._reply_conversation = str(conversation or "")
        self._reply_in_flight.set()

    def end_reply_cycle(self):
        self._reply_conversation = ""
        self._reply_in_flight.clear()

    def start(self) -> bool:
        resume_waits = getattr(self.client, "resume_waits", None)
        if resume_waits:
            resume_waits()
        if not self._hook_started:
            self._hook_started = self._hook.start()
        return self._hook_started

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
        timeout = max(1.0, float(self.config.get("shell_hook_reconcile_seconds", 15)))
        deadline = time.monotonic() + timeout
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "reconcile"
            if self._hook.wait(min(0.25, remaining)):
                signal = self._hook.consume(self._last_hook_at)
                if signal.signaled:
                    self._last_hook_at = signal.created_at
                    if self._settle_hook_signal(stop_event):
                        return "stopped"
                    return signal.reason
                if self._hook.error:
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
        return max(1, min(int(self.config.get("uia_message_limit", 5)), 5))

    @staticmethod
    def _row_key(row) -> str:
        if row.runtime_id:
            return f"uia-session:{row.runtime_id}"
        stable = f"{row.automation_id}\0{row.row_index}"
        return "uia-session:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()

    @classmethod
    def _target_key(cls, message: UiaChatMessage, index: int) -> str:
        stable = message.runtime_id or repr(
            (
                message.direction,
                message.message_type,
                message.content,
                int(index),
            )
        )
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def _select_reply_target(
        self,
        messages: list[UiaChatMessage],
        is_group: bool,
        owner: str,
    ) -> tuple[int, Optional[UiaChatMessage]]:
        """Return exactly one latest, structurally unanswered UIA bubble."""
        if is_group:
            candidates = [
                index
                for index, message in enumerate(messages)
                if message.direction == "incoming"
                and self._mentions_owner(message.content, owner)
            ]
        else:
            last_outgoing = max(
                (
                    index
                    for index, message in enumerate(messages)
                    if message.direction == "outgoing"
                ),
                default=-1,
            )
            candidates = [
                index
                for index, message in enumerate(messages)
                if index > last_outgoing and message.direction == "incoming"
            ]
        if not candidates:
            return -1, None
        target_index = candidates[-1]
        if any(
            message.direction == "unknown"
            for message in messages[target_index + 1 :]
        ):
            return -1, None
        if any(
            message.direction == "outgoing"
            for message in messages[target_index + 1 :]
        ):
            return -1, None
        return target_index, messages[target_index]

    @staticmethod
    def _history_snapshot(
        messages: list[UiaChatMessage], target_index: int, is_group: bool
    ) -> list[dict]:
        history = []
        for index, message in enumerate(messages):
            if index == target_index or message.direction not in {"incoming", "outgoing"}:
                continue
            history.append(
                {
                    "sender_name": (
                        message.sender_name
                        if is_group and message.direction == "incoming"
                        else ""
                    ),
                    "content": message.content,
                    "direction": message.direction,
                    "content_type": message.message_type,
                }
            )
        return history

    @staticmethod
    def _mentions_owner(content: str, owner: str) -> bool:
        if not owner:
            return False
        value = unicodedata.normalize("NFKC", str(content or ""))
        target = unicodedata.normalize("NFKC", str(owner)).strip()
        compact = value.replace("\u2005", " ").replace("\u00a0", " ")
        return f"@{target}" in compact

    def _remember(self, event_id: str) -> bool:
        if event_id in self._seen_ids:
            return False
        self._seen_ids.add(event_id)
        self._seen_order.append(event_id)
        if len(self._seen_order) > 10000:
            expired = self._seen_order[:1000]
            self._seen_order = self._seen_order[1000:]
            self._seen_ids.difference_update(expired)
        return True

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
    ) -> Optional[WechatDesktopEvent]:
        owner = self.client.get_owner_info().nick_name
        event_id = hashlib.sha256(
            f"wechat-uia4x\0{owner}\0{conversation_id}\0{target_key}".encode("utf-8")
        ).hexdigest()
        if not self._remember(event_id):
            return None
        sender = message.sender_name or ("群成员" if is_group else conversation)
        event = WechatDesktopEvent(
            kind="message",
            conversation_id=conversation_id,
            conversation_name=conversation,
            sender_id=sender,
            sender_name=sender,
            content_type=message.message_type,
            content=message.content,
            direction=message.direction,
            is_group=is_group,
            is_at=is_at,
            source_type="group" if is_group else "private",
            bounds=message.bounds,
            confidence=message.confidence,
            history=history,
            target_key=target_key,
            observed_at=time.time() + ordinal / 1_000_000.0,
            event_id=event_id,
        )
        setattr(event, "_fingerprint_content", target_key)
        return event

    def observe_events(self) -> tuple[dict, list[WechatDesktopEvent]]:
        with self._operation_lock:
            try:
                rows = self.client.get_visible_conversations()
                current_rows = {self._row_key(row): row for row in rows}
                self._conversation_selectors = dict(current_rows)
                first_scan = not self._rows
                previous_rows = self._rows
                self._rows = current_rows
                if first_scan and not bool(self.config.get("bootstrap_existing_messages", False)):
                    return self._observation(
                        visible_conversations=len(rows), unread_conversations=sum(row.not_read_number > 0 for row in rows)
                    ), []

                candidates = []
                for row_key, row in current_rows.items():
                    previous = previous_rows.get(row_key)
                    changed = previous is None or previous.row_signature != row.row_signature
                    if row.not_read_number > 0 or changed:
                        candidates.append((row_key, row))
                if candidates:
                    self.client.focus_window()

                events: list[WechatDesktopEvent] = []
                ordinal = 0
                for row_key, row in candidates:
                    name = row.conversation_title
                    is_group = row_key in self._known_group_keys or name in self._known_groups
                    if is_group and not row.mentions_self:
                        continue
                    if not is_group:
                        if not self.client.locate_conversation(
                            name, row.runtime_id, row.row_index
                        ):
                            continue
                        header = self.client.get_title()
                        is_group = header.header_type == "group"
                        if is_group:
                            self._known_group_keys.add(row_key)
                            if not row.mentions_self:
                                continue

                    messages = self.client.get_chat_history(
                        name,
                        self._message_limit(),
                        runtime_id=row.runtime_id,
                        row_index=row.row_index,
                    )
                    self._message_tails[row_key] = list(messages)
                    owner = self.client.get_owner_info().nick_name
                    target_index, message = self._select_reply_target(
                        messages, is_group, owner
                    )
                    if message is None:
                        self._emitted_targets.pop(row_key, None)
                        continue
                    target_key = self._target_key(message, target_index)
                    if self._emitted_targets.get(row_key) == target_key:
                        continue
                    if (
                        is_group
                        and not message.sender_name
                        and row.preview_has_sender_prefix
                    ):
                        message = replace(message, sender_name=row.preview_sender)
                    history = self._history_snapshot(messages, target_index, is_group)
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
                    )
                    if event is not None:
                        self._emitted_targets[row_key] = target_key
                        events.append(event)
                return self._observation(
                    visible_conversations=len(rows),
                    unread_conversations=sum(row.not_read_number > 0 for row in rows),
                ), events
            except Exception as exc:
                return self._observation(
                    error=f"WeChat UI Automation unavailable: {exc}",
                    uia_available=False,
                ), []

    def _selector(self, conversation: str):
        row = self._conversation_selectors.get(conversation)
        if row is None:
            return conversation, "", -1
        return row.conversation_title, row.runtime_id, row.row_index

    def validate_reply_target(self, event: WechatDesktopEvent) -> tuple[bool, str]:
        """Re-read the UI immediately before send to reject stale LLM output."""
        try:
            if event.conversation_id not in self._conversation_selectors:
                return False, "conversation row is no longer visible"
            title, runtime_id, row_index = self._selector(event.conversation_id)
            messages = self.client.get_chat_history(
                title,
                self._message_limit(),
                runtime_id=runtime_id,
                row_index=row_index,
            )
            owner = self.client.get_owner_info().nick_name
            index, target = self._select_reply_target(messages, event.is_group, owner)
            if target is None:
                return False, "target message is already answered or no longer visible"
            if self._target_key(target, index) != event.target_key:
                return False, "a newer reply target replaced this message"
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def send_text(self, conversation: str, text: str) -> dict:
        title, runtime_id, row_index = self._selector(conversation)
        result = self.client.send_message(
            title, text, runtime_id=runtime_id, row_index=row_index
        )
        try:
            tail = self.client.get_chat_history(
                title,
                self._message_limit(),
                runtime_id=runtime_id,
                row_index=row_index,
            )
            for index in range(len(tail) - 1, -1, -1):
                if tail[index].content == text:
                    tail[index] = replace(tail[index], direction="outgoing")
                    break
            self._message_tails[conversation] = tail
            self._emitted_targets.pop(conversation, None)
        except Exception:
            pass
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
        try:
            tail = self.client.get_chat_history(
                title,
                self._message_limit(),
                runtime_id=runtime_id,
                row_index=row_index,
            )
            if tail:
                tail[-1] = replace(tail[-1], direction="outgoing")
            self._message_tails[conversation] = tail
        except Exception:
            pass
        return {
            **result,
            "accepted_by": "uia",
            "verification": "outgoing_uia_bubble" if result.get("verified") else "unverified",
            "observation": self._observation(),
        }
