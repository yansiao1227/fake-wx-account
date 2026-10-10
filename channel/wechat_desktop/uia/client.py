"""Synchronous WeChat 4.1.9.30 client built on Windows UI Automation."""

from __future__ import annotations
from channel.wechat_desktop.config import DEFAULT_CONFIG
from channel.wechat_desktop.uia.reference_resolver import WechatReferenceResolver
from channel.wechat_desktop.uia.image_viewer import WechatImageViewer
from channel.wechat_desktop.uia.attachments import WechatAttachmentReader
from channel.wechat_desktop.text import split_message_text
from channel.wechat_desktop.send_control import (
    wait_send_delay,
)
import hashlib
import ctypes
import os
import random
import re
import threading
import time
import unicodedata
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional
from urllib.parse import urlparse
from common.log import file_logger, logger
from channel.wechat_desktop.uia.group_sender_ocr import RapidOcrGroupSenderResolver
from channel.wechat_desktop.models import (
    ConversationInfo,
    DEFAULT_SELF_SENDER_NAME,
    HeaderInfo,
    OwnerInfo,
    UNKNOWN_SENDER_NAME,
    UiaChatMessage,
    UiaReferencedMessage,
    WechatHistoryMessage,
    WechatHistoryReadResult,
)
from channel.wechat_desktop.conversation import (
    conversation_titles_match,
    strip_member_count_suffix,
)
from channel.wechat_desktop.uia.controls import (
    SESSION_PREFIX,
    MESSAGE_LIST_ID,
    HISTORY_LIST_ID,
    HISTORY_WINDOW_CLASS,
    MENTION_MARKERS,
    HISTORY_TIME_RE,
    FILE_SIZE_RE,
    FILE_SIZE_UNITS,
    FILE_DUPLICATE_SUFFIX_RE,
    _text,
    _bounds,
    _runtime_id,
)
from channel.wechat_desktop.uia.history_reader import WechatHistoryReader
from channel.wechat_desktop.uia.share_browser import WechatShareBrowser
from channel.wechat_desktop.uia.message_sender import WechatMessageSender


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
        self._history_reader = WechatHistoryReader(self)
        self._share_browser = WechatShareBrowser(self)
        self._send_controller = WechatMessageSender(self)
        self._reference_resolver = WechatReferenceResolver(self)
        self._image_viewer = WechatImageViewer(self)
        self._attachment_reader = WechatAttachmentReader(self)
        self.operation_lock = threading.RLock()
        # Serializes concurrent senders (reply FIFO vs. agent-initiated sends)
        # so humanized pacing waits happen outside any UIA lock: a waiting
        # sender must never block independent attachment reads.
        self._send_lock = threading.RLock()
        # Optional per-section UIA priority context installed by the gateway.
        # Each bounded send section (one chunk's focus/locate/paste/verify)
        # runs inside ``uia_section()``; attachment operations may acquire the
        # UI lease while a sender sleeps between chunks.
        self.uia_section = nullcontext
        self._owner_cache: Optional[OwnerInfo] = None
        self._owner_cache_window: Optional[tuple[int, int]] = None
        self._owner_lookup_retry_after = 0.0
        self._stop_event = threading.Event()
        self._last_send_at = 0.0
        self._last_conversation_send: dict[str, float] = {}
        # OCR 仅供显式完整 UI 快照的后备识别，普通发送快照及数据库接收均不加载模型。
        self._group_sender_ocr = RapidOcrGroupSenderResolver(self.config)

    def cancel_waits(self):
        self._stop_event.set()

    def resume_waits(self):
        self._stop_event.clear()

    def _paced_wait(
        self,
        minimum_key: str,
        maximum_key: str,
    ) -> None:
        minimum = max(0, min(int(self.config.get(minimum_key, DEFAULT_CONFIG[minimum_key])), 10000))
        maximum = max(
            minimum,
            min(int(self.config.get(maximum_key, DEFAULT_CONFIG[maximum_key])), 10000),
        )
        delay_ms = random.randint(minimum, maximum) if maximum > minimum else minimum
        wait_send_delay(delay_ms / 1000.0, self._stop_event)

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
        wait_send_delay(remaining, self._stop_event)

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
                try:
                    executable = win32process.GetModuleFileNameEx(process, 0)
                finally:
                    try:
                        process.Close()
                    except Exception:
                        pass
            except Exception:
                executable = ""
            # Qt 主窗口类被许多程序共享。无法读取进程路径时只能信任微信独有类，
            # 否则受保护/提权的非微信 Qt 窗口会造成多个微信主窗口的误判。
            if (executable and Path(executable).name.casefold() == "weixin.exe") or (
                not executable and native_class == "mmui::MainWindow"
            ):
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

    def _read_visible_owner_sidebar(self, root) -> str:
        """只解析主窗口已经暴露的账号控件，不打开个人资料。"""
        root_bounds = _bounds(root)
        left_limit = (
            root_bounds[0] + max(100, (root_bounds[2] - root_bounds[0]) // 5)
            if root_bounds else 0
        )
        for control in self._walk(root):
            aid = _text(control.AutomationId).casefold()
            cls = _text(control.ClassName).casefold()
            name = _text(control.Name)
            bounds = _bounds(control)
            profile_hint = any(
                key in aid
                for key in ("self_avatar", "owner_avatar", "profile", "account", "userinfo")
            ) or ("avatar" in cls and any(key in aid for key in ("self", "owner")))
            if (
                profile_hint and bounds and left_limit and bounds[0] <= left_limit
                and name and name not in {"头像", "微信"}
            ):
                return name
        return ""

    def get_owner_info_passive(self) -> OwnerInfo:
        """只读窗口内账号信息；空树或缺少身份时绝不点击、恢复或聚焦。"""
        return self._get_owner_info(allow_profile_popup=False)

    def get_owner_info(self) -> OwnerInfo:
        """显式账号诊断入口，可交互打开个人资料；发送绑定使用被动接口。"""
        return self._get_owner_info(allow_profile_popup=True)

    def _get_owner_info(self, *, allow_profile_popup: bool) -> OwnerInfo:
        with self.operation_lock:
            return self._get_owner_info_locked(allow_profile_popup=allow_profile_popup)

    def _get_owner_info_locked(self, *, allow_profile_popup: bool) -> OwnerInfo:
        # HWND/PID 变化时不能继承其他窗口的账号或失败重试缓存。
        try:
            window = self._window()
        except Exception:
            self._owner_cache = None
            self._owner_cache_window = None
            self._owner_lookup_retry_after = 0.0
            return OwnerInfo("", source="unknown")
        if window != self._owner_cache_window:
            self._owner_cache = None
            self._owner_lookup_retry_after = 0.0
            self._owner_cache_window = window
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
                discovered = self._read_visible_owner_sidebar(root)
                if discovered:
                    discovery_method = "sidebar"
                if not discovered and allow_profile_popup:
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
        # 读取过程中窗口被替换时，结果不能归属于旧账号。
        try:
            if self._window() != window:
                self._owner_cache = None
                self._owner_cache_window = None
                self._owner_lookup_retry_after = 0.0
                return OwnerInfo("", source="unknown")
        except Exception:
            self._owner_cache = None
            self._owner_cache_window = None
            self._owner_lookup_retry_after = 0.0
            return OwnerInfo("", source="unknown")
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
        WeChat UI operation for close to a minute.  Native window enumeration is
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

    @staticmethod
    def _is_selected_session(control) -> bool:
        """同名会话必须同时通过行选中状态和聊天标题校验；读不到时不猜测。"""
        try:
            return bool(control.GetSelectionItemPattern().IsSelected)
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
            # 行号只是可变化的位置，不能消除同名会话的歧义。
            if not runtime_id and len(candidates) != 1:
                return False
            if control is None and 0 <= int(row_index) < len(session_rows):
                indexed = session_rows[int(row_index)]
                if _text(indexed.AutomationId) == target_id:
                    control = indexed
            if control is None and not runtime_id and len(candidates) == 1:
                control = candidates[0]
            if control is not None:
                def identity_matches():
                    if len(candidates) == 1:
                        return True
                    selected = [item for item in candidates if self._is_selected_session(item)]
                    return len(selected) == 1 and selected[0] is control

                # 已打开会话的快路径也必须先验证精确身份。
                if identity_matches() and self._active_conversation_has_content(root, target):
                    return True
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
                # (that would paste replies into the wrong chat).
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
                    )
                    if identity_matches() and self._active_conversation_matches(root, target):
                        return True
        return False

    def get_title(self) -> HeaderInfo:
        title = ""
        count = 1
        count_label_seen = False
        count_from_label = False
        with self.operation_lock, self._uia_root() as root:
            for control in self._walk(root):
                automation_id = _text(control.AutomationId)
                if automation_id.endswith("current_chat_name_label"):
                    title = _text(control.Name)
                elif automation_id.endswith("current_chat_count_label"):
                    count_label_seen = True
                    match = re.search(r"(\d+)", _text(control.Name))
                    if match:
                        count = max(1, int(match.group(1)))
                        count_from_label = True
        # 昵称也可以含有数字括号后缀，后缀不能作为群类型的证据。
        # 只有独立人数控件已经确认群聊时，才去掉标题展示人数。
        base = strip_member_count_suffix(title)
        title_has_count_suffix = bool(base and base != title)
        if count_from_label and title_has_count_suffix:
            title = base
        # 独立人数控件确认的一人群仍执行群策略；仅有后缀或人数控件
        # 不可解析时无法确认类型，禁止主动发送时降级为私聊或群聊。
        kind = "unknown"
        if title:
            kind = ("group" if count_from_label else "unknown"
                    if count_label_seen or title_has_count_suffix else "private")
        return HeaderInfo(
            title,
            kind,
            count,
        )

    @classmethod
    def _find_chat_history_button(cls, root):
        candidates = []
        for control in cls._walk(root):
            name = _text(getattr(control, "Name", ""))
            control_type = _text(getattr(control, "ControlTypeName", ""))
            class_name = _text(getattr(control, "ClassName", ""))
            if name != "聊天记录" or control_type != "ButtonControl":
                continue
            candidates.append((class_name == "mmui::XOutlineButton", control))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1] if candidates else None

    @staticmethod
    def _history_window_is_open(history_hwnd: int) -> bool:
        try:
            import win32gui

            return bool(
                history_hwnd
                and win32gui.IsWindow(history_hwnd)
                and win32gui.IsWindowVisible(history_hwnd)
                and not win32gui.IsIconic(history_hwnd)
            )
        except Exception:
            return False

    @staticmethod
    def _history_window_native_candidates(main_hwnd: int, process_id: int) -> list[int]:
        import win32gui
        import win32process

        candidates: list[int] = []

        def callback(hwnd, _):
            native_hwnd = int(hwnd or 0)
            if not native_hwnd or native_hwnd == int(main_hwnd):
                return True
            if not win32gui.IsWindowVisible(native_hwnd):
                return True
            _, candidate_pid = win32process.GetWindowThreadProcessId(native_hwnd)
            if int(candidate_pid) != int(process_id):
                return True
            native_class = _text(win32gui.GetClassName(native_hwnd))
            title = _text(win32gui.GetWindowText(native_hwnd))
            if native_class.startswith("Qt") and "聊天记录" in title:
                candidates.append(native_hwnd)
            return True

        win32gui.EnumWindows(callback, None)
        return candidates

    @classmethod
    def _verified_history_root(cls, history_hwnd: int):
        import uiautomation as auto

        root = auto.ControlFromHandle(history_hwnd)
        if _text(getattr(root, "ClassName", "")) != HISTORY_WINDOW_CLASS:
            return None
        history_list = next(
            (
                control
                for control in cls._walk(root)
                if _text(getattr(control, "AutomationId", "")) == HISTORY_LIST_ID
                and _text(getattr(control, "ControlTypeName", "")) == "ListControl"
            ),
            None,
        )
        return (root, history_list) if history_list is not None else None

    def _find_history_window(self, main_hwnd: int, process_id: int) -> tuple[int, object, object] | None:
        return self._history_reader._find_history_window(main_hwnd, process_id)

    @staticmethod
    def _parse_history_row_text(value: str) -> tuple[str, str, Optional[str], bool]:
        raw = _text(value)
        match = HISTORY_TIME_RE.search(raw)
        if not match:
            return raw, "", None, True
        content = raw[: match.start()].rstrip()
        time_text = match.group("time")
        try:
            parsed = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                int(match.group("hour")),
                int(match.group("minute")),
            ).astimezone()
            timestamp = parsed.isoformat()
            return content, time_text, timestamp, False
        except ValueError:
            return content, time_text, None, True

    @classmethod
    def _parse_history_row(cls, row) -> Optional[WechatHistoryMessage]:
        class_name = _text(getattr(row, "ClassName", ""))
        control_type = _text(getattr(row, "ControlTypeName", ""))
        raw = _text(getattr(row, "Name", ""))
        if (
            control_type != "ListItemControl"
            or "Chat" not in class_name
            or "ItemView" not in class_name
            or not raw
        ):
            return None
        content, time_text, timestamp, degraded = cls._parse_history_row_text(raw)
        if not content:
            return None
        message_type = cls._message_type(class_name, content)
        digest = hashlib.sha256(
            "\0".join(
                (class_name, message_type, content, time_text)
            ).encode("utf-8")
        ).hexdigest()
        return WechatHistoryMessage(
            sender_name=UNKNOWN_SENDER_NAME,
            direction="unknown",
            content_type=message_type,
            content=content,
            time_text=time_text,
            timestamp=timestamp,
            stable_id=f"sha256:{digest}",
            degraded=degraded,
        )

    @classmethod
    def _read_history_rows(
        cls, history_list
    ) -> list[tuple[str, WechatHistoryMessage]]:
        messages = []
        try:
            rows = history_list.GetChildren()
        except Exception:
            rows = []
        for row in rows:
            message = cls._parse_history_row(row)
            if message is not None:
                # RecyclerListView reuses the same UIA RuntimeId slots after
                # scrolling. Message content/type/time is the stable identity
                # across viewports; using RuntimeId here would make newly
                # loaded older rows look like the original screen.
                messages.append((message.stable_id, message))
        return messages

    @classmethod
    def _scroll_history_table_older(cls, history_list) -> bool:
        try:
            pattern = history_list.GetScrollPattern()
            if pattern is not None and getattr(
                pattern, "VerticallyScrollable", False
            ):
                import uiautomation as auto

                pattern.Scroll(
                    auto.ScrollAmount.NoAmount,
                    auto.ScrollAmount.LargeIncrement,
                    waitTime=0.1,
                )
                return True
        except Exception:
            pass

        try:
            # WeChat 4.1.9.30 does not expose ScrollPattern on
            # chat_log_message_list, but the control's WheelDown helper sends
            # real wheel input to the verified history list bounds. In this
            # window the newest messages are at the top, so WheelDown loads
            # older rows.
            history_list.WheelDown(
                wheelTimes=8,
                interval=0.05,
                waitTime=0.1,
            )
            return True
        except Exception:
            return False

    def _close_history_window(self, history_hwnd: int, main_hwnd: int) -> bool:
        return self._history_reader._close_history_window(history_hwnd, main_hwnd)

    def read_current_chat_history(self, limit: int=20) -> WechatHistoryReadResult:
        return self._history_reader.read_current_chat_history(limit)

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
    def _file_card_fields(content: str) -> tuple[str, Optional[int], str]:
        """Extract filename, parsed size, and the raw size token from a file card."""
        lines = [line.strip() for line in _text(content).splitlines() if line.strip()]
        if len(lines) < 2 or lines[0].casefold() not in {"文件", "file"}:
            return "", None, ""
        filename = Path(lines[1]).name.strip()
        if not filename or not Path(filename).suffix:
            return "", None, ""
        size_token = lines[2] if len(lines) >= 3 else ""
        size = WechatUiaClient._parse_file_card_size(size_token)
        return filename, size, size_token

    @staticmethod
    def _file_card_metadata(content: str) -> tuple[str, Optional[int]]:
        """Extract the filename and displayed size from a WeChat file card."""
        filename, size, _size_token = WechatUiaClient._file_card_fields(content)
        return filename, size

    @staticmethod
    def _parse_file_card_size(token: str) -> Optional[int]:
        match = FILE_SIZE_RE.fullmatch(str(token or "").strip())
        if not match:
            return None
        return int(float(match.group(1)) * FILE_SIZE_UNITS[match.group(2).upper()])

    @staticmethod
    def _cached_file_size_limit(size_token: str, expected_size: int) -> int:
        """Allow both a 2% band and half of the file-card display quantum.

        WeChat rounds the bubble size to the printed precision. A card that
        says ``1.2M`` can therefore be ~0.05MiB away from 1.2 * 1024^2, which
        already exceeds a plain 2% window.
        """
        percent_limit = max(2048, int(expected_size * 0.02))
        match = FILE_SIZE_RE.fullmatch(str(size_token or "").strip())
        if not match:
            return percent_limit
        number = match.group(1)
        unit = match.group(2).upper()
        decimals = len(number.split(".", 1)[1]) if "." in number else 0
        quantum = FILE_SIZE_UNITS[unit] * (10 ** (-decimals))
        rounding_limit = max(2048, int(quantum / 2))
        return max(percent_limit, rounding_limit)

    @staticmethod
    def _cached_file_name_key(name: str) -> tuple[str, str]:
        """Compare file-card names after NFKC and stripping ``(1)`` copies."""
        normalized = unicodedata.normalize("NFKC", str(name or "")).casefold()
        path = Path(normalized)
        stem = FILE_DUPLICATE_SUFFIX_RE.sub("", path.stem).strip()
        return stem, path.suffix

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
        messages = self._read_visible_chat_messages(header, use_ocr=True)
        return messages if bounded_limit == 0 else messages[-bounded_limit:]

    def get_send_bubble_snapshot(
        self, conversation: Optional[str] = None, limit: int = 5,
    ) -> list[UiaChatMessage]:
        """发送回声快照，只读 UIA 树；不定位会话、不截图、不调用 OCR。"""
        header = self.get_title()
        if conversation and not conversation_titles_match(header.title, conversation):
            return []
        messages = self._read_visible_chat_messages(header, use_ocr=False)
        bounded_limit = max(1, min(int(limit), 20))
        return messages[-bounded_limit:]

    def get_chat_scroll_position_passive(self) -> Optional[bool]:
        """仅读取 ScrollPattern；无法证明在底部时返回 None，不滚动或激活窗口。"""
        with self.operation_lock, self._uia_root() as root:
            for control in self._walk(root):
                if _text(control.AutomationId) != MESSAGE_LIST_ID:
                    continue
                try:
                    pattern = control.GetScrollPattern()
                    if pattern is None:
                        return None
                    if not bool(pattern.VerticallyScrollable):
                        return True
                    percent = float(pattern.VerticalScrollPercent)
                    return percent >= 99.99 if 0.0 <= percent <= 100.0 else None
                except Exception:
                    return None
        return None

    @classmethod
    def _send_direction_from_children(cls, item, pane_bounds) -> str:
        """只信任气泡本体/头像子控件；消息行的矩形可能覆盖整行。"""
        if not pane_bounds:
            return "unknown"
        pane_left, pane_top, pane_right, pane_bottom = pane_bounds
        pane_width = pane_right - pane_left
        if pane_width <= 0 or pane_bottom <= pane_top:
            return "unknown"
        center = (pane_left + pane_right) / 2
        margin = max(5, pane_width * 0.04)
        evidence = set()
        try:
            pending = list(item.GetChildren())
        except Exception:
            return "unknown"
        visited = 0
        while pending and visited < 128:
            child = pending.pop(0)
            visited += 1
            class_name = _text(getattr(child, "ClassName", "")).casefold()
            aid = _text(getattr(child, "AutomationId", "")).casefold()
            # 引用内容的头像/气泡属于原消息，整条子树都不能证明当前方向。
            if ("itemview" in class_name or "refer" in class_name
                    or "refer" in aid or "quote" in aid):
                continue
            try:
                pending.extend(child.GetChildren())
            except Exception:
                pass
            avatar = any(key in class_name for key in (
                "chatavatar", "messageavatar", "senderavatar",
            )) or any(
                key in aid for key in ("chat_avatar", "message_avatar", "sender_avatar")
            )
            body = any(key in class_name for key in (
                "chattextbubble", "chatbubbleview", "chatmessagebubble", "chattextbody",
            )) or any(key in aid for key in (
                "message_bubble", "chat_bubble", "bubble_body", "message_body",
            ))
            if not (avatar or body):
                continue
            bounds = _bounds(child)
            if not bounds:
                continue
            left, top, right, bottom = bounds
            if (
                right <= left or bottom <= top
                or left < pane_left or right > pane_right
                or top < pane_top or bottom > pane_bottom
                or right - left >= pane_width * (0.25 if avatar else 0.9)
            ):
                continue
            if right < center - margin:
                evidence.add("incoming")
            elif left > center + margin:
                evidence.add("outgoing")
        # 遍历上限截断时，未读取子树可能包含相反证据。
        return evidence.pop() if not pending and len(evidence) == 1 else "unknown"

    def _read_visible_chat_messages(
        self, header: HeaderInfo, *, use_ocr: bool,
    ) -> list[UiaChatMessage]:
        """共享字段解析；显式完整 UI 快照才补充截图和 OCR 方向/群发送者。"""
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
                direction = (
                    "unknown" if use_ocr
                    else self._send_direction_from_children(item, list_bounds)
                )
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
            if use_ocr and header.header_type in {"group", "private"}:
                messages = self._group_sender_ocr.enrich(
                    messages,
                    list_bounds,
                    resolve_sender_names=header.header_type == "group",
                )
            if header.header_type == "private":
                messages = self._normalize_private_senders(header, messages)
        return messages

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

    @classmethod
    def _find_image_viewer_close_control(cls, root):
        return WechatImageViewer(cls)._find_image_viewer_close_control(root)

    def _is_image_viewer_window(self, viewer_hwnd: int, main_hwnd: int) -> bool:
        return self._image_viewer._is_image_viewer_window(viewer_hwnd, main_hwnd)

    def _has_image_viewer_close_control(self, viewer_hwnd: int) -> bool:
        return self._image_viewer._has_image_viewer_close_control(viewer_hwnd)

    _image_viewer_is_open = staticmethod(WechatImageViewer._image_viewer_is_open)

    _visible_top_level_window_handles = staticmethod(WechatImageViewer._visible_top_level_window_handles)

    def _find_opened_image_viewer(
        self,
        main_hwnd: int,
        visible_before: set[int],
    ) -> int:
        return self._image_viewer._find_opened_image_viewer(main_hwnd, visible_before)

    def _try_close_image_viewer_with_uia(self, viewer_hwnd: int) -> bool:
        return self._image_viewer._try_close_image_viewer_with_uia(viewer_hwnd)

    def _close_image_viewer(self, viewer_hwnd: int, main_hwnd: int) -> bool:
        return self._image_viewer._close_image_viewer(viewer_hwnd, main_hwnd)

    _restore_main_window_after_viewer = staticmethod(WechatImageViewer._restore_main_window_after_viewer)

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
    def _pick_located_original_control(cls, message_list, message_type: str, preview: str):
        return WechatReferenceResolver(cls)._pick_located_original_control(message_list, message_type, preview)

    def _return_to_reference(self) -> bool:
        return self._reference_resolver._return_to_reference()

    def resolve_message_reference(
        self, message: UiaChatMessage, *, allow_filename_cache: bool = True, validate_target=None,
    ) -> UiaChatMessage:
        return self._reference_resolver.resolve_message_reference(
            message, allow_filename_cache=allow_filename_cache, validate_target=validate_target)

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
            # 新版微信将分享页显示为主窗口右侧的 WebView 面板。该面板和
            # 主窗口同进程，但不能因此把 ExtensionIndicator 等任意浮层误认
            # 成浏览器；嵌入式页面固定使用 Chromium 的宿主窗口类。
            if int(browser_pid) == int(main_pid):
                return win32gui.GetClassName(browser_hwnd) == "Chrome_WidgetWin_0"
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
        return self._share_browser._verified_share_browser_controls(browser_hwnd, main_hwnd)

    def _find_opened_share_browser(self, main_hwnd: int, visible_before: set[int]) -> int:
        return self._share_browser._find_opened_share_browser(main_hwnd, visible_before)

    def _find_embedded_share_browser(self, main_hwnd: int) -> int:
        return self._share_browser._find_embedded_share_browser(main_hwnd)

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

    def _click_copy_link_menu(self, browser_hwnd: int, *, validate_context=None) -> bool:
        return self._share_browser._click_copy_link_menu(browser_hwnd, validate_context=validate_context)

    def _close_share_browser(self, browser_hwnd: int, main_hwnd: int, *, restore_main=True, expected_identity=None) -> bool:
        return self._share_browser._close_share_browser(
            browser_hwnd, main_hwnd, restore_main=restore_main, expected_identity=expected_identity)

    def fetch_referenced_share_url(self, message: UiaChatMessage) -> str:
        return self._share_browser.fetch_referenced_share_url(message)

    def fetch_referenced_share_page(self, message: UiaChatMessage, *, strict=False, validate_target=None) -> tuple[str, str]:
        return self._share_browser.fetch_referenced_share_page(message, strict=strict, validate_target=validate_target)

    def _fetch_referenced_share(self, message: UiaChatMessage, *, direct_read: bool, strict=False, validate_target=None) -> tuple[str, str]:
        return self._share_browser._fetch_referenced_share(
            message, direct_read=direct_read, strict=strict, validate_target=validate_target)

    def _read_share_browser_content(self, browser_hwnd: int, *, validate_context=None) -> str:
        return self._share_browser._read_share_browser_content(browser_hwnd, validate_context=validate_context)

    def _read_share_browser_uia_content_once(self, browser_hwnd: int) -> str:
        return self._share_browser._read_share_browser_uia_content_once(browser_hwnd)

    def _wait_for_share_browser_content(self, browser_hwnd: int, *, validate_context=None) -> str:
        return self._share_browser._wait_for_share_browser_content(browser_hwnd, validate_context=validate_context)

    def _normalize_share_browser_content(self, value: str) -> str:
        return self._share_browser._normalize_share_browser_content(value)

    def fetch_share_url_from_point(self, point: tuple[int, int]) -> str:
        return self._share_browser.fetch_share_url_from_point(point)

    def fetch_share_page_from_point(self, point: tuple[int, int]) -> tuple[str, str]:
        return self._share_browser.fetch_share_page_from_point(point)

    def fetch_share_message_page(self, message: UiaChatMessage, *, strict=False, validate_target=None) -> tuple[str, str]:
        return self._share_browser.fetch_share_message_page(message, strict=strict, validate_target=validate_target)

    def _fetch_share_from_point(self, point: tuple[int, int], *, direct_read: bool,
                               validate_before_click=None, validate_context=None) -> tuple[str, str]:
        return self._share_browser._fetch_share_from_point(
            point, direct_read=direct_read, validate_before_click=validate_before_click, validate_context=validate_context)

    def _copy_control_file_paths(self, control) -> list[str]:
        return self._attachment_reader._copy_control_file_paths(control)

    @classmethod
    def _download_button(cls, file_control):
        return WechatAttachmentReader(cls)._download_button(file_control)

    _store_file_in_tmp = staticmethod(WechatAttachmentReader._store_file_in_tmp)

    def _wechat_data_roots(self) -> list[Path]:
        return self._attachment_reader._wechat_data_roots()

    _xwechat_configured_file_roots = staticmethod(WechatAttachmentReader._xwechat_configured_file_roots)

    def _find_cached_message_file(self, content: str) -> str:
        return self._attachment_reader._find_cached_message_file(content)

    def _save_control_file_as(
        self,
        control,
        content: str,
        target_root: Path,
        timeout: float,
    ) -> str:
        return self._attachment_reader._save_control_file_as(control, content, target_root, timeout)

    def _try_cancel_verified_native_dialog(
        self,
        dialog_hwnd: int,
        main_hwnd: int,
    ) -> bool:
        return self._attachment_reader._try_cancel_verified_native_dialog(dialog_hwnd, main_hwnd)

    def fetch_message_file(
        self, message: UiaChatMessage, tmp_root: Optional[Path] = None,
        *, allow_filename_cache: bool = True, validate_target=None,
    ) -> str:
        return self._attachment_reader.fetch_message_file(
            message, tmp_root, allow_filename_cache=allow_filename_cache, validate_target=validate_target)

    def _capture_image_viewer_from_point(
        self,
        point: tuple[int, int],
        target: Path,
        *, validate_before_click=None, validate_context=None,
    ) -> str:
        return self._attachment_reader._capture_image_viewer_from_point(
            point, target, validate_before_click=validate_before_click, validate_context=validate_context)

    _reference_image_activation_point = staticmethod(WechatAttachmentReader._reference_image_activation_point)

    def fetch_referenced_message_image(
        self,
        message: UiaChatMessage,
        tmp_root: Optional[Path] = None,
        *, strict: bool = False, validate_target=None,
    ) -> str:
        return self._attachment_reader.fetch_referenced_message_image(
            message, tmp_root, strict=strict, validate_target=validate_target)

    def fetch_message_image(
        self,
        message: UiaChatMessage,
        tmp_root: Optional[Path] = None,
        prefer_viewer: bool = True,
        *, strict: bool = False, validate_target=None,
    ) -> str:
        return self._attachment_reader.fetch_message_image(
            message, tmp_root, prefer_viewer, strict=strict, validate_target=validate_target)

    def _clipboard(self, unicode_text: Optional[str]=None, files: Optional[Iterable[str]]=None):
        return self._send_controller._clipboard(unicode_text, files)

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
        return self._send_controller._wait_for_input_value(control, predicate, timeout)

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

    _split_message_text = staticmethod(split_message_text)

    @staticmethod
    def _keyboard_focus_state(control) -> Optional[bool]:
        try:
            return bool(control.HasKeyboardFocus)
        except Exception:
            return None

    def _wait_for_keyboard_focus(self, control, timeout: float=0.25) -> Optional[bool]:
        return self._send_controller._wait_for_keyboard_focus(control, timeout)

    def _press_shortcut(self, win32api, win32con, key: int) -> None:
        return self._send_controller._press_shortcut(win32api, win32con, key)

    def _click_send_button(self, send_button):
        return self._send_controller._click_send_button(send_button)

    def _paste_and_send(self, expected_text: str=''):
        return self._send_controller._paste_and_send(expected_text)

    def send_message(self, who: str, message: str, runtime_id: str='', row_index: int=-1, expedited: bool=False) -> dict:
        return self._send_controller.send_message(who, message, runtime_id, row_index, expedited)

    def send_file(self, who: str, files: Iterable[str], runtime_id: str='', row_index: int=-1) -> dict:
        return self._send_controller.send_file(who, files, runtime_id, row_index)

    def _verify_send(self, who: str, before: list[UiaChatMessage], text: str='', expected_type: str='') -> dict:
        return self._send_controller._verify_send(who, before, text, expected_type)
