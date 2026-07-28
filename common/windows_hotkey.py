"""Minimal Windows global-hotkey listener used for emergency shutdown."""

from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Optional


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012


@dataclass(frozen=True)
class ParsedHotkey:
    modifiers: int
    virtual_key: int
    display: str


def parse_hotkey(value: str) -> ParsedHotkey:
    parts = [part.strip().casefold() for part in str(value or "").split("+")]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("hotkey is empty")
    modifier_map = {
        "alt": MOD_ALT,
        "ctrl": MOD_CONTROL,
        "control": MOD_CONTROL,
        "shift": MOD_SHIFT,
        "win": MOD_WIN,
        "windows": MOD_WIN,
    }
    modifiers = MOD_NOREPEAT
    key_name = ""
    for part in parts:
        if part in modifier_map:
            modifiers |= modifier_map[part]
        elif key_name:
            raise ValueError("hotkey must contain exactly one non-modifier key")
        else:
            key_name = part
    if not key_name:
        raise ValueError("hotkey requires a non-modifier key")
    if len(key_name) == 1 and key_name.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        virtual_key = ord(key_name.upper())
        display_key = key_name.upper()
    elif key_name.startswith("f") and key_name[1:].isdigit():
        function_number = int(key_name[1:])
        if not 1 <= function_number <= 24:
            raise ValueError("function key must be between F1 and F24")
        virtual_key = 0x70 + function_number - 1
        display_key = f"F{function_number}"
    else:
        special_keys = {"esc": (0x1B, "Esc"), "escape": (0x1B, "Esc"), "pause": (0x13, "Pause")}
        if key_name not in special_keys:
            raise ValueError(f"unsupported hotkey key: {key_name!r}")
        virtual_key, display_key = special_keys[key_name]
    display_parts = []
    if modifiers & MOD_CONTROL:
        display_parts.append("Ctrl")
    if modifiers & MOD_ALT:
        display_parts.append("Alt")
    if modifiers & MOD_SHIFT:
        display_parts.append("Shift")
    if modifiers & MOD_WIN:
        display_parts.append("Win")
    display_parts.append(display_key)
    return ParsedHotkey(modifiers, virtual_key, "+".join(display_parts))


class WindowsGlobalHotkey:
    """Own a RegisterHotKey message loop on a daemon thread."""

    def __init__(self, hotkey: ParsedHotkey, callback: Callable[[], None]):
        self.hotkey = hotkey
        self.callback = callback
        self.registered = False
        self.error = ""
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self, ready_timeout: float = 2.0) -> bool:
        if os.name != "nt":
            self.error = "global hotkeys are only supported on Windows"
            return False
        self._thread = threading.Thread(
            target=self._message_loop,
            name="cowagent-emergency-hotkey",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(max(0.1, ready_timeout))
        return self.registered

    def _message_loop(self):
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = int(kernel32.GetCurrentThreadId())
        hotkey_id = 0xCA11
        try:
            self.registered = bool(
                user32.RegisterHotKey(
                    None,
                    hotkey_id,
                    self.hotkey.modifiers,
                    self.hotkey.virtual_key,
                )
            )
            if not self.registered:
                self.error = (
                    "RegisterHotKey failed with Windows error "
                    f"{int(kernel32.GetLastError())}"
                )
                return
            message = wintypes.MSG()
            self._ready.set()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam == hotkey_id:
                    self.callback()
                    break
        except Exception as exc:
            self.error = str(exc)
        finally:
            if self.registered:
                user32.UnregisterHotKey(None, hotkey_id)
            self.registered = False
            self._ready.set()

    def stop(self):
        if self._thread_id and self._thread and self._thread.is_alive():
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(timeout=2)
