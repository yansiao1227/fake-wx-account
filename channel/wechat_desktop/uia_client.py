"""Synchronous WeChat 4.1.9.30 client built on Windows UI Automation."""

from __future__ import annotations

import hashlib
import ctypes
import os
import random
import re
import shutil
import struct
import threading
import time
import unicodedata
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Iterator, Optional
from urllib.parse import urlparse

from common.log import file_logger, logger
from channel.wechat_desktop.group_sender_ocr import RapidOcrGroupSenderResolver
from channel.wechat_desktop.models import (
    ConversationInfo,
    DEFAULT_SELF_SENDER_NAME,
    HeaderInfo,
    OwnerInfo,
    UNKNOWN_SENDER_NAME,
    UiaChatMessage,
    UiaReferencedMessage,
)
from channel.wechat_desktop.operations import (
    conversation_titles_match,
    strip_member_count_suffix,
)

SESSION_PREFIX = "session_item_"
MESSAGE_LIST_ID = "chat_message_list"
INPUT_ID = "chat_input_field"
MENTION_MARKERS = ("[有人@我]", "有人@我", "[Someone mentioned me]")


def _text(value) -> str:
    return str(value or "").strip()


def _encode_cf_hdrop(files: Iterable[str]) -> bytes:
    """Encode file paths as the Windows DROPFILES payload used by CF_HDROP."""

    paths = [str(path) for path in files]
    if not paths:
        raise ValueError("CF_HDROP requires at least one file path")
    if any("\0" in path for path in paths):
        raise ValueError("CF_HDROP file paths cannot contain NUL characters")

    # DROPFILES is 20 bytes: pFiles, POINT(x, y), fNC, fWide. File names
    # follow as a double-NUL-terminated UTF-16LE list when fWide is true.
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    file_list = ("\0".join(paths) + "\0\0").encode("utf-16le")
    return header + file_list


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
        # Serializes concurrent senders (reply FIFO vs. agent-initiated sends)
        # so humanized pacing waits happen outside any UIA lock: a waiting
        # sender must never block conversation scanning or attachment reads.
        self._send_lock = threading.RLock()
        # Optional per-section UIA priority context installed by the driver.
        # Each bounded send section (one chunk's focus/locate/paste/verify)
        # runs inside ``uia_section()`` so scans yield during UI work but can
        # run freely while a sender sleeps between chunks.
        self.uia_section = nullcontext
        self._owner_cache: Optional[OwnerInfo] = None
        self._owner_lookup_retry_after = 0.0
        self._stop_event = threading.Event()
        self._last_send_at = 0.0
        self._last_conversation_send: dict[str, float] = {}
        self._known_outgoing_texts: dict[str, list[str]] = {}
        self._known_outgoing_messages: dict[
            str, list[tuple[str, str, float]]
        ] = {}
        # RapidOCR is preloaded at channel startup; first group scan should not
        # pay model-load latency. Enrich still falls back to on-demand load.
        self._group_sender_ocr = RapidOcrGroupSenderResolver(self.config)

    def preload_group_sender_ocr(self) -> bool:
        """Load RapidOCR models early (channel startup). Returns readiness."""
        return bool(self._group_sender_ocr.preload())

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

    def remember_outgoing_message(
        self, conversation: str, text: str = "", runtime_id: str = ""
    ) -> None:
        value = _text(text)
        runtime = _text(runtime_id)
        if not value and not runtime:
            return
        key = _text(conversation)
        if value:
            self.remember_outgoing_text(key, value)
        records = self._known_outgoing_messages.setdefault(key, [])
        records.append((value, runtime, time.monotonic()))
        del records[:-50]

    def is_known_outgoing_message(
        self, conversation: str, message: UiaChatMessage
    ) -> bool:
        key = _text(conversation)
        records = self._known_outgoing_messages.get(key, [])
        window = max(
            5.0,
            min(
                float(self.config.get("outgoing_echo_suppression_seconds", 300)),
                3600.0,
            ),
        )
        cutoff = time.monotonic() - window
        records[:] = [record for record in records if record[2] >= cutoff]
        runtime = _text(message.runtime_id)
        content = _text(message.content)
        return any(
            (runtime and record_runtime and runtime == record_runtime)
            or (content and record_text and content == record_text)
            for record_text, record_runtime, _created_at in records
        )

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
        activation_error = None
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception as first_error:
            current_thread = win32api.GetCurrentThreadId()
            foreground = win32gui.GetForegroundWindow()
            foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground)
            ctypes.windll.user32.AttachThreadInput(
                current_thread, foreground_thread, True
            )
            try:
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception as second_error:
                    activation_error = second_error
            finally:
                ctypes.windll.user32.AttachThreadInput(
                    current_thread, foreground_thread, False
                )
            if activation_error is not None and not self._click_taskbar_button():
                raise activation_error from first_error
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

    @staticmethod
    def _window_process_is_foreground(main_hwnd: int) -> bool:
        """Return whether the foreground window belongs to the main window process."""

        try:
            import win32gui
            import win32process

            foreground = int(win32gui.GetForegroundWindow() or 0)
            if not foreground or not main_hwnd:
                return False
            _, foreground_pid = win32process.GetWindowThreadProcessId(foreground)
            _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
            return bool(foreground_pid and int(foreground_pid) == int(main_pid))
        except Exception:
            return False

    def _recover_foreground_after_dependency_failure(
        self,
        context: str,
        main_hwnd: Optional[int] = None,
    ) -> bool:
        """Recover WeChat focus after a viewer, menu, or dialog operation failed."""

        try:
            owner_hwnd = int(main_hwnd or self.get_owner_window_handle())
            if self._window_process_is_foreground(owner_hwnd):
                return False
            activated = self.ensure_foreground_window()
            logger.info(
                "[WechatDesktop] restored WeChat foreground after %s; activated=%s",
                context,
                activated,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] failed to restore WeChat foreground after %s: %s",
                context,
                exc,
            )
            return False

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
                file_logger.info("[WechatDesktop][trace:05-owner] source=cache name=%s",
                    self._owner_cache.nick_name,
                )
            return self._owner_cache
        now = time.monotonic()
        if now < self._owner_lookup_retry_after:
            if bool(self.config.get("diagnostic_logging", False)):
                file_logger.info("[WechatDesktop][trace:05-owner] "
                    "source=failure_cache retry_in=%.3fs",
                    self._owner_lookup_retry_after - now,
                )
            return OwnerInfo("", source="unknown")
        discovered = ""
        wx_id = ""
        discovery_method = "none"
        if bool(self.config.get("diagnostic_logging", False)):
            file_logger.info("[WechatDesktop][trace:05-owner] lookup_start configured=%s cached=%s",
                bool(configured),
                bool(self._owner_cache and self._owner_cache.nick_name),
            )
        try:
            with self.operation_lock, self._uia_root() as root:
                root_bounds = _bounds(root)
                left_limit = (
                    root_bounds[0]
                    + max(100, (root_bounds[2] - root_bounds[0]) // 5)
                    if root_bounds
                    else 0
                )
                for control in self._walk(root):
                    aid = _text(control.AutomationId).casefold()
                    cls = _text(control.ClassName).casefold()
                    name = _text(control.Name)
                    bounds = _bounds(control)
                    profile_hint = any(
                        key in aid
                        for key in (
                            "self_avatar",
                            "owner_avatar",
                            "profile",
                            "account",
                            "userinfo",
                        )
                    ) or (
                        "avatar" in cls
                        and any(key in aid for key in ("self", "owner"))
                    )
                    in_sidebar = bool(
                        bounds and left_limit and bounds[0] <= left_limit
                    )
                    if (
                        profile_hint
                        and in_sidebar
                        and name
                        and name not in {"头像", "微信"}
                    ):
                        discovered = name
                        discovery_method = "sidebar"
                        break
                if not discovered:
                    if bool(self.config.get("diagnostic_logging", False)):
                        file_logger.info("[WechatDesktop][trace:05-owner] "
                            "sidebar_missing; opening_profile_popup"
                        )
                    discovered, wx_id = self._read_owner_profile_popup(root)
                    if discovered:
                        discovery_method = "profile_popup"
        except Exception as exc:
            logger.warning(
                "[WechatDesktop][trace:05-owner] lookup_failed error=%s",
                exc,
            )
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
        if discovered:
            self._owner_lookup_retry_after = 0.0
        elif not owner.nick_name:
            try:
                failure_cache_seconds = float(
                    self.config.get("uia_owner_failure_cache_seconds", 60.0)
                )
            except (TypeError, ValueError):
                failure_cache_seconds = 60.0
            self._owner_lookup_retry_after = time.monotonic() + max(
                0.0,
                min(failure_cache_seconds, 3600.0),
            )
        if bool(self.config.get("diagnostic_logging", False)):
            file_logger.info("[WechatDesktop][trace:05-owner] lookup_result available=%s source=%s method=%s name=%s",
                bool(owner.nick_name),
                owner.source,
                discovery_method if owner.source == "uia" else owner.source,
                owner.nick_name or "<empty>",
            )
        return owner

    def _find_owner_profile_popup(
        self,
        main_hwnd: int,
        visible_before: Optional[set[int]] = None,
    ):
        """Return the profile popup without enumerating the desktop UIA tree.

        Desktop ``GetChildren()`` asks every top-level window's UIA provider for
        data.  A single unrelated, unresponsive provider can therefore block the
        WeChat scan thread for close to a minute.  Native window enumeration is
        cheap; only same-process candidates are converted to UIA controls.
        """

        import uiautomation as auto
        import win32gui
        import win32process

        _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
        try:
            timeout_seconds = float(
                self.config.get("uia_owner_lookup_timeout_seconds", 2.0)
            )
        except (TypeError, ValueError):
            timeout_seconds = 2.0
        deadline = time.monotonic() + max(0.0, min(timeout_seconds, 5.0))

        while True:
            candidates: list[int] = []

            def callback(hwnd, _):
                native_hwnd = int(hwnd or 0)
                if (
                    not native_hwnd
                    or native_hwnd == int(main_hwnd)
                    or not win32gui.IsWindowVisible(native_hwnd)
                ):
                    return True
                try:
                    _, process_id = win32process.GetWindowThreadProcessId(
                        native_hwnd
                    )
                except Exception:
                    return True
                if int(process_id) == int(main_pid):
                    candidates.append(native_hwnd)
                return True

            win32gui.EnumWindows(callback, None)
            # Prefer a window created by the avatar click, but also accept an
            # already-open profile popup left behind by a previous attempt.
            if visible_before is not None:
                candidates.sort(key=lambda hwnd: hwnd in visible_before)
            # Do not enter COM/UIA inside EnumWindows' callback. Qt may
            # synchronously wait on its UI thread while the popup is created.
            for hwnd in candidates:
                try:
                    popup = auto.ControlFromHandle(hwnd)
                    if _text(popup.ClassName) == "mmui::ProfileUniquePop":
                        return popup
                except Exception as exc:
                    if bool(self.config.get("diagnostic_logging", False)):
                        file_logger.info("[WechatDesktop][trace:05-owner] "
                            "profile_popup_candidate_failed hwnd=%s error=%s",
                            hwnd,
                            exc,
                        )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if self._stop_event.wait(min(0.05, remaining)):
                raise RuntimeError("WeChat UI Automation is stopping")

    def _read_owner_profile_popup(self, root) -> tuple[str, str]:
        """Open the unlabelled top-left avatar and read its semantic popup."""
        started_at = time.monotonic()
        try:
            import uiautomation as auto
            import win32api
            import win32con

            bounds = _bounds(root)
            if not bounds:
                return "", ""
            main_hwnd = self.get_owner_window_handle()
            visible_before = self._visible_top_level_window_handles()
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

            popup = self._find_owner_profile_popup(main_hwnd, visible_before)
            if popup is None:
                if bool(self.config.get("diagnostic_logging", False)):
                    file_logger.info("[WechatDesktop][trace:05-owner] "
                        "profile_popup_not_found elapsed=%.3fs",
                        time.monotonic() - started_at,
                    )
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
            if bool(self.config.get("diagnostic_logging", False)):
                file_logger.info("[WechatDesktop][trace:05-owner] "
                    "profile_popup_read available=%s elapsed=%.3fs",
                    bool(display_name),
                    time.monotonic() - started_at,
                )
            return display_name, wx_id
        except Exception as exc:
            logger.warning(
                "[WechatDesktop][trace:05-owner] "
                "profile_popup_read_failed elapsed=%.3fs error=%s",
                time.monotonic() - started_at,
                exc,
            )
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
    def _read_active_chat_state(cls, root) -> tuple[str, object | None]:
        """Return ``(current_title, message_list_control_or_None)`` for the open chat."""
        current_title = ""
        message_list = None
        for control in cls._walk(root):
            automation_id = _text(control.AutomationId)
            if automation_id.endswith("current_chat_name_label"):
                current_title = _text(control.Name)
            elif automation_id == MESSAGE_LIST_ID:
                message_list = control
        return current_title, message_list

    @classmethod
    def _active_conversation_has_content(cls, root, target: str) -> bool:
        """Return whether *target* is already open with a populated chat pane."""
        current_title, message_list = cls._read_active_chat_state(root)
        if message_list is None or not conversation_titles_match(current_title, target):
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

    @classmethod
    def _active_conversation_matches(cls, root, target: str) -> bool:
        """Return whether the open detail pane is *target* with a message list."""
        current_title, message_list = cls._read_active_chat_state(root)
        return bool(
            message_list is not None
            and conversation_titles_match(current_title, target)
        )

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
                #
                # Critical: a visible message list alone is NOT enough. If the
                # click fails to switch the detail pane, the previous chat's
                # message list remains visible and we must not claim success
                # (that would paste daily-hot / replies into the wrong chat).
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
                    if self._active_conversation_matches(root, target):
                        return True
        return False

    def get_title(self) -> HeaderInfo:
        title = ""
        count = 1
        count_from_label = False
        with self.operation_lock, self._uia_root() as root:
            for control in self._walk(root):
                automation_id = _text(control.AutomationId)
                if automation_id.endswith("current_chat_name_label"):
                    title = _text(control.Name)
                elif automation_id.endswith("current_chat_count_label"):
                    match = re.search(r"(\d+)", _text(control.Name))
                    if match:
                        count = max(1, int(match.group(1)))
                        count_from_label = True
        # Some builds put the member count only in a dedicated label; others
        # also (or instead) append "(n)" to the name control. Prefer the bare
        # group name so callers compare cleanly with session_item_* / config.
        base = strip_member_count_suffix(title)
        if base and base != title:
            if not count_from_label:
                match = re.search(r"[（(](\d+)[）)]\s*$", title)
                if match:
                    count = max(1, int(match.group(1)))
            title = base
        return HeaderInfo(
            title,
            "group" if count > 1 else ("private" if title else "unknown"),
            count,
        )

    @staticmethod
    def _message_type(class_name: str, content: str) -> str:
        class_value = _text(class_name).casefold()
        content_value = _text(content)
        if WechatUiaClient._share_card_metadata(content_value)[0]:
            return "share_card"
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
    def _share_card_metadata(content: str) -> tuple[str, str]:
        """Return a WeChat share-card title and optional platform label."""

        lines = [line.strip() for line in _text(content).splitlines() if line.strip()]
        if not lines:
            return "", ""
        first = lines[0]
        marker = next(
            (item for item in ("[链接]", "[link]") if first.casefold().startswith(item.casefold())),
            "",
        )
        if not marker:
            return "", ""
        title = first[len(marker) :].strip()
        remaining = lines[1:]
        if not title and remaining:
            title, remaining = remaining[0], remaining[1:]
        if not title:
            return "", ""
        return title, remaining[-1] if remaining else ""

    @staticmethod
    def _normalized_reference_text(value: str) -> str:
        value = unicodedata.normalize("NFKC", _text(value)).casefold()
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)

    @classmethod
    def _share_card_matches_preview(cls, content: str, preview: str) -> bool:
        title, _platform = cls._share_card_metadata(content)
        title_value = cls._normalized_reference_text(title)
        preview_value = cls._normalized_reference_text(preview)
        return bool(
            title_value
            and preview_value
            and (
                title_value == preview_value
                or title_value in preview_value
                or preview_value in title_value
            )
        )

    @staticmethod
    def _parse_reference_label(content: str) -> Optional[dict]:
        match = re.match(
            r"^(?P<current>.*?)引用\s+(?P<sender>.+?)\s+的消息\s*[:：]\s*(?P<quoted>.*)$",
            _text(content),
            re.DOTALL,
        )
        if not match:
            return None
        return {
            "current": match.group("current").strip(),
            "sender": match.group("sender").strip(),
            "preview": match.group("quoted").strip(),
        }

    @classmethod
    def _reference_message_type(cls, preview: str) -> str:
        value = _text(preview)
        folded = value.casefold()
        if folded in {"图片", "image"}:
            return "image"
        if re.search(
            r"\.(?:docx?|pdf|xlsx?|pptx?|txt|md|csv|zip|rar|7z)$",
            value,
            re.IGNORECASE,
        ):
            return "file"
        if folded in {"文件", "file"}:
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

    def _self_sender_name(self) -> str:
        return (
            _text(self.config.get("self_display_name"))
            or DEFAULT_SELF_SENDER_NAME
        )

    def _message_sender(
        self,
        header: HeaderInfo,
        item,
        content: str,
        direction: str,
    ) -> str:
        """按会话类型和方向生成 OCR 前的发送者占位值。"""
        del item, content
        if direction == "outgoing":
            return self._self_sender_name()
        if direction != "incoming":
            return UNKNOWN_SENDER_NAME
        if header.header_type == "private":
            return _text(header.title) or UNKNOWN_SENDER_NAME
        return UNKNOWN_SENDER_NAME

    def _normalize_private_senders(
        self,
        header: HeaderInfo,
        messages: list[UiaChatMessage],
    ) -> list[UiaChatMessage]:
        """私聊不读取用户名，只根据最终方向映射为自己、对方或 unknown。"""

        normalized = []
        for message in messages:
            if message.direction == "outgoing":
                sender = self._self_sender_name()
            elif message.direction == "incoming":
                sender = _text(header.title) or UNKNOWN_SENDER_NAME
            else:
                sender = UNKNOWN_SENDER_NAME
            normalized.append(replace(message, sender_name=sender))
        return normalized

    @staticmethod
    def _click_and_restore(control) -> None:
        import win32api

        cursor = win32api.GetCursorPos()
        try:
            control.Click()
        finally:
            win32api.SetCursorPos(cursor)

    def get_chat_history(
        self,
        conversation: Optional[str] = None,
        limit: int = 0,
        runtime_id: str = "",
        row_index: int = -1,
        ensure_conversation: bool = True,
    ) -> list[UiaChatMessage]:
        requested_limit = int(limit)
        bounded_limit = 0 if requested_limit <= 0 else min(requested_limit, 20)
        if ensure_conversation and conversation and not self.locate_conversation(
            conversation, runtime_id=runtime_id, row_index=row_index
        ):
            raise RuntimeError(f"Conversation is not visible: {conversation}")
        header = self.get_title()
        if (
            not ensure_conversation
            and conversation
            and not conversation_titles_match(header.title, conversation)
        ):
            return []
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
                direction = "unknown"
                item_bounds = _bounds(item)
                parsed_reference = self._parse_reference_label(content)
                reference = None
                if parsed_reference is not None:
                    content = parsed_reference["current"]
                    reference = UiaReferencedMessage(
                        sender_name=parsed_reference["sender"],
                        content=parsed_reference["preview"],
                        message_type=self._reference_message_type(
                            parsed_reference["preview"]
                        ),
                        resolved=False,
                        degraded=True,
                        strategy="preview_only",
                    )
                sender = self._message_sender(
                    header, item, content, direction
                )
                messages.append(
                    UiaChatMessage(
                        sender_name=sender,
                        content=content,
                        message_type=self._message_type(class_name, content),
                        direction=direction,
                        runtime_id=_runtime_id(item),
                        bounds=item_bounds,
                        reference=reference,
                    )
                )
            if header.header_type in {"group", "private"}:
                messages = self._group_sender_ocr.enrich(
                    messages,
                    list_bounds,
                    resolve_sender_names=header.header_type == "group",
                )
            if header.header_type == "private":
                messages = self._normalize_private_senders(header, messages)
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
            control_name = _text(control.Name)
            name_matches = control_name == _text(message.content)
            if not name_matches and message.reference is not None:
                parsed = self._parse_reference_label(control_name)
                name_matches = bool(
                    parsed is not None
                    and parsed["current"] == _text(message.content)
                    and parsed["preview"] == _text(message.reference.content)
                    and (
                        not message.reference.sender_name
                        or parsed["sender"] == _text(message.reference.sender_name)
                    )
                )
            bounds_match = self._same_bounds(_bounds(control), message.bounds)
            runtime_matches = bool(
                message.runtime_id and _runtime_id(control) == message.runtime_id
            )
            if name_matches and (bounds_match or runtime_matches):
                return control
            if name_matches:
                candidates.append(control)
        return candidates[0] if len(candidates) == 1 else None

    @classmethod
    def _image_activation_bounds(cls, control, fallback_bounds):
        """选择图片本体的点击区域，避免用整行消息区域覆盖已有锚点。"""

        control_bounds = _bounds(control) if control is not None else None
        fallback = tuple(fallback_bounds) if fallback_bounds else None
        if fallback:
            if control_bounds is None:
                return fallback
            outer_left, outer_top, outer_right, outer_bottom = control_bounds
            inner_left, inner_top, inner_right, inner_bottom = fallback
            if (
                outer_left <= inner_left
                and outer_top <= inner_top
                and outer_right >= inner_right
                and outer_bottom >= inner_bottom
            ):
                return fallback

        image_bounds = []
        if control is not None:
            for child in cls._walk(control, 100):
                bounds = _bounds(child)
                if not bounds:
                    continue
                left, top, right, bottom = bounds
                if right <= left or bottom <= top:
                    continue
                class_name = _text(getattr(child, "ClassName", ""))
                content = _text(getattr(child, "Name", ""))
                if cls._message_type(class_name, content) == "image":
                    image_bounds.append(bounds)
        if image_bounds:
            return max(
                image_bounds,
                key=lambda bounds: (bounds[2] - bounds[0])
                * (bounds[3] - bounds[1]),
            )
        return control_bounds or fallback

    @staticmethod
    def _right_click_point(point: tuple[int, int]) -> None:
        import win32api
        import win32con

        cursor = win32api.GetCursorPos()
        try:
            win32api.SetCursorPos(point)
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
        finally:
            win32api.SetCursorPos(cursor)

    @staticmethod
    def _left_click_point(point: tuple[int, int]) -> None:
        import win32api
        import win32con

        cursor = win32api.GetCursorPos()
        try:
            win32api.SetCursorPos(point)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        finally:
            win32api.SetCursorPos(cursor)

    @classmethod
    def _click_control_point(cls, control) -> bool:
        """Physically click the centre of an already verified UIA control."""

        bounds = _bounds(control) if control is not None else None
        if not bounds or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            return False
        cls._left_click_point(
            (
                int((bounds[0] + bounds[2]) / 2),
                int((bounds[1] + bounds[3]) / 2),
            )
        )
        return True

    @staticmethod
    def _press_escape_key() -> None:
        import win32api
        import win32con

        win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
        win32api.keybd_event(
            win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0
        )

    @classmethod
    def _find_image_viewer_close_control(cls, root):
        """在图片查看器自身的 UIA 树中查找关闭按钮。

        微信 4.1.9.30 实测将其暴露为：
        ``Name=关闭``、``ClassName=mmui::XButton``、
        ``ControlTypeName=ButtonControl``。英文标题作为兼容项保留。
        """

        controls = [root, *cls._walk(root, 300)]
        return next(
            (
                control
                for control in controls
                if _text(getattr(control, "Name", "")).casefold()
                in {"关闭", "close"}
                and _text(getattr(control, "ClassName", ""))
                == "mmui::XButton"
                and _text(getattr(control, "ControlTypeName", ""))
                == "ButtonControl"
            ),
            None,
        )

    def _is_image_viewer_window(self, viewer_hwnd: int, main_hwnd: int) -> bool:
        """严格确认句柄属于微信图片查看器，绝不能把主窗口当作关闭目标。"""

        import win32gui
        import win32process

        if (
            not viewer_hwnd
            or not main_hwnd
            or int(viewer_hwnd) == int(main_hwnd or 0)
            or not win32gui.IsWindow(viewer_hwnd)
            or not win32gui.IsWindow(main_hwnd)
        ):
            return False
        try:
            _, viewer_pid = win32process.GetWindowThreadProcessId(viewer_hwnd)
            _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
            title = _text(win32gui.GetWindowText(viewer_hwnd))
        except Exception:
            return False
        return bool(
            int(viewer_pid) == int(main_pid)
            and title.casefold() in {"图片和视频", "images and videos"}
        )

    def _has_image_viewer_close_control(self, viewer_hwnd: int) -> bool:
        """确认候选窗口确实暴露了微信图片查看器自己的关闭按钮。"""

        try:
            import uiautomation as auto

            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(viewer_hwnd)
                return self._find_image_viewer_close_control(root) is not None
        except Exception:
            return False

    @staticmethod
    def _image_viewer_is_open(viewer_hwnd: int) -> bool:
        """Return whether the viewer still has a visible, non-minimized window.

        WeChat's Qt windows are commonly hidden and reused after Close instead
        of having their HWND destroyed.  ``IsWindow`` alone therefore reports
        a successfully closed viewer as alive and can make the caller click a
        stale close control a second time.
        """

        try:
            import win32gui

            return bool(
                viewer_hwnd
                and win32gui.IsWindow(viewer_hwnd)
                and win32gui.IsWindowVisible(viewer_hwnd)
                and not win32gui.IsIconic(viewer_hwnd)
            )
        except Exception:
            return False

    @staticmethod
    def _visible_top_level_window_handles() -> set[int]:
        import win32gui

        handles: set[int] = set()

        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                handles.add(int(hwnd))
            return True

        win32gui.EnumWindows(callback, None)
        return handles

    def _find_opened_image_viewer(
        self,
        main_hwnd: int,
        visible_before: set[int],
    ) -> int:
        """查找本次点击新打开的、经过原生窗口和 UIA 双重验证的查看器。"""

        import win32gui

        foreground = int(win32gui.GetForegroundWindow() or 0)
        native_candidates: list[int] = []

        def callback(hwnd, _):
            native_hwnd = int(hwnd or 0)
            if (
                native_hwnd
                and native_hwnd not in visible_before
                and win32gui.IsWindowVisible(native_hwnd)
                and self._is_image_viewer_window(native_hwnd, main_hwnd)
            ):
                native_candidates.append(native_hwnd)
            return True

        win32gui.EnumWindows(callback, None)
        # Do not enter COM/UIA from inside EnumWindows' native callback.  Some
        # WeChat builds synchronously wait on their UI thread while a new image
        # viewer is initializing, which can otherwise freeze this worker before
        # it ever reaches screenshot capture.
        candidates = [
            hwnd
            for hwnd in native_candidates
            if self._has_image_viewer_close_control(hwnd)
        ]
        if foreground in candidates:
            return foreground
        return candidates[0] if len(candidates) == 1 else 0

    def _try_close_image_viewer_with_uia(self, viewer_hwnd: int) -> bool:
        """通过查看器窗口句柄获取 UIA 根节点并操作关闭按钮。

        微信的 ``mmui::XButton`` 会暴露 InvokePattern，但部分版本调用 Invoke
        后不执行任何动作。因此 Invoke 后必须检查查看器是否仍可见；仍可见时再
        使用 UIA 控件自己的 Click，而不是把“未抛异常”误判成关闭成功。
        """

        import uiautomation as auto
        import win32api

        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(viewer_hwnd)
            close_button = self._find_image_viewer_close_control(root)
            if close_button is None:
                return False
            cursor = win32api.GetCursorPos()
            try:
                try:
                    close_button.GetInvokePattern().Invoke()
                except Exception:
                    pass
                invoke_deadline = time.monotonic() + 0.3
                while (
                    self._image_viewer_is_open(viewer_hwnd)
                    and time.monotonic() < invoke_deadline
                ):
                    time.sleep(0.03)
                if self._image_viewer_is_open(viewer_hwnd):
                    close_button.Click(simulateMove=False, waitTime=0.1)
            finally:
                win32api.SetCursorPos(cursor)
        if not self._image_viewer_is_open(viewer_hwnd):
            return True
        close_deadline = time.monotonic() + 1.0
        while (
            self._image_viewer_is_open(viewer_hwnd)
            and time.monotonic() < close_deadline
        ):
            time.sleep(0.05)
        return not self._image_viewer_is_open(viewer_hwnd)

    def _close_image_viewer(self, viewer_hwnd: int, main_hwnd: int) -> bool:
        """只点击图片查看器自己的关闭按钮，绝不关闭任何原生窗口。

        全局 Esc 和 WM_CLOSE 都可能在焦点或窗口身份变化时作用于微信主窗口，
        因而这里不提供键盘或窗口消息兜底。UIA 关闭失败时宁可留下查看器，
        也不能终止用户的微信会话。
        """

        import win32gui

        if not self._is_image_viewer_window(viewer_hwnd, main_hwnd):
            logger.warning(
                "[WechatDesktop] refused to close an unverified image viewer: "
                "viewer_hwnd=%s main_hwnd=%s",
                viewer_hwnd,
                main_hwnd,
            )
            return False

        try:
            closed = self._try_close_image_viewer_with_uia(viewer_hwnd)
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] image viewer UIA close failed: %s", exc
            )
            return False
        if not win32gui.IsWindow(main_hwnd):
            logger.error(
                "[WechatDesktop] main WeChat window disappeared while closing "
                "image viewer: main_hwnd=%s viewer_hwnd=%s",
                main_hwnd,
                viewer_hwnd,
            )
            return False
        if closed:
            logger.info(
                "[WechatDesktop] image viewer closed via its UIA control: hwnd=%s",
                viewer_hwnd,
            )
        return closed

    @staticmethod
    def _restore_main_window_after_viewer(main_hwnd: int) -> bool:
        """Restore the exact main window after the viewer has finished closing.

        A same-process foreground window is not sufficient here: Qt can retain
        the hidden viewer HWND after Close.  Restore and verify the original
        main HWND itself so it cannot be left behind another application.
        """

        import win32con
        import win32gui

        if not main_hwnd or not win32gui.IsWindow(main_hwnd):
            return False
        try:
            win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
            deadline = time.monotonic() + 1.0
            while True:
                if int(win32gui.GetForegroundWindow() or 0) == int(main_hwnd):
                    return bool(
                        win32gui.IsWindowVisible(main_hwnd)
                        and not win32gui.IsIconic(main_hwnd)
                    )
                win32gui.BringWindowToTop(main_hwnd)
                win32gui.SetForegroundWindow(main_hwnd)
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            return False
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] failed to restore main window after viewer: %s",
                exc,
            )
            return False

    @staticmethod
    def _press_end_key() -> None:
        import win32api
        import win32con

        win32api.keybd_event(win32con.VK_END, 0, 0, 0)
        win32api.keybd_event(win32con.VK_END, 0, win32con.KEYEVENTF_KEYUP, 0)

    @classmethod
    def _find_desktop_control(cls, name: str, class_name: str):
        import uiautomation as auto

        root = auto.GetRootControl()
        if _text(root.Name) == name and _text(root.ClassName) == class_name:
            return root
        return next(
            (
                control
                for control in cls._walk(root)
                if _text(control.Name) == name
                and _text(control.ClassName) == class_name
            ),
            None,
        )

    @classmethod
    def _pick_located_original_control(
        cls, message_list, message_type: str, preview: str
    ):
        list_bounds = _bounds(message_list)
        if not list_bounds:
            return None
        center_y = (list_bounds[1] + list_bounds[3]) / 2
        preview_folded = _text(preview).casefold()
        expected_classes = {
            "text": ("chattextitemview",),
            "file": ("chatbubbleitemview", "chatfileitemview"),
            "image": ("chatbubblereferitemview", "chatimageitemview"),
        }
        ranked = []
        preview_matches = []
        for control in message_list.GetChildren():
            class_name = _text(control.ClassName).casefold()
            if "chat" not in class_name or "itemview" not in class_name:
                continue
            bounds = _bounds(control)
            if not bounds:
                continue
            name = _text(control.Name)
            # The current quoted bubble may remain in the viewport and contains
            # the same preview text. It is never a valid original candidate.
            if cls._parse_reference_label(name) is not None:
                continue
            row_center = (bounds[1] + bounds[3]) / 2
            score = -abs(row_center - center_y)
            if any(
                expected in class_name
                for expected in expected_classes.get(message_type, ())
            ):
                score += 240
            if preview_folded and preview_folded in name.casefold():
                score += 320
            if message_type == "file" and cls._file_card_metadata(name)[0]:
                score += 200
            ranked.append((score, control))
            normalized_name = cls._normalized_reference_text(name)
            normalized_preview = cls._normalized_reference_text(preview)
            if normalized_preview and normalized_preview in normalized_name:
                preview_matches.append(control)
        # The locate bar normally centers the original, but a viewport can
        # contain repeated text. Never resolve an ambiguous quote by proximity.
        if len(preview_matches) == 1:
            return preview_matches[0]
        if len(preview_matches) > 1:
            return None
        return max(ranked, key=lambda item: item[0])[1] if ranked and not preview else None

    def _return_to_reference(self) -> bool:
        button = self._find_desktop_control(
            "回到引用位置", "mmui::UnreadBarView"
        )
        if button is None:
            return False
        if not self._click_control_point(button):
            return False
        self._paced_wait(
            "uia_reference_return_settle_ms_min",
            "uia_reference_return_settle_ms_max",
            300,
            600,
        )
        return True

    def _send_existing_input_with_enter(self, expected_text: str) -> bool:
        """Retry a failed Send-button click without pasting a duplicate."""
        import win32api
        import win32con

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
                return False
            value_available, value = self._try_input_value(control)
            if (
                value_available
                and self._normalize_input_text(value)
                != self._normalize_input_text(expected_text)
            ):
                return False
            try:
                control.SetFocus()
            except Exception:
                return False
            win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
            win32api.keybd_event(
                win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0
            )
            return True

    def resolve_message_reference(
        self, message: UiaChatMessage
    ) -> UiaChatMessage:
        """Locate one quoted original, classify it, and materialize it safely."""
        reference = message.reference
        if (
            reference is None
            or reference.message_type == "image"
            or not message.bounds
        ):
            return message
        left, top, right, bottom = message.bounds
        if right <= left or bottom <= top:
            return message
        point = (
            int(left + (right - left) * 0.25),
            int(top + (bottom - top) * 0.72),
        )
        located = False
        restored = False
        message_list_verified = False
        resolved_message = message
        original = None
        try:
            self.focus_window()
            with self.operation_lock, self._uia_root() as root:
                self._right_click_point(point)
                self._paced_wait(
                    "uia_reference_menu_settle_ms_min",
                    "uia_reference_menu_settle_ms_max",
                    250,
                    450,
                )
                menu_item = self._find_desktop_control(
                    "定位到原文位置", "mmui::XMenuView"
                )
                if menu_item is None:
                    logger.warning(
                        "[WechatDesktop] locate-original menu item unavailable; "
                        "leaving UI untouched"
                    )
                    return message
                if not self._click_control_point(menu_item):
                    return message
                located = True
                self._paced_wait(
                    "uia_reference_locate_settle_ms_min",
                    "uia_reference_locate_settle_ms_max",
                    500,
                    900,
                )
                message_list = next(
                    (
                        control
                        for control in self._walk(root)
                        if _text(control.AutomationId) == MESSAGE_LIST_ID
                    ),
                    None,
                )
                if message_list is None:
                    return message
                message_list_verified = True
                control = self._pick_located_original_control(
                    message_list, reference.message_type, reference.content
                )
                if control is None:
                    logger.warning(
                        "[WechatDesktop] located reference original is ambiguous"
                    )
                else:
                    content = _text(control.Name)
                    class_name = _text(control.ClassName)
                    original_type = self._message_type(class_name, content)
                    if original_type == "share_card" and not self._share_card_matches_preview(
                        content, reference.content
                    ):
                        logger.warning(
                            "[WechatDesktop] share-card title does not match quote preview"
                        )
                    else:
                        original = UiaChatMessage(
                            sender_name=reference.sender_name,
                            content=content,
                            message_type=original_type,
                            direction="unknown",
                            runtime_id=_runtime_id(control),
                            bounds=_bounds(control),
                        )
            if original is not None:
                # The original must remain visible for attachment extraction,
                # but nested UIA initializers are avoided by leaving the tree.
                file_path = ""
                if original.message_type == "image":
                    file_path = self.fetch_message_image(original)
                elif original.message_type == "file":
                    file_path = self.fetch_message_file(original)
                title, platform = self._share_card_metadata(original.content)
                share_url = ""
                browser_content = ""
                if original.message_type == "share_card" and original.bounds:
                    browser_content, share_url = self.fetch_share_page_from_point(
                        self._share_card_activation_point(original.bounds)
                    )
                resolved_reference = UiaReferencedMessage(
                    sender_name=reference.sender_name,
                    content=(
                        title
                        if original.message_type == "share_card"
                        else original.content
                    ),
                    message_type=original.message_type,
                    file_path=file_path,
                    resolved=(
                        original.message_type != "share_card"
                        or bool(browser_content or share_url)
                    ),
                    degraded=(
                        original.message_type in {"image", "file"}
                        and not file_path
                    ),
                    strategy=(
                        "wechat_share_browser_direct_read"
                        if browser_content
                        else "wechat_share_browser_copy_link"
                        if share_url
                        else "wechat_locate_original"
                    ),
                    original_content=original.content,
                    url=share_url,
                    platform=platform,
                    browser_content=browser_content,
                    browser_status=("success" if browser_content else "unavailable"),
                )
                resolved_message = replace(message, reference=resolved_reference)
        except Exception as exc:
            logger.warning("[WechatDesktop] failed to resolve reference: %s", exc)
        finally:
            if located:
                try:
                    restored = self._return_to_reference()
                    if not restored and message_list_verified:
                        logger.warning(
                            "[WechatDesktop] return-to-reference control unavailable; "
                            "falling back to End"
                        )
                        self._press_end_key()
                        self._paced_wait(
                            "uia_reference_return_settle_ms_min",
                            "uia_reference_return_settle_ms_max",
                            300,
                            600,
                        )
                        restored = True
                except Exception as exc:
                    logger.warning(
                        "[WechatDesktop] failed to return to reference: %s", exc
                    )
        if (
            resolved_message.reference is not None
            and resolved_message.reference.message_type == "share_card"
        ):
            if (
                resolved_message.reference.browser_content
                or resolved_message.reference.url
            ):
                return resolved_message
            if not restored:
                return replace(
                    resolved_message,
                    reference=replace(
                        resolved_message.reference,
                        degraded=True,
                        strategy="wechat_locate_original_return_failed",
                    ),
                )
            browser_content, url = self.fetch_referenced_share_page(
                resolved_message
            )
            return replace(
                resolved_message,
                reference=replace(
                    resolved_message.reference,
                    url=url,
                    browser_content=browser_content,
                    browser_status=("success" if browser_content else "unavailable"),
                    resolved=bool(browser_content or url),
                    degraded=not bool(browser_content or url),
                    strategy=(
                        "wechat_share_browser_direct_read"
                        if browser_content
                        else "wechat_share_browser_copy_link"
                    ),
                ),
            )
        return resolved_message

    @staticmethod
    def _share_card_activation_point(
        bounds: tuple[int, int, int, int]
    ) -> tuple[int, int]:
        left, top, right, bottom = (int(value) for value in bounds)
        return (
            int(left + (right - left) * 0.25),
            int(top + (bottom - top) * 0.5),
        )

    @staticmethod
    def _is_safe_public_url(value: str) -> bool:
        parsed = urlparse(_text(value))
        return bool(
            parsed.scheme.casefold() in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        )

    @classmethod
    def _find_share_browser_controls(cls, root):
        controls = [root, *cls._walk(root, 800)]
        root_bounds = _bounds(root)
        more_candidates = []
        close_candidates = []
        for control in controls:
            name = _text(getattr(control, "Name", "")).casefold()
            control_type = _text(
                getattr(control, "ControlTypeName", "")
            ).casefold()
            bounds = _bounds(control)
            if not bounds:
                continue
            if name in {"关闭", "close"} and "button" in control_type:
                close_candidates.append(control)
            if name in {
                "更多",
                "more",
                "更多选项",
                "more options",
                "...",
                "⋯",
            } and ("button" in control_type or "menuitem" in control_type):
                more_candidates.append(control)
        if root_bounds:
            left, top, right, bottom = root_bounds

            def upper_right(control):
                bounds = _bounds(control)
                if not bounds:
                    return False
                center_x = (bounds[0] + bounds[2]) / 2
                center_y = (bounds[1] + bounds[3]) / 2
                return center_x >= left + (right - left) * 0.65 and center_y <= top + (
                    bottom - top
                ) * 0.25

            more_candidates = [item for item in more_candidates if upper_right(item)]
            close_candidates = [
                item
                for item in close_candidates
                if upper_right(item)
                and (_bounds(item)[0] + _bounds(item)[2]) / 2
                >= left + (right - left) * 0.85
            ]
        return (
            more_candidates[0] if len(more_candidates) == 1 else None,
            max(
                close_candidates,
                key=lambda item: (_bounds(item)[0] + _bounds(item)[2]) / 2,
            )
            if close_candidates
            else None,
        )

    @staticmethod
    def _is_verified_share_browser_window(browser_hwnd: int, main_hwnd: int) -> bool:
        try:
            import win32api
            import win32con
            import win32gui
            import win32process

            if (
                not browser_hwnd
                or not main_hwnd
                or int(browser_hwnd) == int(main_hwnd)
                or not win32gui.IsWindow(browser_hwnd)
                or not win32gui.IsWindowVisible(browser_hwnd)
            ):
                return False
            _, browser_pid = win32process.GetWindowThreadProcessId(browser_hwnd)
            _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
            if int(browser_pid) == int(main_pid):
                return True
            process = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ,
                False,
                browser_pid,
            )
            try:
                executable = Path(
                    win32process.GetModuleFileNameEx(process, 0)
                ).resolve()
            finally:
                process.Close()
            normalized = str(executable).replace("/", "\\").casefold()
            return bool(
                executable.name.casefold() == "wechatappex.exe"
                and "\\tencent\\xwechat\\xplugin\\" in normalized
                and win32gui.GetClassName(browser_hwnd) == "Chrome_WidgetWin_0"
                and _text(win32gui.GetWindowText(browser_hwnd)).casefold()
                in {"微信", "wechat"}
            )
        except Exception:
            return False

    def _verified_share_browser_controls(self, browser_hwnd: int, main_hwnd: int):
        try:
            import uiautomation as auto

            if not self._is_verified_share_browser_window(
                browser_hwnd, main_hwnd
            ):
                return None, None
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(browser_hwnd)
                return self._find_share_browser_controls(root)
        except Exception:
            return None, None

    def _find_opened_share_browser(
        self, main_hwnd: int, visible_before: set[int]
    ) -> int:
        import win32gui

        native_candidates = []
        foreground = int(win32gui.GetForegroundWindow() or 0)

        def callback(hwnd, _):
            value = int(hwnd or 0)
            if (
                value
                and win32gui.IsWindowVisible(value)
                and (value not in visible_before or value == foreground)
            ):
                native_candidates.append(value)
            return True

        win32gui.EnumWindows(callback, None)
        # Direct page reading only needs a verified WeChatAppEx window. Menu
        # and close controls are optional and are resolved later for fallback.
        candidates = [
            value
            for value in native_candidates
            if self._is_verified_share_browser_window(value, main_hwnd)
        ]
        if foreground in candidates:
            return foreground
        return candidates[0] if len(candidates) == 1 else 0

    @staticmethod
    def _clipboard_unicode_after(action) -> str:
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
        value = ""
        try:
            action()
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(
                    win32con.CF_UNICODETEXT
                ):
                    value = _text(
                        win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                    )
            finally:
                win32clipboard.CloseClipboard()
        finally:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                for fmt, saved_value in saved:
                    try:
                        if fmt == win32con.CF_UNICODETEXT:
                            win32clipboard.SetClipboardText(saved_value, fmt)
                        else:
                            win32clipboard.SetClipboardData(fmt, saved_value)
                    except Exception:
                        pass
            finally:
                win32clipboard.CloseClipboard()
        return value

    def _click_copy_link_menu(self, browser_hwnd: int) -> bool:
        menu_item = self._find_desktop_control("复制链接", "mmui::XMenuView")
        if menu_item is None:
            try:
                import uiautomation as auto

                desktop = auto.GetRootControl()
                matches = [
                    control
                    for control in [desktop, *self._walk(desktop, 1200)]
                    if _text(getattr(control, "Name", "")).casefold()
                    in {"复制链接", "copy link"}
                    and "xmenu"
                    in _text(getattr(control, "ClassName", "")).casefold()
                ]
                menu_item = matches[0] if len(matches) == 1 else None
            except Exception:
                menu_item = None
        if menu_item is not None:
            return self._click_control_point(menu_item)
        # Some WebView builds do not expose menu text through UIA. OCR is only
        # allowed inside the already verified browser window and must recognize
        # the exact first-item label before a click is issued.
        try:
            import numpy as np
            import win32gui
            from PIL import ImageGrab

            bounds = tuple(int(value) for value in win32gui.GetWindowRect(browser_hwnd))
            image = ImageGrab.grab(bbox=bounds, all_screens=True)
            engine = self._group_sender_ocr._get_engine()
            if engine is None:
                return False
            output = engine(np.asarray(image))
            lines = self._group_sender_ocr._output_lines(output, (bounds[0], bounds[1]))
            matches = [
                line
                for line in lines
                if line.text.strip().casefold() in {"复制链接", "copy link"}
                and line.score >= 0.8
            ]
            if len(matches) != 1:
                return False
            line = matches[0]
            self._left_click_point(
                (
                    int((line.bounds[0] + line.bounds[2]) / 2),
                    int((line.bounds[1] + line.bounds[3]) / 2),
                )
            )
            return True
        except Exception as exc:
            logger.warning("[WechatDesktop] copy-link OCR fallback failed: %s", exc)
            return False

    def _close_share_browser(self, browser_hwnd: int, main_hwnd: int) -> bool:
        import win32con
        import win32gui

        # The HWND has already passed the strict WeChatAppEx verification.
        # Do not enumerate the dynamic WebView UIA tree again just to find its
        # titlebar: on feed pages that scan can block for tens of seconds.
        timeout = max(
            0.25,
            min(
                float(
                    self.config.get(
                        "uia_share_browser_close_timeout_seconds", 2
                    )
                ),
                5.0,
            ),
        )
        deadline = time.monotonic() + timeout
        closed = False
        for attempt in range(2):
            win32gui.PostMessage(browser_hwnd, win32con.WM_CLOSE, 0, 0)
            while time.monotonic() < deadline:
                if not (
                    win32gui.IsWindow(browser_hwnd)
                    and win32gui.IsWindowVisible(browser_hwnd)
                ):
                    closed = True
                    break
                if self._stop_event.wait(0.1):
                    break
            if closed:
                break
        restored = bool(closed and self._restore_main_window_after_viewer(main_hwnd))
        if restored:
            logger.info(
                "[WechatDesktop][share-browser] closed hwnd=%s", browser_hwnd
            )
        return restored

    def fetch_referenced_share_url(self, message: UiaChatMessage) -> str:
        """Open a verified share-card quote and copy its browser URL."""

        _content, url = self._fetch_referenced_share(message, direct_read=False)
        return url

    def fetch_referenced_share_page(
        self, message: UiaChatMessage
    ) -> tuple[str, str]:
        """Read a quoted share card in WeChat, falling back to its URL."""

        return self._fetch_referenced_share(message, direct_read=True)

    def _fetch_referenced_share(
        self, message: UiaChatMessage, *, direct_read: bool
    ) -> tuple[str, str]:
        """Open a verified share-card quote and return page text plus fallback URL."""

        reference = message.reference
        if (
            reference is None
            or reference.message_type != "share_card"
            or not message.bounds
        ):
            return "", ""
        try:
            self.focus_window()
            with self.operation_lock, self._uia_root() as root:
                control = self._find_message_control(root, message)
                click_bounds = _bounds(control) if control is not None else message.bounds
                if not click_bounds:
                    return "", ""
                point = self._reference_image_activation_point(click_bounds)
            return self._fetch_share_from_point(point, direct_read=direct_read)
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] failed to prepare share quote activation: %s",
                exc,
            )
            return "", ""

    def _read_share_browser_content(self, browser_hwnd: int) -> str:
        """Read the rendered document from WeChat UIA, then use clipboard fallback."""

        value = self._wait_for_share_browser_content(browser_hwnd)
        if value:
            return value
        logger.info(
            "[WechatDesktop][share-browser] UIA document text unavailable "
            "after load wait hwnd=%s; trying clipboard fallback",
            browser_hwnd,
        )

        try:
            import win32api
            import win32con
            import win32gui

            left, top, right, bottom = (
                int(value) for value in win32gui.GetWindowRect(browser_hwnd)
            )
            if right <= left or bottom <= top:
                return ""
            win32gui.SetForegroundWindow(browser_hwnd)
            # Focus below the browser chrome so Ctrl+A targets the rendered page.
            content_point = (
                int((left + right) / 2),
                int(top + (bottom - top) * 0.45),
            )
            self._left_click_point(content_point)
            self._paced_wait(
                "uia_share_browser_direct_read_settle_ms_min",
                "uia_share_browser_direct_read_settle_ms_max",
                300,
                600,
            )

            def copy_page():
                self._press_shortcut(win32api, win32con, ord("A"))
                self._press_shortcut(win32api, win32con, ord("C"))
                self._paced_wait(
                    "uia_share_browser_clipboard_settle_ms_min",
                    "uia_share_browser_clipboard_settle_ms_max",
                    200,
                    450,
                )

            value = self._normalize_share_browser_content(
                self._clipboard_unicode_after(copy_page)
            )
            if value:
                logger.info(
                    "[WechatDesktop][share-browser] direct read success "
                    "hwnd=%s source=clipboard chars=%s lines=%s",
                    browser_hwnd,
                    len(value),
                    value.count("\n") + 1,
                )
            return value
        except Exception as exc:
            logger.warning(
                "[WechatDesktop][share-browser] clipboard read failed "
                "hwnd=%s error=%s",
                browser_hwnd,
                exc,
            )
            return ""

    def _read_share_browser_uia_content_once(self, browser_hwnd: int) -> str:
        """Return the currently exposed UIA document without a deep tree walk."""

        try:
            import uiautomation as auto

            maximum = max(
                1,
                int(
                    self.config.get(
                        "uia_share_browser_direct_read_max_chars", 50000
                    )
                ),
            )
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(browser_hwnd)
                document = None
                for control in [root, *self._walk(root, 128)]:
                    if "document" in _text(
                        getattr(control, "ControlTypeName", "")
                    ).casefold():
                        document = control
                        break
                if document is None:
                    return ""

                # Chromium/WebView documents expose their whole accessible text
                # through TextPattern.  Reading that range is both faster and
                # more predictable than recursively expanding a dynamic page's
                # thousands of UIA descendants on every poll.
                try:
                    pattern = document.GetTextPattern()
                    if pattern is not None:
                        value = self._normalize_share_browser_content(
                            pattern.DocumentRange.GetText(maximum)
                        )
                        if value:
                            return value
                except Exception:
                    pass

                # Some WeChat builds omit TextPattern. Keep a small compatibility
                # fallback, bounded tightly so a live feed cannot stall a probe.
                names = []
                title = _text(getattr(document, "Name", ""))
                if title:
                    names.append(title)
                for control in self._walk(document, 256):
                    control_type = _text(
                        getattr(control, "ControlTypeName", "")
                    ).casefold()
                    if "text" not in control_type:
                        continue
                    name = _text(getattr(control, "Name", ""))
                    if name and (not names or names[-1] != name):
                        names.append(name)
                return self._normalize_share_browser_content("\n".join(names))
        except Exception as exc:
            logger.warning(
                "[WechatDesktop][share-browser] UIA document probe failed "
                "hwnd=%s error=%s",
                browser_hwnd,
                exc,
            )
            return ""

    def _wait_for_share_browser_content(self, browser_hwnd: int) -> str:
        """Poll until a usable UIA document remains stable for several probes."""

        timeout = max(
            0.0,
            min(
                float(self.config.get("uia_share_browser_load_timeout_seconds", 6)),
                30.0,
            ),
        )
        poll_ms = max(
            50,
            min(int(self.config.get("uia_share_browser_load_poll_ms", 250)), 2000),
        )
        minimum_wait = max(
            0.0,
            min(
                int(self.config.get("uia_share_browser_load_min_wait_ms", 800)),
                10000,
            )
            / 1000.0,
        )
        stable_required = max(
            1,
            min(
                int(self.config.get("uia_share_browser_content_stable_polls", 2)),
                10,
            ),
        )
        ready_chars = max(
            int(self.config.get("uia_share_browser_direct_read_min_chars", 20)),
            int(self.config.get("uia_share_browser_direct_read_ready_chars", 80)),
        )
        started = time.monotonic()
        deadline = started + timeout
        probes = 0
        stable_count = 0
        previous = ""
        best = ""
        logger.info(
            "[WechatDesktop][share-browser] waiting for page content "
            "hwnd=%s timeout_ms=%s poll_ms=%s stable_polls=%s",
            browser_hwnd,
            int(timeout * 1000),
            poll_ms,
            stable_required,
        )
        while True:
            probes += 1
            value = self._read_share_browser_uia_content_once(browser_hwnd)
            if len(value) > len(best):
                best = value
                logger.info(
                    "[WechatDesktop][share-browser] page content progress "
                    "hwnd=%s probe=%s chars=%s elapsed_ms=%s",
                    browser_hwnd,
                    probes,
                    len(value),
                    int((time.monotonic() - started) * 1000),
                )
            if len(value) >= ready_chars:
                stable_count = stable_count + 1 if value == previous else 1
            else:
                stable_count = 0
            elapsed = time.monotonic() - started
            if (
                stable_count >= stable_required
                and elapsed >= minimum_wait
            ):
                logger.info(
                    "[WechatDesktop][share-browser] direct read success "
                    "hwnd=%s source=uia_document chars=%s lines=%s "
                    "probes=%s elapsed_ms=%s",
                    browser_hwnd,
                    len(value),
                    value.count("\n") + 1,
                    probes,
                    int(elapsed * 1000),
                )
                return value
            previous = value
            if time.monotonic() >= deadline:
                logger.info(
                    "[WechatDesktop][share-browser] page content wait timed out "
                    "hwnd=%s probes=%s best_chars=%s ready_chars=%s elapsed_ms=%s",
                    browser_hwnd,
                    probes,
                    len(best),
                    ready_chars,
                    int((time.monotonic() - started) * 1000),
                )
                return best if len(best) >= ready_chars else ""
            if self._stop_event.wait(poll_ms / 1000.0):
                raise RuntimeError("WeChat UI Automation is stopping")

    def _normalize_share_browser_content(self, value: str) -> str:
        value = str(value or "").replace("\x00", "").replace("\r\n", "\n").strip()
        value = re.sub(r"[\t ]{3,}", "  ", value)
        value = re.sub(r"\n{4,}", "\n\n\n", value)
        minimum = max(
            1,
            int(self.config.get("uia_share_browser_direct_read_min_chars", 20)),
        )
        is_url_only = bool(
            re.fullmatch(r"https?://[^\s]+", value, re.IGNORECASE)
            and self._is_safe_public_url(value)
        )
        if len(value) < minimum or is_url_only:
            return ""
        maximum = max(
            minimum,
            int(self.config.get("uia_share_browser_direct_read_max_chars", 50000)),
        )
        return value[:maximum]

    def fetch_share_url_from_point(self, point: tuple[int, int]) -> str:
        """Open a share card at a verified message point and copy its URL."""

        _content, url = self._fetch_share_from_point(point, direct_read=False)
        return url

    def fetch_share_page_from_point(
        self, point: tuple[int, int]
    ) -> tuple[str, str]:
        """Read a share card in WeChat, copying its URL only as fallback."""

        return self._fetch_share_from_point(point, direct_read=True)

    def _fetch_share_from_point(
        self, point: tuple[int, int], *, direct_read: bool
    ) -> tuple[str, str]:
        """Open one verified share browser and return rendered text or fallback URL."""

        main_hwnd = self.get_owner_window_handle()
        visible_before = self._visible_top_level_window_handles()
        browser_hwnd = 0
        try:
            logger.info(
                "[WechatDesktop][share-browser] opening card direct_read=%s",
                direct_read,
            )
            self._left_click_point(point)
            self._paced_wait(
                "uia_share_browser_open_settle_ms_min",
                "uia_share_browser_open_settle_ms_max",
                600,
                1200,
            )
            browser_hwnd = self._find_opened_share_browser(
                main_hwnd, visible_before
            )
            if not browser_hwnd:
                logger.warning(
                    "[WechatDesktop][share-browser] opened window was not detected"
                )
                return "", ""
            logger.info(
                "[WechatDesktop][share-browser] verified hwnd=%s direct_read=%s",
                browser_hwnd,
                direct_read,
            )
            if direct_read and bool(
                self.config.get("uia_share_browser_direct_read_enabled", True)
            ):
                content = self._read_share_browser_content(browser_hwnd)
                if content:
                    return content, ""
                logger.info(
                    "[WechatDesktop][share-browser] direct read unavailable "
                    "hwnd=%s; falling back to copy link",
                    browser_hwnd,
                )
            more, _close = self._verified_share_browser_controls(
                browser_hwnd, main_hwnd
            )
            more_bounds = _bounds(more) if more is not None else None
            if not more_bounds:
                return "", ""
            more_point = (
                int((more_bounds[0] + more_bounds[2]) / 2),
                int((more_bounds[1] + more_bounds[3]) / 2),
            )

            def copy_action():
                self._right_click_point(more_point)
                self._paced_wait(
                    "uia_share_browser_menu_settle_ms_min",
                    "uia_share_browser_menu_settle_ms_max",
                    250,
                    500,
                )
                if not self._click_copy_link_menu(browser_hwnd):
                    return
                self._paced_wait(
                    "uia_share_browser_clipboard_settle_ms_min",
                    "uia_share_browser_clipboard_settle_ms_max",
                    200,
                    450,
                )

            value = self._clipboard_unicode_after(copy_action)
            if not self._is_safe_public_url(value):
                value = ""
            logger.info(
                "[WechatDesktop][share-browser] copy-link fallback %s hwnd=%s",
                "success" if value else "failed",
                browser_hwnd,
            )
            return "", value
        except Exception as exc:
            logger.warning("[WechatDesktop] failed to copy share URL: %s", exc)
            return "", ""
        finally:
            if browser_hwnd:
                if not self._close_share_browser(browser_hwnd, main_hwnd):
                    logger.warning(
                        "[WechatDesktop] verified share browser could not be closed"
                    )
            else:
                self._recover_foreground_after_dependency_failure(
                    "share browser detection failure", main_hwnd
                )

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

    def _save_control_file_as(
        self,
        control,
        content: str,
        target_root: Path,
        timeout: float,
    ) -> str:
        """Use WeChat's native Save As menu when no cached path is available."""
        filename, _expected_size = self._file_card_metadata(content)
        bounds = _bounds(control)
        if not filename or not bounds:
            return ""
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .")
        if not safe_name:
            return ""
        target_root = Path(target_root).resolve()
        target_root.mkdir(parents=True, exist_ok=True)
        identity = f"{content}\0{_runtime_id(control)}\0{time.time_ns()}"
        target = target_root / (
            f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:12]}_{safe_name}"
        )
        main_hwnd = self.get_owner_window_handle()
        left, top, right, bottom = bounds
        self._right_click_point(
            (int(left + (right - left) * 0.25), int((top + bottom) / 2))
        )
        self._paced_wait(
            "uia_file_menu_settle_ms_min",
            "uia_file_menu_settle_ms_max",
            250,
            450,
        )
        menu_item = self._find_desktop_control("另存为...", "mmui::XMenuView")
        if menu_item is None:
            self._recover_foreground_after_dependency_failure(
                "missing Save As menu"
            )
            return ""
        self._click_and_restore(menu_item)
        self._paced_wait(
            "uia_file_save_dialog_settle_ms_min",
            "uia_file_save_dialog_settle_ms_max",
            400,
            700,
        )
        dialog_hwnd = 0
        saved = False
        try:
            import uiautomation as auto
            import win32gui

            dialog_hwnd = int(win32gui.GetForegroundWindow() or 0)
            if not dialog_hwnd or win32gui.GetClassName(dialog_hwnd) != "#32770":
                return ""
            dialog = auto.ControlFromHandle(dialog_hwnd)
            filename_input = next(
                (
                    item
                    for item in self._walk(dialog, 500)
                    if _text(item.AutomationId) == "1001"
                    and "edit" in _text(item.ControlTypeName).casefold()
                ),
                None,
            )
            save_button = next(
                (
                    item
                    for item in self._walk(dialog, 500)
                    if _text(item.AutomationId) == "1"
                    and "button" in _text(item.ControlTypeName).casefold()
                ),
                None,
            )
            if filename_input is None or save_button is None:
                return ""
            filename_input.GetValuePattern().SetValue(str(target))
            self._click_and_restore(save_button)
            deadline = time.time() + max(1.0, min(float(timeout), 30.0))
            while time.time() < deadline:
                if target.is_file() and target.stat().st_size > 0:
                    logger.info(
                        "[WechatDesktop] file saved from native dialog: %s", target
                    )
                    saved = True
                    return str(target)
                time.sleep(0.25)
        finally:
            if not saved:
                # Never use WM_CLOSE or a global Escape fallback here.  If the
                # native dialog changed identity, leave it alone rather than
                # risk applying a close action to the WeChat main window.
                self._try_cancel_verified_native_dialog(dialog_hwnd, main_hwnd)
                self._recover_foreground_after_dependency_failure(
                    "Save As dialog failure",
                    main_hwnd,
                )
        return ""

    def _try_cancel_verified_native_dialog(
        self,
        dialog_hwnd: int,
        main_hwnd: int,
    ) -> bool:
        """Invoke Cancel only on an exact same-process native child dialog."""

        try:
            import uiautomation as auto
            import win32gui
            import win32process

            if (
                not dialog_hwnd
                or not main_hwnd
                or int(dialog_hwnd) == int(main_hwnd)
                or not win32gui.IsWindow(dialog_hwnd)
                or not win32gui.IsWindow(main_hwnd)
                or win32gui.GetClassName(dialog_hwnd) != "#32770"
            ):
                return False
            _, dialog_pid = win32process.GetWindowThreadProcessId(dialog_hwnd)
            _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
            if int(dialog_pid) != int(main_pid):
                return False
            with auto.UIAutomationInitializerInThread():
                dialog = auto.ControlFromHandle(dialog_hwnd)
                cancel_button = next(
                    (
                        item
                        for item in self._walk(dialog, 500)
                        if _text(getattr(item, "AutomationId", "")) == "2"
                        and "button"
                        in _text(
                            getattr(item, "ControlTypeName", "")
                        ).casefold()
                    ),
                    None,
                )
                if cancel_button is None:
                    return False
                cancel_button.GetInvokePattern().Invoke()
            return True
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] verified native dialog cancel failed: %s",
                exc,
            )
            return False

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
        if bool(self.config.get("uia_file_save_as_enabled", True)):
            try:
                self.focus_window()
                with self.operation_lock, self._uia_root() as root:
                    control = self._find_message_control(root, message)
                    if control is not None:
                        saved = self._save_control_file_as(
                            control,
                            message.content,
                            Path(target_root),
                            max(timeout, 5.0),
                        )
                        if saved:
                            return saved
            except Exception as exc:
                logger.warning(
                    "[WechatDesktop] native Save As fallback failed: %s", exc
                )
        logger.warning(
            "[WechatDesktop] file bubble was detected but no local file path was available: %s",
            message.content,
        )
        return ""

    def _capture_image_viewer_from_point(
        self,
        point: tuple[int, int],
        target: Path,
    ) -> str:
        """点击一个已验证的图片命中区域并截取新打开的微信图片查看器。"""

        import win32gui
        from PIL import ImageGrab

        main_hwnd = self.get_owner_window_handle()
        visible_before = self._visible_top_level_window_handles()
        self._left_click_point(point)
        self._paced_wait(
            "uia_image_viewer_settle_ms_min",
            "uia_image_viewer_settle_ms_max",
            500,
            900,
        )
        viewer_hwnd = self._find_opened_image_viewer(main_hwnd, visible_before)
        if not viewer_hwnd:
            logger.warning(
                "[WechatDesktop] image click did not open a strictly "
                "verified viewer"
            )
            self._recover_foreground_after_dependency_failure(
                "image viewer detection failure",
                main_hwnd,
            )
            return ""

        captured = False
        viewer_closed = False
        main_restored = False
        try:
            viewer_bounds = tuple(
                int(value) for value in win32gui.GetWindowRect(viewer_hwnd)
            )
            image = ImageGrab.grab(bbox=viewer_bounds, all_screens=True)
            if image.width >= 2 and image.height >= 2:
                image.save(target, format="PNG")
                captured = True
                logger.info(
                    "[WechatDesktop] image viewer captured to tmp: %s", target
                )
        finally:
            # The viewer is a separate WeChat top-level window.  Let its UI and
            # image surface stabilize before touching its own close control.
            # In particular, never race a just-opened referenced-image viewer
            # with a global key or a native window-close message.
            self._paced_wait(
                "uia_image_viewer_before_close_ms_min",
                "uia_image_viewer_before_close_ms_max",
                300,
                500,
            )
            viewer_closed = self._close_image_viewer(viewer_hwnd, main_hwnd)
            # Qt may keep the closed viewer HWND alive while asynchronously
            # transferring activation.  Let that transition finish before
            # bringing the main HWND forward, or Windows can immediately undo
            # our foreground request.
            self._paced_wait(
                "uia_image_viewer_close_settle_ms_min",
                "uia_image_viewer_close_settle_ms_max",
                200,
                400,
            )
            if not viewer_closed:
                logger.warning(
                    "[WechatDesktop] image viewer remained open: hwnd=%s",
                    viewer_hwnd,
                )
                self._recover_foreground_after_dependency_failure(
                    "image viewer close failure",
                    main_hwnd,
                )
            else:
                main_restored = self._restore_main_window_after_viewer(main_hwnd)
                if not main_restored:
                    logger.warning(
                        "[WechatDesktop] main window focus was not restored "
                        "after image viewer close: hwnd=%s",
                        main_hwnd,
                    )
                    self._recover_foreground_after_dependency_failure(
                        "image viewer focus restore failure",
                        main_hwnd,
                    )
        return str(target) if captured and viewer_closed and main_restored else ""

    @staticmethod
    def _reference_image_activation_point(
        bounds: tuple[int, int, int, int],
    ) -> tuple[int, int]:
        """返回扁平引用节点中“引用内容”区域的命中点。

        微信 UIA 将当前消息气泡和引用卡片折叠成单个无子节点的
        ``ChatTextItemView``。引用卡片位于节点下半部，使用左侧四分之一、
        高度约四分之三的位置可避开上方当前消息气泡。
        """

        left, top, right, bottom = (int(value) for value in bounds)
        return (
            int(left + (right - left) * 0.25),
            int(top + (bottom - top) * 0.76),
        )

    def fetch_referenced_message_image(
        self,
        message: UiaChatMessage,
        tmp_root: Optional[Path] = None,
    ) -> str:
        """直接点击当前引用节点的引用区域并截取被引用图片的大图。"""

        reference = message.reference
        if (
            reference is None
            or reference.message_type != "image"
            or not message.bounds
        ):
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
                str(reference.sender_name),
                str(reference.content),
                str(tuple(message.bounds)),
            )
        )
        target = target_root / (
            hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16] + ".png"
        )
        try:
            self.focus_window()
            with self.operation_lock, self._uia_root() as root:
                control = self._find_message_control(root, message)
                click_bounds = _bounds(control) if control is not None else message.bounds
                if not click_bounds:
                    return ""
                point = self._reference_image_activation_point(click_bounds)
            # Leave the main-window UIA initializer before clicking.  Viewer
            # discovery creates its own UIA context; nesting the two contexts
            # while WeChat is creating a top-level viewer can deadlock COM.
            logger.info(
                "[WechatDesktop] referenced image activation prepared: "
                "point=%s bounds=%s",
                point,
                click_bounds,
            )
            return self._capture_image_viewer_from_point(point, target)
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] referenced image viewer capture failed: %s", exc
            )
            return ""

    def fetch_message_image(
        self,
        message: UiaChatMessage,
        tmp_root: Optional[Path] = None,
        prefer_viewer: bool = True,
    ) -> str:
        """Open WeChat's image viewer and capture it, falling back to the bubble."""
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
        if prefer_viewer and bool(self.config.get("uia_image_viewer_enabled", True)):
            try:
                self.focus_window()
                with self.operation_lock, self._uia_root() as root:
                    control = self._find_message_control(root, message)
                    click_bounds = self._image_activation_bounds(
                        control, message.bounds
                    )
                    if not click_bounds:
                        raise RuntimeError("image activation bounds are unavailable")
                    click_left, click_top, click_right, click_bottom = click_bounds
                    point = (
                        int((click_left + click_right) / 2),
                        int((click_top + click_bottom) / 2),
                    )
                captured = self._capture_image_viewer_from_point(point, target)
                if captured:
                    return captured
            except Exception as exc:
                logger.warning(
                    "[WechatDesktop] image viewer capture failed; using bubble: %s",
                    exc,
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
                win32clipboard.SetClipboardData(
                    win32con.CF_HDROP,
                    _encode_cf_hdrop(files),
                )
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
                        elif fmt == win32con.CF_HDROP:
                            win32clipboard.SetClipboardData(
                                fmt,
                                value
                                if isinstance(value, (bytes, bytearray, memoryview))
                                else _encode_cf_hdrop(value),
                            )
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
        expedited: bool = False,
    ) -> dict:
        text = str(message or "")
        if not text:
            raise ValueError("text is empty")
        chunk_limit = max(
            100,
            min(int(self.config.get("uia_text_chunk_chars", 500)), 2000),
        )
        chunks = self._split_message_text(text, chunk_limit)
        # ``_send_lock`` only serializes competing senders. Humanized pacing
        # (send interval + per-conversation cooldown) waits under it but
        # OUTSIDE ``uia_section``/``operation_lock``, so conversation scanning
        # and attachment reads keep running while this sender sleeps. Each
        # chunk then performs its bounded UI work (focus, locate, paste,
        # verify) inside an exclusive UIA section; the per-chunk
        # focus/locate/title checks make interleaved scans safe.
        with self._send_lock:
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
                    if not expedited:
                        self._wait_for_send_slot(who)
                    with self.uia_section(), self.operation_lock:
                        self.focus_window()
                        if not self.locate_conversation(
                            who, runtime_id, row_index
                        ):
                            raise RuntimeError(
                                f"Conversation is not visible: {who}"
                            )
                        # Defense in depth: never paste into a detail pane that
                        # still shows a different chat title (failed session
                        # switch).
                        active = self.get_title()
                        if not conversation_titles_match(active.title, who):
                            raise RuntimeError(
                                f"Active chat is {active.title!r}, expected {who!r}"
                            )
                        before = self.get_chat_history(limit=5)
                        with self._clipboard(unicode_text=chunk):
                            try:
                                self._paste_and_send(expected_text=chunk)
                            except RuntimeError as exc:
                                if "did not clear the reply input" not in str(exc):
                                    raise
                                # The UIA ValuePattern can lag behind a
                                # successful click. Verify first so the
                                # fallback cannot send a duplicate, then
                                # reacquire the input before Enter.
                                result = self._verify_send(
                                    who, before, text=chunk
                                )
                                if not result.get("verified"):
                                    if not self._send_existing_input_with_enter(
                                        chunk
                                    ):
                                        raise
                                    result = self._verify_send(
                                        who, before, text=chunk
                                    )
                                    if not result.get("verified"):
                                        raise exc
                            else:
                                result = self._verify_send(
                                    who, before, text=chunk
                                )
                    results.append(result)
                    if not expedited:
                        now = time.monotonic()
                        self._last_send_at = now
                        self._last_conversation_send[str(who)] = now
                    if result.get("verified"):
                        self.remember_outgoing_message(
                            who,
                            chunk,
                            str(result.get("runtime_id") or ""),
                        )
                    else:
                        # Sent-but-unverified: the UI action completed but the
                        # outgoing bubble was not confirmed in time. We do NOT
                        # retry (to avoid duplicates), so treat the message as
                        # "probably sent" and suppress the echo.  Register the
                        # text without a runtime_id so is_known_outgoing_message
                        # can still match on content and prevent a self-reply
                        # loop if the bubble appears on the next scan.
                        self.remember_outgoing_message(who, chunk)
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
        with self._send_lock:
            # Pacing waits stay outside the UIA section (see send_message).
            self._wait_for_send_slot(who)
            with self.uia_section(), self.operation_lock:
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
            if result.get("verified"):
                self.remember_outgoing_message(
                    who, runtime_id=str(result.get("runtime_id") or "")
                )
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
                if is_new and matches:
                    return {
                        "success": True,
                        "verified": True,
                        "runtime_id": item.runtime_id,
                    }
        return {
            "success": True,
            "verified": False,
            "message": "UI action completed but the outgoing bubble was not verified",
        }
