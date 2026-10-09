"""微信消息粘贴、发送与气泡验证。客户端显式注入，锁仍由客户端统一持有。"""
from __future__ import annotations
from channel.wechat_desktop.config import DEFAULT_CONFIG
import hashlib
import time
from collections import Counter
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
                "uia_key_event_settle_ms_min",
                "uia_key_event_settle_ms_max",
            )
            check_send_allowed()
            win32api.keybd_event(key, 0, 0, 0)
            win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
            client._paced_wait(
                "uia_key_event_settle_ms_min",
                "uia_key_event_settle_ms_max",
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
                )
            client._press_shortcut(win32api, win32con, ord("A"))
            client._press_shortcut(win32api, win32con, ord("V"))

        def input_is_clear(control) -> bool:
            available, value = client._try_input_value(control)
            return available and not value

        def reject_paste(control, actual_text: str, value_available: bool) -> None:
            nonlocal input_readable_seen
            logger.warning(
                "[WechatDesktop] paste attempt %s/%s did not match reply text: "
                "expected_chars=%s actual_chars=%s keyboard_focus=%s value_pattern=%s",
                attempt, attempts, len(expected_text), len(actual_text),
                client._keyboard_focus_state(control), value_available,
            )
            # 错误草稿既不能提交，也不能留给下次 Enter 回退。每次失败都
            # 清空输入，然后重新获取控件和粘贴；此处尚未标记发送提交。
            check_send_allowed()
            control.SetFocus()
            if client._wait_for_keyboard_focus(control) is False:
                raise SendNotSubmitted("WeChat input focus could not be confirmed")
            client._press_shortcut(win32api, win32con, ord("A"))
            win32api.keybd_event(win32con.VK_BACK, 0, 0, 0)
            win32api.keybd_event(win32con.VK_BACK, 0, win32con.KEYEVENTF_KEYUP, 0)
            clear_available, _ = client._try_input_value(control)
            input_readable_seen = input_readable_seen or clear_available
            if clear_available and not client._wait_for_input_value(
                control, lambda _: input_is_clear(control), 1.25
            ):
                raise SendNotSubmitted("WeChat mismatched draft could not be cleared")
            if attempt < attempts:
                client._paced_wait("uia_paste_retry_ms_min", "uia_paste_retry_ms_max")

        # Conversation selection and clipboard setup can take long enough for
        # another window to steal focus. Reassert WeChat immediately before
        # keyboard input and the physical Send-button click.
        attempts = max(1, min(int(client.config.get("uia_paste_attempts", 3)), 5))
        input_readable_seen = False
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
                input_readable_seen = input_readable_seen or value_available
                actual_text = ""

                def capture_expected(value: str) -> bool:
                    nonlocal actual_text
                    actual_text = value
                    return (client._normalize_input_text(value)
                            == client._normalize_input_text(expected_text))

                accepted = (
                    not expected_text
                    or not value_available
                    or client._wait_for_input_value(control, capture_expected, 1.25)
                )
                if not accepted:
                    reject_paste(control, actual_text, value_available)
                    continue
                client._paced_wait(
                    "uia_paste_settle_ms_min",
                    "uia_paste_settle_ms_max",
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
                    )
                if expected_text:
                    current_available, current_text = client._try_input_value(control)
                    input_readable_seen = input_readable_seen or current_available
                    if ((input_readable_seen and not current_available)
                            or (current_available
                                and client._normalize_input_text(current_text)
                                != client._normalize_input_text(expected_text))):
                        reject_paste(control, current_text, current_available)
                        continue
                    # ValuePattern 可能刚恢复；一旦已确认可读，也必须验证
                    # 发送后输入清空。始终不可读时保留初次发送兼容路径。
                    value_available = current_available
                if send_button is not None:
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
                        control, lambda _: input_is_clear(control), 1.5
                    )
                ):
                    raise RuntimeError("WeChat Send button did not clear the reply input")
                return

        raise SendNotSubmitted(
            f"WeChat chat input did not match reply text after {attempts} attempts"
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
        # OUTSIDE ``uia_section``/``operation_lock``, so independent attachment
        # reads keep running while this sender sleeps. Each
        # chunk then performs its bounded UI work (focus, locate, paste,
        # verify) inside an exclusive UIA section; the per-chunk
        # focus/locate/title checks reject a target changed between sections.
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
                            before = client.get_send_bubble_snapshot(conversation=who, limit=5)
                            with client._clipboard(unicode_text=chunk):
                                try:
                                    check_send_allowed()
                                    client._paste_and_send(expected_text=chunk)
                                    attempt.submitted = True
                                except RuntimeError as exc:
                                    if "did not clear the reply input" not in str(exc):
                                        raise
                                    # ValuePattern 可能滞后于已执行的发送点击。
                                    # 未确认气泡也不能证明未发送，禁止再次按 Enter。
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
                    except Exception as exc:
                        if results or attempt.submitted:
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
                        before = client.get_send_bubble_snapshot(conversation=who, limit=5)
                        with client._clipboard(files=paths):
                            check_send_allowed()
                            client._paste_and_send()
                            attempt.submitted = True
                        result = client._verify_send(who, before, expected_type="file")
                    now = time.monotonic()
                    client._last_send_at = now
                    client._last_conversation_send[str(who)] = now
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
        def identity(item):
            if item.runtime_id:
                return ("runtime", item.runtime_id)
            if item.stable_id:
                return ("stable", item.stable_id)
            return None

        def signature(item):
            # 引用解析状态、附件本地路径、位置和发送者均可能补齐，
            # 不能作为气泡新增的证据。只保留展示正文和消息类型。
            return (item.message_type, item.content)

        before_ids = {identity(item) for item in before if identity(item)}
        before_counts = Counter(signature(item) for item in before)
        before_outgoing = Counter(signature(item) for item in before if item.direction == "outgoing")
        before_unknown = Counter(signature(item) for item in before
                                 if item.direction not in {"incoming", "outgoing"})
        before_has_anonymous = any(not identity(item) for item in before)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            time.sleep(0.15)
            after = client.get_send_bubble_snapshot(conversation=who, limit=5)
            after_counts = Counter(signature(item) for item in after)
            after_outgoing = Counter(signature(item) for item in after if item.direction == "outgoing")
            snapshot_increased = (len(after) > len(before)
                                  and all(after_counts[key] >= count
                                          for key, count in before_counts.items()))
            for item in reversed(after):
                if item.direction != "outgoing":
                    continue
                key = signature(item)
                count_increased = (snapshot_increased
                                   and after_counts[key] > before_counts[key]
                                   and after_outgoing[key] > before_outgoing[key] + before_unknown[key])
                item_id = identity(item)
                # 匿名气泡必须同时证明旧签名全部保留、快照总数以及
                # 同签名 outgoing 数量超过旧出站与未知方向气泡的总数，
                # 避免识别完善与无关新消息组合成假新增证据。最近 5 条窗口
                # 已满时无法证明新增，保守保留 unverified。
                # 原匿名气泡后来暴露 ID，也不能仅凭新 ID 误认为新增。
                if item_id:
                    is_new = (item_id not in before_ids
                              and (not before_has_anonymous or count_increased))
                else:
                    is_new = count_increased
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
