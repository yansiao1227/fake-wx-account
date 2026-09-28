"""当前聊天记录窗口的读取与关闭。客户端显式注入，锁仍由客户端统一持有。"""
from __future__ import annotations
import time
from common.log import logger
from channel.wechat_desktop.models import HeaderInfo, WechatHistoryMessage, WechatHistoryReadError, WechatHistoryReadResult
from channel.wechat_desktop.uia.operations import conversation_titles_match



class WechatHistoryReader:
    def __init__(self, client):
        self.client = client

    def _find_history_window(
        self, main_hwnd: int, process_id: int
    ) -> tuple[int, object, object] | None:
        client = self.client
        try:
            import uiautomation as auto
        except ImportError as exc:
            raise WechatHistoryReadError(
                "uia_error", "uiautomation is not installed"
            ) from exc

        native_candidates = client._history_window_native_candidates(
            main_hwnd, process_id
        )
        verified = []
        with auto.UIAutomationInitializerInThread():
            for hwnd in native_candidates:
                controls = client._verified_history_root(hwnd)
                if controls is not None:
                    verified.append((hwnd, controls[0], controls[1]))
        if len(verified) == 1:
            return verified[0]
        return None


    def _close_history_window(self, history_hwnd: int, main_hwnd: int) -> bool:
        client = self.client
        import win32con
        import win32gui

        if not history_hwnd or history_hwnd == main_hwnd:
            return False
        timeout = max(
            0.25,
            min(
                float(client.config.get("wechat_history_close_timeout_seconds", 2.0)),
                5.0,
            ),
        )
        win32gui.PostMessage(history_hwnd, win32con.WM_CLOSE, 0, 0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not client._history_window_is_open(history_hwnd):
                return client._restore_main_window_after_viewer(main_hwnd)
            time.sleep(0.05)
        return False


    def read_current_chat_history(
        self, limit: int = 20
    ) -> WechatHistoryReadResult:
        """打开独立聊天记录窗口并读取当前会话最近的消息。"""

        client = self.client
        if not bool(client.config.get("wechat_history_read_enabled", True)):
            raise WechatHistoryReadError(
                "history_read_disabled", "WeChat history reading is disabled."
            )
        max_messages = max(
            1, min(int(client.config.get("wechat_history_max_messages", 50)), 200)
        )
        requested_limit = max(1, min(int(limit), max_messages))
        main_hwnd = 0
        history_hwnd = 0
        close_failed = False
        warnings = []
        header = HeaderInfo("", "unknown", 1)
        try:
            client.focus_window()
            main_hwnd, process_id = client._window()
            header = client.get_title()
            if not header.title:
                raise WechatHistoryReadError(
                    "current_conversation_unavailable",
                    "No active WeChat conversation is open.",
                )
            with client.operation_lock, client._uia_root() as root:
                _active_title, message_list = client._read_active_chat_state(root)
                if message_list is None or not conversation_titles_match(
                    _active_title, header.title
                ):
                    raise WechatHistoryReadError(
                        "current_conversation_unavailable",
                        "The current WeChat conversation is unavailable.",
                    )
                button = client._find_chat_history_button(root)
                if button is None:
                    raise WechatHistoryReadError(
                        "history_button_unavailable",
                        "WeChat chat history button is unavailable.",
                    )
                client._click_and_restore(button)

            open_timeout = max(
                0.5,
                min(
                    float(
                        client.config.get("wechat_history_open_timeout_seconds", 4.0)
                    ),
                    10.0,
                ),
            )
            opened = None
            deadline = time.monotonic() + open_timeout
            while time.monotonic() < deadline and opened is None:
                opened = client._find_history_window(main_hwnd, process_id)
                if opened is None:
                    time.sleep(0.1)
            if opened is None:
                raise WechatHistoryReadError(
                    "history_window_unavailable",
                    "WeChat chat history window could not be opened.",
                )
            history_hwnd, _history_root, history_list = opened

            total_timeout = max(
                open_timeout,
                min(
                    float(
                        client.config.get("wechat_history_total_timeout_seconds", 8.0)
                    ),
                    30.0,
                ),
            )
            read_deadline = time.monotonic() + total_timeout
            max_scrolls = max(
                0, min(int(client.config.get("wechat_history_max_scrolls", 12)), 100)
            )
            no_progress_limit = max(
                1,
                min(
                    int(client.config.get("wechat_history_no_progress_limit", 2)), 10
                ),
            )
            messages: dict[str, WechatHistoryMessage] = {}
            no_progress = 0
            exhausted = False
            reached_limit = False
            for scroll_index in range(max_scrolls + 1):
                before = len(messages)
                for identity, message in client._read_history_rows(history_list):
                    messages.setdefault(identity, message)
                if len(messages) >= requested_limit:
                    reached_limit = True
                    break
                if len(messages) == before:
                    no_progress += 1
                else:
                    no_progress = 0
                if no_progress >= no_progress_limit:
                    exhausted = True
                    break
                if scroll_index >= max_scrolls or time.monotonic() >= read_deadline:
                    break
                if not client._scroll_history_table_older(history_list):
                    exhausted = True
                    break
                client._paced_wait(
                    "wechat_history_scroll_settle_ms_min",
                    "wechat_history_scroll_settle_ms_max",
                )
                refreshed = client._find_history_window(main_hwnd, process_id)
                if refreshed is None or int(refreshed[0]) != int(history_hwnd):
                    raise WechatHistoryReadError(
                        "history_window_unavailable",
                        "WeChat chat history window disappeared while reading.",
                    )
                _history_root, history_list = refreshed[1], refreshed[2]

            result_messages = list(messages.values())
            indexed_messages = list(enumerate(result_messages))
            indexed_messages.sort(
                key=lambda item: (
                    item[1].timestamp is None,
                    item[1].timestamp or "",
                    -item[0],
                )
            )
            result_messages = [message for _index, message in indexed_messages]
            result_messages = result_messages[-requested_limit:]
            degraded = any(message.degraded for message in result_messages)
            if degraded:
                warnings.append(
                    "Some history rows did not expose a complete parseable timestamp."
                )
            has_more = None
            if exhausted:
                has_more = False
            elif reached_limit:
                has_more = True
            return WechatHistoryReadResult(
                conversation_title=header.title,
                conversation_type=header.header_type,
                messages=result_messages,
                requested_limit=requested_limit,
                returned_count=len(result_messages),
                has_more=has_more,
                degraded=degraded,
                warnings=tuple(warnings),
            )
        finally:
            if history_hwnd:
                close_failed = not client._close_history_window(
                    history_hwnd, main_hwnd
                )
            if close_failed:
                logger.warning(
                    "[WechatDesktop][history] verified history window could not be closed: hwnd=%s",
                    history_hwnd,
                )
