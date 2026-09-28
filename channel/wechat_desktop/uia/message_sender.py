"""微信消息粘贴、发送与气泡验证。客户端显式注入，锁仍由客户端统一持有。"""
from __future__ import annotations
from channel.wechat_desktop.config import DEFAULT_CONFIG
import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional
from common.log import logger
from channel.wechat_desktop.models import UiaChatMessage
from channel.wechat_desktop.send_control import (
    SendCancelled, SendNotSubmitted, check_send_allowed, mark_send_submitted,
    send_lock, track_send_attempt,
)
from channel.wechat_desktop.uia.operations import conversation_titles_match

from channel.wechat_desktop.uia.controls import INPUT_ID, _text, _encode_cf_hdrop


class WechatMessageSender:
    def __init__(self, client):
        self.client = client

    @contextmanager
    def _clipboard(self, unicode_text: Optional[str] = None, files: Optional[Iterable[str]] = None):
        client = self.client
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


    def _wait_for_input_value(self, control, predicate, timeout: float) -> bool:
        client = self.client
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if predicate(client._input_value(control)):
                return True
            time.sleep(0.05)
        return predicate(client._input_value(control))


    def _wait_for_keyboard_focus(self, control, timeout: float = 0.25) -> Optional[bool]:
        client = self.client
        state = client._keyboard_focus_state(control)
        if state is None or state:
            return state
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            time.sleep(0.025)
            state = client._keyboard_focus_state(control)
            if state is None or state:
                return state
        return client._keyboard_focus_state(control)


    def _press_shortcut(self, win32api, win32con, key: int) -> None:
        client = self.client
        check_send_allowed()
        win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
        try:
            client._paced_wait(
                "uia_key_event_settle_ms_min", "uia_key_event_settle_ms_max", 20, 40,
            )
            check_send_allowed()
            win32api.keybd_event(key, 0, 0, 0)
            win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
            client._paced_wait(
                "uia_key_event_settle_ms_min", "uia_key_event_settle_ms_max", 20, 40,
            )
        finally:
            win32api.keybd_event(
                win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0
            )


    def _click_send_button(self, send_button):
        """Click the visible Send button while restoring the user's cursor.

        WeChat 4.1.9.30 exposes InvokePattern on this button, but some environments
        acknowledge Invoke() without actually sending. A real button click is
        therefore the primary operation.
        """
        client = self.client
        import win32api

        cursor = None
        try:
            cursor = win32api.GetCursorPos()
        except Exception:
            pass
        try:
            mark_send_submitted()
            send_button.Click(simulateMove=False, waitTime=0.1)
        finally:
            if cursor is not None:
                try:
                    win32api.SetCursorPos(cursor)
                except Exception:
                    pass


    def _paste_and_send(self, expected_text: str = ""):
        client = self.client
        import win32api
        import win32con

        def paste_into(control) -> None:
            control.SetFocus()
            client._paced_wait(
                "uia_input_focus_settle_ms_min",
                "uia_input_focus_settle_ms_max",
                50,
                100,
            )
            focus_state = client._wait_for_keyboard_focus(control)
            if focus_state is False:
                logger.warning(
                    "[WechatDesktop] chat input did not report keyboard focus; "
                    "retrying SetFocus before paste"
                )
                control.SetFocus()
                client._paced_wait(
                    "uia_input_focus_settle_ms_min",
                    "uia_input_focus_settle_ms_max",
                    50,
                    100,
                )
            client._press_shortcut(win32api, win32con, ord("A"))
            client._press_shortcut(win32api, win32con, ord("V"))

        # Conversation selection and clipboard setup can take long enough for
        # another window to steal focus. Reassert WeChat immediately before
        # keyboard input and the physical Send-button click.
        attempts = max(1, min(int(client.config.get("uia_paste_attempts", 3)), 5))
        for attempt in range(1, attempts + 1):
            check_send_allowed()
            client.focus_window()
            with client._uia_root() as root:
                control = next(
                    (
                        item
                        for item in client._walk(root)
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
                value_available, _value = client._try_input_value(control)
                actual_text = ""

                def capture_nonempty(value: str) -> bool:
                    nonlocal actual_text
                    actual_text = value
                    return bool(value)

                accepted = (
                    not expected_text
                    or not value_available
                    or client._wait_for_input_value(control, capture_nonempty, 1.25)
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
                        client._keyboard_focus_state(control),
                        value_available,
                    )
                    if attempt < attempts:
                        client._paced_wait(
                            "uia_paste_retry_ms_min",
                            "uia_paste_retry_ms_max",
                            150,
                            300,
                        )
                    continue

                if (
                    expected_text
                    and value_available
                    and client._normalize_input_text(actual_text)
                    != client._normalize_input_text(expected_text)
                ):
                    logger.warning(
                        "[WechatDesktop] pasted reply text differs from the source; "
                        "continuing because the input is non-empty: expected=%r actual=%r",
                        expected_text,
                        actual_text,
                    )
                client._paced_wait(
                    "uia_paste_settle_ms_min",
                    "uia_paste_settle_ms_max",
                    150,
                    300,
                )
                send_button = next(
                    (
                        item
                        for item in client._walk(root)
                        if _text(item.ClassName) == "mmui::XOutlineButton"
                        and _text(item.Name) in {"发送", "Send"}
                    ),
                    None,
                )
                if send_button is not None:
                    client._paced_wait(
                        "uia_pre_send_settle_ms_min",
                        "uia_pre_send_settle_ms_max",
                        100,
                        250,
                    )
                    client._click_send_button(send_button)
                else:
                    mark_send_submitted()
                    win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
                    win32api.keybd_event(
                        win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0
                    )
                if (
                    expected_text
                    and value_available
                    and not client._wait_for_input_value(
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
        client = self.client
        text = str(message or "")
        if not text:
            raise ValueError("text is empty")
        chunk_limit = max(
            100,
            min(int(client.config.get("uia_text_chunk_chars", DEFAULT_CONFIG["uia_text_chunk_chars"])), 4000),
        )
        chunks = client._split_message_text(text, chunk_limit)
        # ``_send_lock`` only serializes competing senders. Humanized pacing
        # (send interval + per-conversation cooldown) waits under it but
        # OUTSIDE ``uia_section``/``operation_lock``, so conversation scanning
        # and attachment reads keep running while this sender sleeps. Each
        # chunk then performs its bounded UI work (focus, locate, paste,
        # verify) inside an exclusive UIA section; the per-chunk
        # focus/locate/title checks make interleaved scans safe.
        with send_lock(client._send_lock):
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
                with track_send_attempt() as attempt:
                    try:
                        check_send_allowed()
                        if not expedited:
                            client._wait_for_send_slot(who)
                        with client.uia_section(), send_lock(client.operation_lock):
                            check_send_allowed()
                            client.focus_window()
                            if not client.locate_conversation(
                                who, runtime_id, row_index
                            ):
                                raise RuntimeError(
                                    f"Conversation is not visible: {who}"
                                )
                            # Defense in depth: never paste into a detail pane that
                            # still shows a different chat title (failed session
                            # switch).
                            active = client.get_title()
                            if not conversation_titles_match(active.title, who):
                                raise RuntimeError(
                                    f"Active chat is {active.title!r}, expected {who!r}"
                                )
                            before = client.get_chat_history(limit=5)
                            with client._clipboard(unicode_text=chunk):
                                try:
                                    check_send_allowed()
                                    client._paste_and_send(expected_text=chunk)
                                    attempt.submitted = True
                                except RuntimeError as exc:
                                    if "did not clear the reply input" not in str(exc):
                                        raise
                                    # The UIA ValuePattern can lag behind a
                                    # successful click. Verify first so the
                                    # fallback cannot send a duplicate, then
                                    # reacquire the input before Enter.
                                    result = client._verify_send(
                                        who, before, text=chunk
                                    )
                                    if not result.get("verified"):
                                        check_send_allowed()
                                        if not client._send_existing_input_with_enter(
                                            chunk
                                        ):
                                            raise
                                        result = client._verify_send(
                                            who, before, text=chunk
                                        )
                                        if not result.get("verified"):
                                            raise exc
                                else:
                                    result = client._verify_send(
                                        who, before, text=chunk
                                    )
                        results.append(result)
                        attempt.submitted = False  # 当前段已计入 results，不重复计算。
                        if not expedited:
                            now = time.monotonic()
                            client._last_send_at = now
                            client._last_conversation_send[str(who)] = now
                        if result.get("verified"):
                            client.remember_outgoing_message(
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
                            client.remember_outgoing_message(who, chunk)
                    except Exception as exc:
                        if results or attempt.submitted:
                            if attempt.submitted:
                                client.remember_outgoing_message(who, chunk)
                            return self._interrupted_result(results, len(chunks), attempt.submitted, exc)
                        if isinstance(exc, SendCancelled):
                            raise
                        raise SendNotSubmitted(
                            f"WeChat text chunk {index}/{len(chunks)} failed: {exc}"
                        ) from exc
            verified = all(result.get("verified") for result in results)
            return {
                "success": True,
                "verified": verified,
                "chunks": len(chunks),
                "submitted_chunks": len(results),
                "verified_chunks": sum(bool(result.get("verified")) for result in results),
                "chunk_results": results,
                "message": "" if verified else "one or more outgoing chunks were not verified",
            }


    def send_file(
        self,
        who: str,
        files: Iterable[str],
        runtime_id: str = "",
        row_index: int = -1,
    ) -> dict:
        client = self.client
        paths = [str(Path(path).resolve()) for path in files]
        if not paths or any(not Path(path).is_file() for path in paths):
            raise ValueError("one or more files do not exist")
        with send_lock(client._send_lock):
            with track_send_attempt() as attempt:
                try:
                    # Pacing waits stay outside the UIA section (see send_message).
                    client._wait_for_send_slot(who)
                    with client.uia_section(), send_lock(client.operation_lock):
                        check_send_allowed()
                        client.focus_window()
                        if not client.locate_conversation(who, runtime_id, row_index):
                            raise RuntimeError(f"Conversation is not visible: {who}")
                        before = client.get_chat_history(limit=5)
                        with client._clipboard(files=paths):
                            check_send_allowed()
                            client._paste_and_send()
                            attempt.submitted = True
                        result = client._verify_send(who, before, expected_type="file")
                    now = time.monotonic()
                    client._last_send_at = now
                    client._last_conversation_send[str(who)] = now
                    if result.get("verified"):
                        client.remember_outgoing_message(
                            who, runtime_id=str(result.get("runtime_id") or "")
                        )
                    return result
                except Exception as exc:
                    if attempt.submitted:
                        return self._interrupted_result([], 1, True, exc)
                    if isinstance(exc, SendCancelled):
                        raise
                    raise SendNotSubmitted(str(exc)) from exc


    @staticmethod
    def _interrupted_result(results: list[dict], chunks: int, submitted: bool, error: Exception) -> dict:
        """保留逐段结果；已提交但未验证的动作不能自动重放。"""
        verified_count = sum(bool(result.get("verified")) for result in results)
        return {
            "success": False,
            "verified": False,
            "status": "partial" if verified_count else "uncertain",
            "chunks": chunks,
            "submitted_chunks": len(results) + int(submitted),
            "verified_chunks": verified_count,
            "chunk_results": list(results),
            "retryable": False,
            "message": f"Send interrupted after {len(results)} completed chunks: {error}",
        }


    def _verify_send(
        self,
        who: str,
        before: list[UiaChatMessage],
        text: str = "",
        expected_type: str = "",
    ) -> dict:
        client = self.client
        before_ids = {item.runtime_id for item in before if item.runtime_id}
        deadline = time.time() + 3.0
        while time.time() < deadline:
            time.sleep(0.15)
            after = client.get_chat_history(limit=5)
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


