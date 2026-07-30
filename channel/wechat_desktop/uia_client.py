"""Synchronous WeChat 4.1.9.30 client built on Windows UI Automation."""

from __future__ import annotations

import hashlib
import ctypes
import os
import random
import re
import shutil
import threading
import time
import unicodedata
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Iterator, Optional

from common.log import logger
from channel.wechat_desktop.models import (
    ConversationInfo,
    HeaderInfo,
    OwnerInfo,
    UiaChatMessage,
)


SESSION_PREFIX = "session_item_"
MESSAGE_LIST_ID = "chat_message_list"
INPUT_ID = "chat_input_field"
MENTION_MARKERS = ("[有人@我]", "有人@我", "[Someone mentioned me]")


def _text(value) -> str:
    return str(value or "").strip()


def _bounds(control) -> Optional[tuple[int, int, int, int]]:
    try:
        rect = control.BoundingRectangle
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        return None


def _runtime_id(control) -> str:
    try:
        value = control.GetRuntimeId()
        if value:
            return ".".join(str(item) for item in value)
    except Exception:
        pass
    try:
        value = control.GetPropertyValue(30000)
        if value:
            return ".".join(str(item) for item in value)
    except Exception:
        pass
    return ""


def parse_session_accessible_name(
    title: str,
    accessible_name: str,
    automation_id: str = "",
    runtime_id: str = "",
    row_index: int = -1,
) -> ConversationInfo:
    """Parse only stable, localized session-row markers.

    The complete accessible name is retained only as a hash so previews do not
    leak into status responses or diagnostics.
    """
    value = _text(accessible_name)
    unread = 0
    patterns = (
        r"\[(\d+)\s*条\]",
        r"(?:未读|新消息)\s*[:：]?\s*(\d+)",
        r"(\d+)\s*条新消息",
        r"(\d+)\s*(?:new|unread)\s+messages?",
    )
    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            unread = int(match.group(1))
            break
    preview_sender = ""
    preview_has_sender_prefix = False
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    preview = next(
        (
            line
            for line in lines[1:]
            if not re.fullmatch(r"\d{1,2}:\d{2}", line)
            and line not in {"免打扰", "消息免打扰", "置顶", "已置顶"}
        ),
        "",
    )
    preview = re.sub(r"^\[\d+\s*条\]\s*", "", preview)
    sender_match = re.match(r"^([^:：]{1,80})[:：]\s*.+$", preview)
    if sender_match:
        preview_sender = sender_match.group(1).strip()
        preview_has_sender_prefix = bool(preview_sender)
    mentions = any(marker.casefold() in value.casefold() for marker in MENTION_MARKERS)
    return ConversationInfo(
        conversation_title=_text(title),
        is_do_not_disturb=any(x in value for x in ("免打扰", "消息免打扰")),
        is_top=any(x in value for x in ("置顶", "已置顶")),
        not_read_number=unread,
        mentions_self=mentions,
        row_signature=hashlib.sha256(value.encode("utf-8")).hexdigest(),
        automation_id=_text(automation_id),
        runtime_id=_text(runtime_id),
        row_index=int(row_index),
        preview_sender=preview_sender,
        preview_has_sender_prefix=preview_has_sender_prefix,
    )


class WechatUiaClient:
    """Minimal, bounded replacement for the SDK methods used by CowAgent."""

    def __init__(self, config: Optional[dict] = None):
        self.config = dict(config or {})
        self.operation_lock = threading.RLock()
        self._owner_cache: Optional[OwnerInfo] = None
        self._stop_event = threading.Event()
        self._last_send_at = 0.0
        self._last_conversation_send: dict[str, float] = {}
        self._known_outgoing_texts: dict[str, list[str]] = {}
        self.last_direction_resolution: dict = {}

    def cancel_waits(self):
        self._stop_event.set()

    def resume_waits(self):
        self._stop_event.clear()

    def _paced_wait(
        self,
        minimum_key: str,
        maximum_key: str,
        default_minimum: int,
        default_maximum: int,
    ) -> None:
        minimum = max(0, min(int(self.config.get(minimum_key, default_minimum)), 10000))
        maximum = max(
            minimum,
            min(int(self.config.get(maximum_key, default_maximum)), 10000),
        )
        delay_ms = random.randint(minimum, maximum) if maximum > minimum else minimum
        if delay_ms and self._stop_event.wait(delay_ms / 1000.0):
            raise RuntimeError("WeChat UI Automation is stopping")

    def _wait_for_send_slot(self, conversation: str) -> None:
        now = time.monotonic()
        minimum = max(
            0,
            int(self.config.get("uia_send_interval_ms_min", 2000)),
        )
        maximum = max(
            minimum,
            int(self.config.get("uia_send_interval_ms_max", 5000)),
        )
        interval = random.randint(minimum, maximum) / 1000.0
        cooldown = max(
            0.0,
            float(self.config.get("uia_conversation_cooldown_seconds", 5)),
        )
        due = max(
            self._last_send_at + interval,
            self._last_conversation_send.get(str(conversation), 0.0) + cooldown,
        )
        remaining = due - now
        if remaining > 0 and self._stop_event.wait(remaining):
            raise RuntimeError("WeChat UI Automation is stopping")

    def remember_outgoing_text(self, conversation: str, text: str) -> None:
        value = _text(text)
        if not value:
            return
        key = _text(conversation)
        items = self._known_outgoing_texts.setdefault(key, [])
        if value in items:
            items.remove(value)
        items.append(value)
        del items[:-20]

    def seed_outgoing_texts(self, conversation: str, texts: Iterable[str]) -> None:
        for text in texts:
            self.remember_outgoing_text(conversation, text)

    @staticmethod
    def _require_windows():
        if os.name != "nt":
            raise RuntimeError("WeChat desktop automation requires Windows")

    @staticmethod
    def _enumerate_main_windows() -> list[tuple[int, int]]:
        WechatUiaClient._require_windows()
        import win32api
        import win32con
        import win32gui
        import win32process

        windows: list[tuple[int, int]] = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            native_class = win32gui.GetClassName(hwnd)
            if native_class != "mmui::MainWindow" and not (
                native_class.startswith("Qt")
                and native_class.endswith("QWindowIcon")
            ):
                return True
            _, process_id = win32process.GetWindowThreadProcessId(hwnd)
            try:
                process = win32api.OpenProcess(
                    win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                    False,
                    process_id,
                )
                executable = win32process.GetModuleFileNameEx(process, 0)
                process.Close()
            except Exception:
                executable = ""
            if not executable or Path(executable).name.casefold() == "weixin.exe":
                windows.append((int(hwnd), int(process_id)))
            return True

        win32gui.EnumWindows(callback, None)
        return windows

    def _window(self) -> tuple[int, int]:
        windows = self._enumerate_main_windows()
        if not windows:
            raise RuntimeError("No logged-in WeChat 4.1.9.30 main window was found")
        if len(windows) != 1:
            raise RuntimeError(
                f"Exactly one WeChat 4.1.9.30 main window is required; found {len(windows)}"
            )
        return windows[0]

    def get_owner_window_handle(self) -> int:
        return self._window()[0]

    def get_owner_window_process_id(self) -> int:
        return self._window()[1]

    def allowed_process_ids(self) -> set[int]:
        try:
            return {self.get_owner_window_process_id()}
        except Exception:
            return set()

    @staticmethod
    def _walk(root, max_nodes: int = 4000) -> Iterator:
        queue = list(root.GetChildren())
        count = 0
        while queue and count < max(1, int(max_nodes)):
            control = queue.pop(0)
            count += 1
            yield control
            try:
                queue.extend(control.GetChildren())
            except Exception:
                pass

    @contextmanager
    def _uia_root(self):
        try:
            import uiautomation as auto
        except ImportError as exc:
            raise RuntimeError("uiautomation is not installed") from exc
        hwnd = self.get_owner_window_handle()
        with auto.UIAutomationInitializerInThread():
            yield auto.ControlFromHandle(hwnd)

    def probe_tree(self) -> int:
        with self.operation_lock, self._uia_root() as root:
            return sum(1 for _ in self._walk(root))

    def ensure_foreground_window(self) -> bool:
        """Bring WeChat forward when its process does not own the foreground."""
        self._require_windows()
        import win32api
        import win32con
        import win32gui
        import win32process

        hwnd, process_id = self._window()
        foreground = win32gui.GetForegroundWindow()
        if foreground:
            try:
                _, foreground_process_id = win32process.GetWindowThreadProcessId(
                    foreground
                )
            except Exception:
                foreground_process_id = 0
            if int(foreground_process_id) == int(process_id):
                return False

        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            current_thread = win32api.GetCurrentThreadId()
            foreground = win32gui.GetForegroundWindow()
            foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground)
            ctypes.windll.user32.AttachThreadInput(
                current_thread, foreground_thread, True
            )
            try:
                win32gui.SetForegroundWindow(hwnd)
            finally:
                ctypes.windll.user32.AttachThreadInput(
                    current_thread, foreground_thread, False
                )
        self._paced_wait(
            "uia_focus_settle_ms_min",
            "uia_focus_settle_ms_max",
            int(self.config.get("uia_focus_settle_ms", 350)),
            700,
        )
        foreground = win32gui.GetForegroundWindow()
        try:
            _, foreground_process_id = win32process.GetWindowThreadProcessId(foreground)
        except Exception:
            foreground_process_id = 0
        if int(foreground_process_id) != int(process_id):
            raise RuntimeError("WeChat could not be brought to the foreground")
        return True

    def focus_window(self) -> None:
        self.ensure_foreground_window()
        hwnd = self.get_owner_window_handle()
        self._recover_empty_tree(hwnd)

    def _click_taskbar_button(self) -> bool:
        try:
            import uiautomation as auto
            import win32gui

            taskbar = win32gui.FindWindow("Shell_TrayWnd", None)
            if not taskbar:
                return False
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(taskbar)
                for control in self._walk(root, 1000):
                    name = _text(control.Name).casefold()
                    if name in {"微信", "weixin", "wechat"} or "微信" in name:
                        try:
                            control.GetInvokePattern().Invoke()
                        except Exception:
                            control.Click()
                        return True
        except Exception:
            return False
        return False

    def _recover_empty_tree(self, hwnd: int) -> bool:
        try:
            if self.probe_tree() > 0:
                return False
        except Exception:
            return False
        attempts = max(0, min(int(self.config.get("uia_recovery_attempts", 3)), 3))
        settle = max(0, min(int(self.config.get("uia_recovery_settle_ms", 500)), 3000))
        for _ in range(attempts):
            self._click_taskbar_button()
            if settle:
                if self._stop_event.wait(settle / 1000.0):
                    raise RuntimeError("WeChat UI Automation is stopping")
            if self.probe_tree() > 0:
                return True
        raise RuntimeError(
            f"WeChat UI Automation tree remained empty after {attempts} recovery attempts"
        )

    def get_owner_info(self) -> OwnerInfo:
        configured = _text(self.config.get("self_display_name"))
        if self._owner_cache and self._owner_cache.nick_name and (
            not configured or self._owner_cache.nick_name == configured
        ):
            if bool(self.config.get("diagnostic_logging", False)):
                logger.info(
                    "[WechatDesktop][trace:05-owner] source=cache name=%s",
                    self._owner_cache.nick_name,
                )
            return self._owner_cache
        discovered = ""
        wx_id = ""
        discovery_method = "none"
        if bool(self.config.get("diagnostic_logging", False)):
            logger.info(
                "[WechatDesktop][trace:05-owner] lookup_start configured=%s cached=%s",
                bool(configured),
                bool(self._owner_cache and self._owner_cache.nick_name),
            )
        with self.operation_lock, self._uia_root() as root:
            root_bounds = _bounds(root)
            left_limit = root_bounds[0] + max(100, (root_bounds[2] - root_bounds[0]) // 5) if root_bounds else 0
            for control in self._walk(root):
                aid = _text(control.AutomationId).casefold()
                cls = _text(control.ClassName).casefold()
                name = _text(control.Name)
                bounds = _bounds(control)
                profile_hint = any(
                    key in aid
                    for key in ("self_avatar", "owner_avatar", "profile", "account", "userinfo")
                ) or ("avatar" in cls and any(key in aid for key in ("self", "owner")))
                in_sidebar = bool(bounds and left_limit and bounds[0] <= left_limit)
                if profile_hint and in_sidebar and name and name not in {"头像", "微信"}:
                    discovered = name
                    discovery_method = "sidebar"
                    break
            if not discovered:
                if bool(self.config.get("diagnostic_logging", False)):
                    logger.info(
                        "[WechatDesktop][trace:05-owner] sidebar_missing; opening_profile_popup"
                    )
                discovered, wx_id = self._read_owner_profile_popup(root)
                if discovered:
                    discovery_method = "profile_popup"
        if discovered and configured and discovered != configured:
            raise RuntimeError(
                "self_display_name does not match the account exposed by WeChat UIA"
            )
        if discovered:
            owner = OwnerInfo(discovered, wx_id=wx_id, source="uia")
        elif configured:
            owner = OwnerInfo(configured, source="config")
        else:
            owner = OwnerInfo("", source="unknown")
        # A transiently unavailable UI tree must not permanently disable group
        # mention matching. Cache only a usable account identity so later group
        # observations retry the profile-popup discovery path.
        self._owner_cache = owner if owner.nick_name else None
        if bool(self.config.get("diagnostic_logging", False)):
            logger.info(
                "[WechatDesktop][trace:05-owner] lookup_result available=%s source=%s method=%s name=%s",
                bool(owner.nick_name),
                owner.source,
                discovery_method if owner.source == "uia" else owner.source,
                owner.nick_name or "<empty>",
            )
        return owner

    def _read_owner_profile_popup(self, root) -> tuple[str, str]:
        """Open the unlabelled top-left avatar and read its semantic popup."""
        try:
            import uiautomation as auto
            import win32api
            import win32con

            bounds = _bounds(root)
            if not bounds:
                return "", ""
            old_cursor = win32api.GetCursorPos()
            try:
                auto.Click(bounds[0] + 38, bounds[1] + 68)
                time.sleep(
                    max(
                        0,
                        min(int(self.config.get("uia_selection_settle_ms", 150)), 1000),
                    )
                    / 1000.0
                )
            finally:
                win32api.SetCursorPos(old_cursor)

            popup = next(
                (
                    item
                    for item in auto.GetRootControl().GetChildren()
                    if _text(item.ClassName) == "mmui::ProfileUniquePop"
                ),
                None,
            )
            if popup is None:
                return "", ""
            display_name = ""
            wx_id = ""
            for control in self._walk(popup, 500):
                automation_id = _text(control.AutomationId)
                if automation_id.endswith("display_name_text"):
                    display_name = _text(control.Name)
                elif "ProfileTextView" in _text(control.ClassName):
                    candidate = _text(control.Name)
                    if candidate and candidate != display_name:
                        wx_id = candidate
            return display_name, wx_id
        except Exception:
            return "", ""
        finally:
            try:
                import win32api
                import win32con

                win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
                win32api.keybd_event(
                    win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0
                )
            except Exception:
                pass

    def get_visible_conversations(self) -> list[ConversationInfo]:
        rows: list[ConversationInfo] = []
        with self.operation_lock, self._uia_root() as root:
            for control in self._walk(root):
                automation_id = _text(control.AutomationId)
                if not automation_id.startswith(SESSION_PREFIX):
                    continue
                title = automation_id[len(SESSION_PREFIX) :]
                rows.append(
                    parse_session_accessible_name(
                        title,
                        _text(control.Name),
                        automation_id,
                        runtime_id=_runtime_id(control),
                        row_index=len(rows),
                    )
                )
        return rows

    @classmethod
    def _active_conversation_has_content(cls, root, target: str) -> bool:
        """Return whether *target* is already open with a populated chat pane."""
        current_title = ""
        message_list = None
        for control in cls._walk(root):
            automation_id = _text(control.AutomationId)
            if automation_id.endswith("current_chat_name_label"):
                current_title = _text(control.Name)
            elif automation_id == MESSAGE_LIST_ID:
                message_list = control
        if current_title != _text(target) or message_list is None:
            return False
        try:
            return any(
                _text(item.Name)
                and "Chat" in _text(item.ClassName)
                and "ItemView" in _text(item.ClassName)
                for item in message_list.GetChildren()
            )
        except Exception:
            return False

    def locate_conversation(
        self,
        who: str,
        runtime_id: str = "",
        row_index: int = -1,
    ) -> bool:
        target = _text(who)
        if not target:
            return False
        with self.operation_lock, self._uia_root() as root:
            # Selecting an already active session toggles some WeChat builds
            # from the populated detail pane to a blank/inactive state.
            if self._active_conversation_has_content(root, target):
                return True
            target_id = f"{SESSION_PREFIX}{target}"
            session_rows = [
                control
                for control in self._walk(root)
                if _text(control.AutomationId).startswith(SESSION_PREFIX)
            ]
            candidates = [
                control
                for control in session_rows
                if _text(control.AutomationId) == target_id
            ]
            control = next(
                (
                    item
                    for item in candidates
                    if runtime_id and _runtime_id(item) == runtime_id
                ),
                None,
            )
            # A supplied RuntimeId is the identity of this exact session row.
            # Never fall back to another row with the same title: that could
            # send to the wrong person when duplicate display names reorder.
            if runtime_id and control is None:
                return False
            if control is None and 0 <= int(row_index) < len(session_rows):
                indexed = session_rows[int(row_index)]
                if _text(indexed.AutomationId) == target_id:
                    control = indexed
            if control is None and not runtime_id and len(candidates) == 1:
                control = candidates[0]
            if control is not None:
                # WeChat 4.1.9.30 exposes SelectionItemPattern on a
                # session row but Select() only changes UIA selection state;
                # it does not activate the detail pane.  Use a real click,
                # restore the cursor, and verify the chat UI before
                # proceeding.  One bounded retry handles a dropped click
                # during window activation.
                for _ in range(2):
                    old_cursor = None
                    try:
                        import win32api

                        old_cursor = win32api.GetCursorPos()
                        control.Click()
                    finally:
                        if old_cursor is not None:
                            try:
                                win32api.SetCursorPos(old_cursor)
                            except Exception:
                                pass
                    self._paced_wait(
                        "uia_selection_settle_ms_min",
                        "uia_selection_settle_ms_max",
                        int(self.config.get("uia_selection_settle_ms", 250)),
                        500,
                    )
                    has_message_list = any(
                        _text(item.AutomationId) == MESSAGE_LIST_ID
                        for item in self._walk(root)
                    )
                    if has_message_list:
                        return True
        return False

    def get_title(self) -> HeaderInfo:
        title = ""
        count = 1
        with self.operation_lock, self._uia_root() as root:
            for control in self._walk(root):
                automation_id = _text(control.AutomationId)
                if automation_id.endswith("current_chat_name_label"):
                    title = _text(control.Name)
                elif automation_id.endswith("current_chat_count_label"):
                    match = re.search(r"(\d+)", _text(control.Name))
                    if match:
                        count = max(1, int(match.group(1)))
        return HeaderInfo(title, "group" if count > 1 else ("private" if title else "unknown"), count)

    @staticmethod
    def _message_type(class_name: str, content: str) -> str:
        class_value = _text(class_name).casefold()
        content_value = _text(content)
        if (
            "image" in class_value
            or content_value in {"[图片]", "[Image]"}
            or (
                "chatbubblereferitemview" in class_value
                and content_value.casefold() in {"图片", "image"}
            )
        ):
            return "image"
        if "voice" in class_value or content_value in {"[语音]", "[Voice]"}:
            return "voice"
        if (
            "file" in class_value
            or content_value in {"[文件]", "[File]"}
            or (
                "chatbubbleitemview" in class_value
                and WechatUiaClient._file_card_metadata(content_value)[0]
            )
        ):
            return "file"
        return "text"

    @staticmethod
    def _file_card_metadata(content: str) -> tuple[str, Optional[int]]:
        """Extract the filename and displayed size from a WeChat file card."""
        lines = [line.strip() for line in _text(content).splitlines() if line.strip()]
        if len(lines) < 2 or lines[0].casefold() not in {"文件", "file"}:
            return "", None
        filename = Path(lines[1]).name.strip()
        if not filename or not Path(filename).suffix:
            return "", None
        size = None
        if len(lines) >= 3:
            match = re.fullmatch(
                r"([0-9]+(?:\.[0-9]+)?)\s*(B|K|KB|M|MB|G|GB)",
                lines[2],
                re.IGNORECASE,
            )
            if match:
                units = {
                    "B": 1,
                    "K": 1024,
                    "KB": 1024,
                    "M": 1024 ** 2,
                    "MB": 1024 ** 2,
                    "G": 1024 ** 3,
                    "GB": 1024 ** 3,
                }
                size = int(float(match.group(1)) * units[match.group(2).upper()])
        return filename, size

    @classmethod
    def _message_sender(
        cls,
        direction: str,
        header: HeaderInfo,
        owner: str,
        item,
        content: str,
    ) -> str:
        """Map structural direction to sender without comparing display names."""
        if direction == "outgoing":
            return _text(owner)
        if direction != "incoming":
            return ""
        if header.header_type == "private":
            return _text(header.title)
        if header.header_type == "group":
            try:
                for child in item.GetChildren():
                    candidate = _text(child.Name)
                    if candidate and candidate != content and len(candidate) <= 80:
                        return candidate
            except Exception:
                pass
        return ""

    @staticmethod
    def _history_message_content(value: str) -> str:
        """Remove the timestamp appended by WeChat's history result rows."""
        text = _text(value)
        return re.sub(
            r"\s+(?:\d{4}年\d{1,2}月\d{1,2}日\s+)?\d{1,2}:\d{2}$",
            "",
            text,
        ).strip()

    @staticmethod
    def _choose_self_member(
        candidate_histories: list[list[str]], outgoing_anchors: Iterable[str]
    ) -> Optional[int]:
        """Choose the current account among same-name member candidates."""
        if len(candidate_histories) == 1:
            return 0
        anchors = {_text(item) for item in outgoing_anchors if _text(item)}
        if not anchors:
            return None
        matches = [
            index
            for index, history in enumerate(candidate_histories)
            if any(anchor in set(history) for anchor in anchors)
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _apply_self_history_directions(
        messages: list[UiaChatMessage], self_history: Iterable[str]
    ) -> list[UiaChatMessage]:
        """Classify messages from a member-filter result, failing on ambiguity."""
        self_counts = Counter(_text(item) for item in self_history if _text(item))
        if not self_counts:
            # An empty filtered list can also mean that WeChat did not finish
            # rendering the history view.  It is not proof that every visible
            # bubble came from somebody else.
            return messages
        total_counts = Counter(
            _text(message.content)
            for message in messages
            if message.direction == "unknown" and _text(message.content)
        )
        resolved = []
        for message in messages:
            if message.direction != "unknown":
                resolved.append(message)
                continue
            content = _text(message.content)
            total = total_counts.get(content, 0)
            own = self_counts.get(content, 0)
            if total and own == total:
                direction = "outgoing"
            elif total and own == 0:
                direction = "incoming"
            else:
                # Identical content appears on both sides, so occurrence order
                # cannot be proven from the filtered history list.
                resolved.append(message)
                continue
            resolved.append(
                replace(
                    message,
                    direction=direction,
                )
            )
        return resolved

    @staticmethod
    def _click_and_restore(control) -> None:
        import win32api

        cursor = win32api.GetCursorPos()
        try:
            control.Click()
        finally:
            win32api.SetCursorPos(cursor)

    @staticmethod
    def _visible_process_windows(process_id: int) -> list[int]:
        import win32gui
        import win32process

        windows = []

        def collect(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if int(pid) == int(process_id):
                windows.append(int(hwnd))
            return True

        win32gui.EnumWindows(collect, None)
        return windows

    def _history_member_candidates(
        self, history_hwnd: int, owner: str
    ) -> list:
        import uiautomation as auto
        import win32process

        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(history_hwnd)
            member_button = next(
                (
                    control
                    for control in self._walk(root)
                    if _text(control.Name) == "群成员"
                ),
                None,
            )
            if member_button is None:
                return []
            self._click_and_restore(member_button)
        self._paced_wait(
            "uia_history_settle_ms_min",
            "uia_history_settle_ms_max",
            800,
            1300,
        )
        _, process_id = win32process.GetWindowThreadProcessId(history_hwnd)
        with auto.UIAutomationInitializerInThread():
            for hwnd in self._visible_process_windows(process_id):
                root = auto.ControlFromHandle(hwnd)
                member_list = next(
                    (
                        control
                        for control in self._walk(root, 300)
                        if _text(control.AutomationId) == "chatroom_member_list"
                    ),
                    None,
                )
                if member_list is None:
                    continue
                return [
                    item
                    for item in member_list.GetChildren()
                    if _text(item.ClassName) == "mmui::XTableCell"
                    and _text(item.Name) == _text(owner)
                ]
        return []

    def _filtered_history_for_member(
        self, history_hwnd: int, owner: str, candidate_index: int
    ) -> tuple[int, list[str]]:
        import uiautomation as auto
        import win32process

        candidates = self._history_member_candidates(history_hwnd, owner)
        candidate_count = len(candidates)
        if not 0 <= int(candidate_index) < candidate_count:
            return candidate_count, []
        self._click_and_restore(candidates[int(candidate_index)])
        self._paced_wait(
            "uia_history_settle_ms_min",
            "uia_history_settle_ms_max",
            800,
            1300,
        )
        _, process_id = win32process.GetWindowThreadProcessId(history_hwnd)
        with auto.UIAutomationInitializerInThread():
            for hwnd in self._visible_process_windows(process_id):
                root = auto.ControlFromHandle(hwnd)
                message_list = next(
                    (
                        control
                        for control in self._walk(root, 1000)
                        if _text(control.AutomationId)
                        in {"chat_log_message_list", "search_message_list"}
                    ),
                    None,
                )
                if message_list is None:
                    continue
                return candidate_count, [
                    self._history_message_content(_text(item.Name))
                    for item in message_list.GetChildren()
                    if _text(item.ClassName).casefold() != "mmui::chatitemview"
                    and "Chat" in _text(item.ClassName)
                    and "ItemView" in _text(item.ClassName)
                    and _text(item.Name)
                ]
        return candidate_count, []

    def resolve_group_message_directions(
        self,
        conversation: str,
        messages: list[UiaChatMessage],
        owner: str,
    ) -> list[UiaChatMessage]:
        """Resolve unknown directions through the history member filter."""
        self.last_direction_resolution = {
            "conversation": _text(conversation),
            "status": "not_started",
        }
        if not owner or not any(item.direction == "unknown" for item in messages):
            self.last_direction_resolution["status"] = "not_needed"
            return messages
        import win32con
        import win32gui

        with self.operation_lock:
            owner_hwnd = self.get_owner_window_handle()
            process_id = self.get_owner_window_process_id()
            with self._uia_root() as root:
                history_button = next(
                    (
                        control
                        for control in self._walk(root)
                        if _text(control.Name) == "聊天记录"
                        and _text(control.ControlTypeName) == "ButtonControl"
                    ),
                    None,
                )
                if history_button is None:
                    self.last_direction_resolution["status"] = "history_button_missing"
                    return messages
                self._click_and_restore(history_button)
            self._paced_wait(
                "uia_history_settle_ms_min",
                "uia_history_settle_ms_max",
                1000,
                1600,
            )
            history_hwnd = next(
                (
                    hwnd
                    for hwnd in self._visible_process_windows(process_id)
                    if hwnd != owner_hwnd
                    and "聊天记录" in win32gui.GetWindowText(hwnd)
                ),
                None,
            )
            if history_hwnd is None:
                self.last_direction_resolution["status"] = "history_window_missing"
                return messages
            try:
                # Selecting a member closes the popover, so the first filtered
                # read also supplies the candidate count; reopen it only for
                # subsequent candidates.
                candidate_count, first_history = self._filtered_history_for_member(
                    history_hwnd, owner, 0
                )
                if candidate_count == 0:
                    self.last_direction_resolution.update(
                        status="owner_member_missing", candidate_count=0
                    )
                    return messages
                histories = [first_history]
                for index in range(1, candidate_count):
                    _, history = self._filtered_history_for_member(
                        history_hwnd, owner, index
                    )
                    histories.append(history)
                self_index = self._choose_self_member(
                    histories,
                    self._known_outgoing_texts.get(_text(conversation), []),
                )
                self.last_direction_resolution.update(
                    candidate_count=candidate_count,
                    filtered_history_counts=[len(history) for history in histories],
                    status="resolved" if self_index is not None else "self_ambiguous",
                )
                if self_index is None:
                    return messages
                resolved = self._apply_self_history_directions(
                    messages, histories[self_index]
                )
                if not histories[self_index]:
                    self.last_direction_resolution["status"] = "filtered_history_empty"
                return resolved
            finally:
                for hwnd in self._visible_process_windows(process_id):
                    if hwnd == owner_hwnd or not win32gui.IsWindow(hwnd):
                        continue
                    title = win32gui.GetWindowText(hwnd)
                    class_name = win32gui.GetClassName(hwnd)
                    if "聊天记录" in title or class_name.endswith("QWindowToolSaveBits"):
                        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                try:
                    win32gui.SetForegroundWindow(owner_hwnd)
                except Exception:
                    pass

    @classmethod
    def _message_direction(cls, item, list_bounds) -> tuple[str, Optional[tuple[int, int, int, int]]]:
        """Classify a message using UI geometry only, never display names.

        WeChat often exposes ChatItemView as a full-width row.  The visible
        bubble/avatar descendants, however, remain aligned to one side.  Vote
        using those bounded descendants and fail closed when geometry is
        ambiguous.
        """
        if not list_bounds:
            return "unknown", _bounds(item)
        list_left, _, list_right, _ = list_bounds
        width = max(1, list_right - list_left)
        center = (list_left + list_right) / 2.0
        threshold = max(12.0, width * 0.06)
        content = _text(item.Name)
        candidates = []
        item_bounds = _bounds(item)
        controls = [item]
        try:
            controls.extend(cls._walk(item, 200))
        except Exception:
            pass
        for control in controls:
            bounds = _bounds(control)
            if not bounds:
                continue
            left, top, right, bottom = bounds
            candidate_width = right - left
            if candidate_width <= 0 or bottom <= top or candidate_width >= width * 0.82:
                continue
            midpoint = (left + right) / 2.0
            if abs(midpoint - center) <= threshold:
                continue
            class_name = _text(control.ClassName).casefold()
            name = _text(control.Name)
            weight = 1.0
            if name and name == content:
                weight += 4.0
            if any(token in class_name for token in ("bubble", "avatar", "message", "content", "text")):
                weight += 2.0
            weight += min(2.0, candidate_width / max(1.0, width * 0.25))
            side = "incoming" if midpoint < center else "outgoing"
            candidates.append((side, weight, bounds))
        if not candidates and item_bounds:
            midpoint = (item_bounds[0] + item_bounds[2]) / 2.0
            if midpoint < center - threshold:
                return "incoming", item_bounds
            if midpoint > center + threshold:
                return "outgoing", item_bounds
            return "unknown", item_bounds
        incoming_score = sum(weight for side, weight, _ in candidates if side == "incoming")
        outgoing_score = sum(weight for side, weight, _ in candidates if side == "outgoing")
        if incoming_score == outgoing_score or max(incoming_score, outgoing_score) < 1.0:
            return "unknown", item_bounds
        winning = "incoming" if incoming_score > outgoing_score else "outgoing"
        if min(incoming_score, outgoing_score) and max(incoming_score, outgoing_score) < min(incoming_score, outgoing_score) * 1.5:
            return "unknown", item_bounds
        winning_bounds = [bounds for side, _, bounds in candidates if side == winning]
        anchor = max(winning_bounds, key=lambda value: (value[2] - value[0]) * (value[3] - value[1]))
        return winning, anchor

    def get_chat_history(
        self,
        conversation: Optional[str] = None,
        limit: int = 0,
        runtime_id: str = "",
        row_index: int = -1,
    ) -> list[UiaChatMessage]:
        requested_limit = int(limit)
        bounded_limit = 0 if requested_limit <= 0 else min(requested_limit, 20)
        if conversation and not self.locate_conversation(
            conversation, runtime_id=runtime_id, row_index=row_index
        ):
            raise RuntimeError(f"Conversation is not visible: {conversation}")
        owner = self.get_owner_info().nick_name
        header = self.get_title()
        messages: list[UiaChatMessage] = []
        with self.operation_lock, self._uia_root() as root:
            message_list = None
            for control in self._walk(root):
                if _text(control.AutomationId) == MESSAGE_LIST_ID:
                    message_list = control
                    break
            if message_list is None:
                raise RuntimeError("WeChat message list is unavailable")
            list_bounds = _bounds(message_list)
            for item in message_list.GetChildren():
                class_name = _text(item.ClassName)
                content = _text(item.Name)
                if "Chat" not in class_name or "ItemView" not in class_name or not content:
                    continue
                # Generic ChatItemView rows are time separators and system
                # notices.  Concrete messages use ChatTextItemView,
                # ChatImageItemView, ChatFileItemView, and similar subclasses.
                if class_name.casefold() == "mmui::chatitemview":
                    continue
                direction, item_bounds = self._message_direction(item, list_bounds)
                sender = self._message_sender(
                    direction, header, owner, item, content
                )
                messages.append(
                    UiaChatMessage(
                        sender_name=sender,
                        content=content,
                        message_type=self._message_type(class_name, content),
                        direction=direction,
                        runtime_id=_runtime_id(item),
                        bounds=item_bounds,
                    )
                )
        return messages if bounded_limit == 0 else messages[-bounded_limit:]

    @staticmethod
    def _same_bounds(left, right, tolerance: int = 3) -> bool:
        if not left or not right:
            return False
        return all(abs(int(a) - int(b)) <= tolerance for a, b in zip(left, right))

    def _find_message_control(self, root, message: UiaChatMessage):
        """Reacquire a visible message control without trusting recyclable IDs alone."""
        candidates = []
        for control in self._walk(root):
            class_name = _text(control.ClassName)
            if "Chat" not in class_name or "ItemView" not in class_name:
                continue
            if self._message_type(class_name, _text(control.Name)) != message.message_type:
                continue
            name_matches = _text(control.Name) == _text(message.content)
            bounds_match = self._same_bounds(_bounds(control), message.bounds)
            runtime_matches = bool(
                message.runtime_id and _runtime_id(control) == message.runtime_id
            )
            if name_matches and (bounds_match or runtime_matches):
                return control
            if name_matches:
                candidates.append(control)
        return candidates[0] if len(candidates) == 1 else None

    def _copy_control_file_paths(self, control) -> list[str]:
        """Copy a file bubble and return CF_HDROP paths, restoring the clipboard."""
        import win32api
        import win32clipboard
        import win32con

        saved = []
        win32clipboard.OpenClipboard()
        try:
            for fmt in (win32con.CF_UNICODETEXT, win32con.CF_TEXT, win32con.CF_HDROP):
                try:
                    if win32clipboard.IsClipboardFormatAvailable(fmt):
                        saved.append((fmt, win32clipboard.GetClipboardData(fmt)))
                except Exception:
                    pass
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()

        paths = []
        try:
            self._click_and_restore(control)
            self._paced_wait(
                "uia_file_selection_settle_ms_min",
                "uia_file_selection_settle_ms_max",
                100,
                200,
            )
            self._press_shortcut(win32api, win32con, ord("C"))
            self._paced_wait(
                "uia_file_clipboard_settle_ms_min",
                "uia_file_clipboard_settle_ms_max",
                200,
                400,
            )
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_HDROP):
                    paths = [
                        str(path)
                        for path in win32clipboard.GetClipboardData(win32con.CF_HDROP)
                        if str(path)
                    ]
            finally:
                win32clipboard.CloseClipboard()
        finally:
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                for fmt, value in saved:
                    try:
                        if fmt == win32con.CF_UNICODETEXT:
                            win32clipboard.SetClipboardText(value, fmt)
                        else:
                            win32clipboard.SetClipboardData(fmt, value)
                    except Exception:
                        pass
            finally:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass
        return paths

    @classmethod
    def _download_button(cls, file_control):
        for control in cls._walk(file_control, 100):
            name = _text(control.Name).casefold()
            if name not in {"下载", "download"}:
                continue
            control_type = _text(control.ControlTypeName).casefold()
            class_name = _text(control.ClassName).casefold()
            if "button" in control_type or "button" in class_name:
                return control
        return None

    @staticmethod
    def _store_file_in_tmp(source: str, tmp_root: Path) -> str:
        try:
            source_path = Path(source).resolve()
            if not source_path.is_file():
                return ""
            stat = source_path.stat()
        except OSError:
            return ""
        destination_root = tmp_root.resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        identity = f"{source_path}\0{stat.st_size}\0{stat.st_mtime_ns}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source_path.name).strip(" .")
        safe_name = safe_name or "wechat-file"
        target = destination_root / f"{digest}_{safe_name}"
        if source_path != target and (
            not target.exists() or target.stat().st_size != stat.st_size
        ):
            shutil.copy2(source_path, target)
        return str(target)

    def _wechat_data_roots(self) -> list[Path]:
        roots = []
        configured = _text(self.config.get("wechat_files_dir"))
        if configured:
            roots.append(Path(os.path.expandvars(os.path.expanduser(configured))))
        roots.extend(
            [
                Path.home() / "Documents" / "xwechat_files",
                Path.home() / "Documents" / "WeChat Files",
            ]
        )
        try:
            import win32api
            import win32con
            import win32process

            _hwnd, process_id = self._window()
            process = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False,
                process_id,
            )
            try:
                executable = Path(win32process.GetModuleFileNameEx(process, 0))
            finally:
                process.Close()
            roots.append(executable.parent.parent / "Documents" / "xwechat_files")
        except Exception:
            pass
        unique = []
        seen = set()
        for root in roots:
            try:
                resolved = root.resolve()
            except OSError:
                continue
            key = os.path.normcase(str(resolved))
            if key not in seen and resolved.is_dir():
                seen.add(key)
                unique.append(resolved)
        return unique

    def _find_cached_message_file(self, content: str) -> str:
        filename, expected_size = self._file_card_metadata(content)
        if not filename:
            return ""
        original = Path(filename)
        duplicate_name = re.compile(
            rf"^{re.escape(original.stem)}(?:\s*\(\d+\))?{re.escape(original.suffix)}$",
            re.IGNORECASE,
        )
        candidates = []
        for root in self._wechat_data_roots():
            patterns = (
                "*/msg/file/*/*",
                "*/FileStorage/File/*/*",
            )
            for pattern in patterns:
                try:
                    paths = root.glob(pattern)
                    for path in paths:
                        if not path.is_file() or not duplicate_name.fullmatch(path.name):
                            continue
                        stat = path.stat()
                        size_gap = (
                            abs(stat.st_size - expected_size)
                            if expected_size is not None
                            else 0
                        )
                        if expected_size is not None and size_gap > max(
                            2048, int(expected_size * 0.02)
                        ):
                            continue
                        candidates.append((size_gap, -stat.st_mtime_ns, path))
                except OSError:
                    continue
        if not candidates:
            return ""
        return str(min(candidates, key=lambda item: (item[0], item[1]))[2])

    def fetch_message_file(
        self, message: UiaChatMessage, tmp_root: Optional[Path] = None
    ) -> str:
        """Copy a visible WeChat file bubble's local file into project tmp."""
        if message.message_type != "file":
            return ""
        target_root = tmp_root or (
            Path(__file__).resolve().parents[2] / "tmp" / "wechat_files"
        )
        timeout = (
            max(0.0, min(float(self.config.get("uia_file_download_timeout_seconds", 10)), 30.0))
            if bool(self.config.get("uia_file_download_enabled", True))
            else 0.0
        )
        deadline = time.time() + timeout
        ui_attempted = False
        while True:
            cached = self._find_cached_message_file(message.content)
            if cached:
                stored = self._store_file_in_tmp(cached, Path(target_root))
                if stored:
                    logger.info("[WechatDesktop] cached file copied to tmp: %s", stored)
                    return stored
            if ui_attempted:
                if time.time() >= deadline:
                    break
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
                continue
            ui_attempted = True
            self.focus_window()
            with self.operation_lock, self._uia_root() as root:
                control = self._find_message_control(root, message)
                if control is None:
                    break
                paths = self._copy_control_file_paths(control)
                for path in paths:
                    try:
                        stored = self._store_file_in_tmp(path, Path(target_root))
                    except OSError as exc:
                        logger.warning(
                            "[WechatDesktop] failed to copy clipboard file to tmp: %s",
                            exc,
                        )
                        stored = ""
                    if stored:
                        logger.info("[WechatDesktop] file copied to tmp: %s", stored)
                        return stored
                if timeout > 0:
                    download = self._download_button(control)
                    if download is not None:
                        self._click_and_restore(download)
            if time.time() >= deadline:
                break
        logger.warning(
            "[WechatDesktop] file bubble was detected but no local file path was available: %s",
            message.content,
        )
        return ""

    def fetch_message_image(
        self, message: UiaChatMessage, tmp_root: Optional[Path] = None
    ) -> str:
        """Capture a visible image bubble into project ``tmp`` for vision input."""
        if message.message_type != "image" or not message.bounds:
            return ""
        left, top, right, bottom = (int(value) for value in message.bounds)
        if right <= left or bottom <= top:
            return ""
        target_root = (
            Path(tmp_root)
            if tmp_root is not None
            else Path(__file__).resolve().parents[2] / "tmp" / "wechat_images"
        )
        target_root.mkdir(parents=True, exist_ok=True)
        identity = "\0".join(
            (
                str(message.stable_id or message.runtime_id or message.content),
                str((left, top, right, bottom)),
            )
        )
        target = target_root / (
            hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16] + ".png"
        )
        try:
            # Bounding rectangles returned by UIA use virtual-screen coordinates.
            # Pillow's all_screens flag preserves negative coordinates on multi-monitor
            # Windows setups and captures only the image-card anchor, not the full row.
            from PIL import ImageGrab

            image = ImageGrab.grab(
                bbox=(left, top, right, bottom),
                all_screens=True,
            )
            if image.width < 2 or image.height < 2:
                return ""
            image.save(target, format="PNG")
            logger.info("[WechatDesktop] image bubble captured to tmp: %s", target)
            return str(target)
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] failed to capture image bubble bounds=%s: %s",
                message.bounds,
                exc,
            )
            return ""

    @contextmanager
    def _clipboard(self, unicode_text: Optional[str] = None, files: Optional[Iterable[str]] = None):
        import win32clipboard
        import win32con

        saved = []
        clipboard_error = ""
        win32clipboard.OpenClipboard()
        try:
            for fmt in (win32con.CF_UNICODETEXT, win32con.CF_TEXT, win32con.CF_HDROP):
                try:
                    if win32clipboard.IsClipboardFormatAvailable(fmt):
                        saved.append((fmt, win32clipboard.GetClipboardData(fmt)))
                except Exception:
                    pass
            win32clipboard.EmptyClipboard()
            if files is not None:
                win32clipboard.SetClipboardData(win32con.CF_HDROP, tuple(files))
            else:
                expected = str(unicode_text or "")
                win32clipboard.SetClipboardText(expected, win32con.CF_UNICODETEXT)
                actual = str(
                    win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
                )
                if actual != expected:
                    clipboard_error = (
                        "clipboard verification failed: "
                        f"expected_chars={len(expected)} actual_chars={len(actual)}"
                    )
        finally:
            win32clipboard.CloseClipboard()
        try:
            if clipboard_error:
                raise RuntimeError(clipboard_error)
            yield
        finally:
            try:
                win32clipboard.OpenClipboard()
                win32clipboard.EmptyClipboard()
                for fmt, value in saved:
                    try:
                        if fmt == win32con.CF_UNICODETEXT:
                            win32clipboard.SetClipboardText(value, fmt)
                        else:
                            win32clipboard.SetClipboardData(fmt, value)
                    except Exception:
                        pass
            finally:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:
                    pass

    @staticmethod
    def _input_value(control) -> str:
        _available, value = WechatUiaClient._try_input_value(control)
        return value

    @staticmethod
    def _try_input_value(control) -> tuple[bool, str]:
        try:
            pattern = control.GetValuePattern()
            if pattern is None:
                return False, ""
            return True, str(pattern.Value or "")
        except Exception:
            return False, ""

    def _wait_for_input_value(self, control, predicate, timeout: float) -> bool:
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if predicate(self._input_value(control)):
                return True
            time.sleep(0.05)
        return predicate(self._input_value(control))

    @staticmethod
    def _normalize_input_text(value: str) -> str:
        """Normalize harmless UIA text differences for paste diagnostics."""
        return (
            unicodedata.normalize("NFC", str(value or ""))
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\u00a0", " ")
            .replace("\u2007", " ")
            .replace("\u202f", " ")
        )

    @staticmethod
    def _split_message_text(value: str, limit: int) -> list[str]:
        """Split long replies at readable boundaries without exceeding limit."""
        text = str(value or "").strip()
        limit = max(100, int(limit))
        chunks = []
        while len(text) > limit:
            minimum_cut = max(1, int(limit * 0.6))
            cut = -1
            for marker in ("\n\n", "\n", "。", "！", "？", ";", "；", ",", "，"):
                candidate = text.rfind(marker, minimum_cut, limit + 1)
                if candidate >= minimum_cut:
                    candidate += len(marker)
                    cut = max(cut, candidate)
            if cut < minimum_cut:
                cut = limit
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()
        if text:
            chunks.append(text)
        return chunks

    @staticmethod
    def _keyboard_focus_state(control) -> Optional[bool]:
        try:
            return bool(control.HasKeyboardFocus)
        except Exception:
            return None

    def _wait_for_keyboard_focus(self, control, timeout: float = 0.25) -> Optional[bool]:
        state = self._keyboard_focus_state(control)
        if state is None or state:
            return state
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            time.sleep(0.025)
            state = self._keyboard_focus_state(control)
            if state is None or state:
                return state
        return self._keyboard_focus_state(control)

    def _press_shortcut(self, win32api, win32con, key: int) -> None:
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        self._paced_wait(
            "uia_key_event_settle_ms_min",
            "uia_key_event_settle_ms_max",
            20,
            40,
        )
        win32api.keybd_event(key, 0, 0, 0)
        win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
        self._paced_wait(
            "uia_key_event_settle_ms_min",
            "uia_key_event_settle_ms_max",
            20,
            40,
        )
        win32api.keybd_event(
            win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0
        )

    def _click_send_button(self, send_button):
        """Click the visible Send button while restoring the user's cursor.

        WeChat 4.1.9.30 exposes InvokePattern on this button, but some environments
        acknowledge Invoke() without actually sending. A real button click is
        therefore the primary operation.
        """
        import win32api

        cursor = None
        try:
            cursor = win32api.GetCursorPos()
        except Exception:
            pass
        try:
            send_button.Click(simulateMove=False, waitTime=0.1)
        finally:
            if cursor is not None:
                try:
                    win32api.SetCursorPos(cursor)
                except Exception:
                    pass

    def _paste_and_send(self, expected_text: str = ""):
        import win32api
        import win32con

        def paste_into(control) -> None:
            control.SetFocus()
            self._paced_wait(
                "uia_input_focus_settle_ms_min",
                "uia_input_focus_settle_ms_max",
                50,
                100,
            )
            focus_state = self._wait_for_keyboard_focus(control)
            if focus_state is False:
                logger.warning(
                    "[WechatDesktop] chat input did not report keyboard focus; "
                    "retrying SetFocus before paste"
                )
                control.SetFocus()
                self._paced_wait(
                    "uia_input_focus_settle_ms_min",
                    "uia_input_focus_settle_ms_max",
                    50,
                    100,
                )
            self._press_shortcut(win32api, win32con, ord("A"))
            self._press_shortcut(win32api, win32con, ord("V"))

        # Conversation selection and clipboard setup can take long enough for
        # another window to steal focus. Reassert WeChat immediately before
        # keyboard input and the physical Send-button click.
        attempts = max(1, min(int(self.config.get("uia_paste_attempts", 3)), 5))
        for attempt in range(1, attempts + 1):
            self.focus_window()
            with self._uia_root() as root:
                control = next(
                    (
                        item
                        for item in self._walk(root)
                        if _text(item.AutomationId) == INPUT_ID
                    ),
                    None,
                )
                if control is None:
                    raise RuntimeError("WeChat chat input is unavailable")

                # The desktop channel owns the input for this atomic operation.
                # Replace stale drafts and reacquire both the window and control
                # on every attempt so a transient focus loss cannot poison retries.
                paste_into(control)
                value_available, _value = self._try_input_value(control)
                actual_text = ""

                def capture_nonempty(value: str) -> bool:
                    nonlocal actual_text
                    actual_text = value
                    return bool(value)

                accepted = (
                    not expected_text
                    or not value_available
                    or self._wait_for_input_value(control, capture_nonempty, 1.25)
                )
                if not accepted:
                    try:
                        import win32gui

                        foreground_hwnd = int(win32gui.GetForegroundWindow() or 0)
                    except Exception:
                        foreground_hwnd = 0
                    logger.warning(
                        "[WechatDesktop] paste attempt %s/%s left input empty: "
                        "chars=%s foreground_hwnd=%s keyboard_focus=%s value_pattern=%s",
                        attempt,
                        attempts,
                        len(expected_text),
                        foreground_hwnd,
                        self._keyboard_focus_state(control),
                        value_available,
                    )
                    if attempt < attempts:
                        self._paced_wait(
                            "uia_paste_retry_ms_min",
                            "uia_paste_retry_ms_max",
                            150,
                            300,
                        )
                    continue

                if (
                    expected_text
                    and value_available
                    and self._normalize_input_text(actual_text)
                    != self._normalize_input_text(expected_text)
                ):
                    logger.warning(
                        "[WechatDesktop] pasted reply text differs from the source; "
                        "continuing because the input is non-empty: expected=%r actual=%r",
                        expected_text,
                        actual_text,
                    )
                self._paced_wait(
                    "uia_paste_settle_ms_min",
                    "uia_paste_settle_ms_max",
                    150,
                    300,
                )
                send_button = next(
                    (
                        item
                        for item in self._walk(root)
                        if _text(item.ClassName) == "mmui::XOutlineButton"
                        and _text(item.Name) in {"发送", "Send"}
                    ),
                    None,
                )
                if send_button is not None:
                    self._paced_wait(
                        "uia_pre_send_settle_ms_min",
                        "uia_pre_send_settle_ms_max",
                        100,
                        250,
                    )
                    self._click_send_button(send_button)
                else:
                    win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
                    win32api.keybd_event(
                        win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0
                    )
                if (
                    expected_text
                    and value_available
                    and not self._wait_for_input_value(
                        control, lambda value: not value, 1.5
                    )
                ):
                    raise RuntimeError("WeChat Send button did not clear the reply input")
                return

        raise RuntimeError(
            f"WeChat chat input remained empty after {attempts} attempts"
        )

    def send_message(
        self,
        who: str,
        message: str,
        runtime_id: str = "",
        row_index: int = -1,
    ) -> dict:
        text = str(message or "")
        if not text:
            raise ValueError("text is empty")
        chunk_limit = max(
            100,
            min(int(self.config.get("uia_text_chunk_chars", 500)), 2000),
        )
        chunks = self._split_message_text(text, chunk_limit)
        with self.operation_lock:
            logger.info(
                "[WechatDesktop] sending text: target=%s chars=%s chunks=%s "
                "chunk_lengths=%s content_hash=%s",
                who,
                len(text),
                len(chunks),
                [len(chunk) for chunk in chunks],
                hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
            )
            results = []
            for index, chunk in enumerate(chunks, 1):
                try:
                    self._wait_for_send_slot(who)
                    self.focus_window()
                    if not self.locate_conversation(who, runtime_id, row_index):
                        raise RuntimeError(f"Conversation is not visible: {who}")
                    before = self.get_chat_history(limit=5)
                    with self._clipboard(unicode_text=chunk):
                        self._paste_and_send(expected_text=chunk)
                    result = self._verify_send(who, before, text=chunk)
                    results.append(result)
                    now = time.monotonic()
                    self._last_send_at = now
                    self._last_conversation_send[str(who)] = now
                    if result.get("verified"):
                        self.remember_outgoing_text(who, chunk)
                except Exception as exc:
                    raise RuntimeError(
                        f"WeChat text chunk {index}/{len(chunks)} failed: {exc}"
                    ) from exc
            verified = all(result.get("verified") for result in results)
            return {
                "success": True,
                "verified": verified,
                "chunks": len(chunks),
                "message": "" if verified else "one or more outgoing chunks were not verified",
            }

    def send_file(
        self,
        who: str,
        files: Iterable[str],
        runtime_id: str = "",
        row_index: int = -1,
    ) -> dict:
        paths = [str(Path(path).resolve()) for path in files]
        if not paths or any(not Path(path).is_file() for path in paths):
            raise ValueError("one or more files do not exist")
        with self.operation_lock:
            self._wait_for_send_slot(who)
            self.focus_window()
            if not self.locate_conversation(who, runtime_id, row_index):
                raise RuntimeError(f"Conversation is not visible: {who}")
            before = self.get_chat_history(limit=5)
            with self._clipboard(files=paths):
                self._paste_and_send()
            result = self._verify_send(who, before, expected_type="file")
            now = time.monotonic()
            self._last_send_at = now
            self._last_conversation_send[str(who)] = now
            return result

    def _verify_send(
        self,
        who: str,
        before: list[UiaChatMessage],
        text: str = "",
        expected_type: str = "",
    ) -> dict:
        before_ids = {item.runtime_id for item in before if item.runtime_id}
        deadline = time.time() + 3.0
        while time.time() < deadline:
            time.sleep(0.15)
            after = self.get_chat_history(limit=5)
            for item in reversed(after):
                is_new = not item.runtime_id or item.runtime_id not in before_ids
                matches = (text and item.content == text) or (
                    expected_type and item.message_type in {expected_type, "image"}
                )
                if is_new and matches and item.direction in {"outgoing", "unknown"}:
                    return {
                        "success": True,
                        "verified": True,
                    }
        return {
            "success": True,
            "verified": False,
            "message": "UI action completed but the outgoing bubble was not verified",
        }
