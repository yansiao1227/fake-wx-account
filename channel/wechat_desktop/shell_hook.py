"""Windows Shell Hook used to wake the WeChat UIA observer."""

from __future__ import annotations

import ctypes
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable, Optional


HSHELL_FLASH = 0x8006


@dataclass(frozen=True)
class ShellFlashSignal:
    signaled: bool
    created_at: float = 0.0
    hwnd: int = 0
    process_id: int = 0
    reason: str = ""


class WindowsShellHook:
    """Receive taskbar flash notifications without doing work in WndProc."""

    def __init__(
        self,
        allowed_process_ids: Callable[[], Iterable[int]],
        debounce_ms: int = 250,
    ):
        self._allowed_process_ids = allowed_process_ids
        self._debounce_seconds = max(0, int(debounce_ms)) / 1000.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._latest = ShellFlashSignal(False)
        self._last_accepted_at = 0.0
        self._hwnd = 0
        self._thread: Optional[threading.Thread] = None
        self._error = ""

    @property
    def available(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._error)

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> bool:
        if os.name != "nt":
            self._error = "Windows Shell Hook is only available on Windows"
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._error = ""
        self._thread = threading.Thread(
            target=self._message_loop,
            name="cow-wechat-shell-flash",
            daemon=True,
        )
        self._thread.start()
        deadline = time.time() + 2.0
        while time.time() < deadline and not self._hwnd and not self._error:
            time.sleep(0.01)
        return self.available

    def _message_loop(self):
        try:
            import win32api
            import win32con
            import win32gui
            import win32process

            shell_message = ctypes.windll.user32.RegisterWindowMessageW("SHELLHOOK")
            class_name = f"CowWechatShellHook_{uuid.uuid4().hex}"

            def wnd_proc(hwnd, message, wparam, lparam):
                if message == shell_message and int(wparam) == HSHELL_FLASH:
                    target = int(lparam or 0)
                    try:
                        _, process_id = win32process.GetWindowThreadProcessId(target)
                    except Exception:
                        process_id = 0
                    allowed = {int(value) for value in self._allowed_process_ids()}
                    now = time.time()
                    if process_id in allowed:
                        with self._lock:
                            same_target = self._latest.hwnd == target
                            if not (
                                same_target
                                and now - self._last_accepted_at < self._debounce_seconds
                            ):
                                self._latest = ShellFlashSignal(
                                    True,
                                    created_at=now,
                                    hwnd=target,
                                    process_id=process_id,
                                    reason="windows_shell_hshell_flash",
                                )
                                self._last_accepted_at = now
                                self._wake.set()
                    return 0
                if message == win32con.WM_CLOSE:
                    win32gui.DestroyWindow(hwnd)
                    return 0
                if message == win32con.WM_DESTROY:
                    win32gui.PostQuitMessage(0)
                    return 0
                return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

            window_class = win32gui.WNDCLASS()
            window_class.hInstance = win32api.GetModuleHandle(None)
            window_class.lpszClassName = class_name
            window_class.lpfnWndProc = wnd_proc
            atom = win32gui.RegisterClass(window_class)
            hwnd = win32gui.CreateWindowEx(
                0,
                atom,
                class_name,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                window_class.hInstance,
                None,
            )
            self._hwnd = int(hwnd)
            if not ctypes.windll.user32.RegisterShellHookWindow(hwnd):
                raise ctypes.WinError()
            while not self._stop.is_set():
                win32gui.PumpWaitingMessages()
                self._stop.wait(0.02)
            ctypes.windll.user32.DeregisterShellHookWindow(hwnd)
            if win32gui.IsWindow(hwnd):
                win32gui.DestroyWindow(hwnd)
            win32gui.UnregisterClass(class_name, window_class.hInstance)
        except Exception as exc:
            self._error = str(exc)
            self._wake.set()
        finally:
            self._hwnd = 0

    def wait(self, timeout: float) -> bool:
        return self._wake.wait(max(0.0, float(timeout)))

    def consume(self, after: float = 0.0) -> ShellFlashSignal:
        with self._lock:
            signal = self._latest
            if not signal.signaled or signal.created_at <= float(after or 0):
                return ShellFlashSignal(False)
            self._wake.clear()
            return signal

    def notify_for_tests(self, hwnd: int, process_id: int, created_at: float = 0.0):
        """Inject a validated signal without a native window message."""
        now = float(created_at or time.time())
        allowed = {int(value) for value in self._allowed_process_ids()}
        if int(process_id) not in allowed:
            return
        with self._lock:
            if (
                self._latest.hwnd == int(hwnd)
                and now - self._last_accepted_at < self._debounce_seconds
            ):
                return
            self._latest = ShellFlashSignal(
                True,
                now,
                int(hwnd),
                int(process_id),
                "windows_shell_hshell_flash",
            )
            self._last_accepted_at = now
            self._wake.set()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._hwnd:
            try:
                import win32con
                import win32gui

                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
