"""微信分享浏览器的打开、读取与恢复。客户端显式注入，锁仍由客户端统一持有。"""
from __future__ import annotations
import re
import time
from common.log import logger
from channel.wechat_desktop.models import UiaChatMessage

from channel.wechat_desktop.uia.controls import _text, _bounds


class WechatShareBrowser:
    def __init__(self, client):
        self.client = client

    def _verified_share_browser_controls(self, browser_hwnd: int, main_hwnd: int):
        client = self.client
        try:
            import uiautomation as auto

            if not client._is_verified_share_browser_window(
                browser_hwnd, main_hwnd
            ):
                return None, None
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(browser_hwnd)
                return client._find_share_browser_controls(root)
        except Exception:
            return None, None


    def _find_opened_share_browser(
        self, main_hwnd: int, visible_before: set[int]
    ) -> int:
        """定位独立或右侧嵌入式的微信分享页 WebView。"""
        client = self.client
        import win32gui

        embedded = client._find_embedded_share_browser(main_hwnd)
        if embedded:
            return embedded

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
            if client._is_verified_share_browser_window(value, main_hwnd)
        ]
        if foreground in candidates:
            return foreground
        return candidates[0] if len(candidates) == 1 else 0


    def _find_embedded_share_browser(self, main_hwnd: int) -> int:
        """返回新版微信右侧分享页面板的原生窗口句柄。

        新版客户端不再为分享页单独创建可由 ``EnumWindows`` 稳定枚举的
        顶层窗口；页面作为主窗口中的 ``Chrome_WidgetWin_0`` 出现。通过
        文档控件和右侧位置双重约束，避免将其它 Chromium 浮层误判为分享页。
        """
        client = self.client
        try:
            import uiautomation as auto

            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(main_hwnd)
                root_bounds = _bounds(root)
                if not root_bounds:
                    return 0
                left, _top, right, _bottom = root_bounds
                width = max(1, right - left)
                candidates: list[tuple[int, tuple[int, int, int, int]]] = []
                for control in [root, *client._walk(root, 256)]:
                    if _text(getattr(control, "ClassName", "")) != "Chrome_WidgetWin_0":
                        continue
                    handle = int(getattr(control, "NativeWindowHandle", 0) or 0)
                    bounds = _bounds(control)
                    if not handle or handle == int(main_hwnd) or not bounds:
                        continue
                    # 侧边栏从主窗口右半部分展开，并承载网页 DocumentControl。
                    if bounds[0] < left + width * 0.45:
                        continue
                    descendants = [control, *client._walk(control, 96)]
                    if not any(
                        "document" in _text(
                            getattr(item, "ControlTypeName", "")
                        ).casefold()
                        for item in descendants
                    ):
                        continue
                    candidates.append((handle, bounds))
                if not candidates:
                    return 0
                # 同时存在多个 WebView 时，选择最靠右的侧边页面。
                candidates.sort(key=lambda item: (item[1][0], item[1][2]), reverse=True)
                return candidates[0][0]
        except Exception as exc:
            logger.debug(
                "[WechatDesktop][share-browser] embedded panel probe failed: %s",
                exc,
            )
            return 0


    def _click_copy_link_menu(self, browser_hwnd: int) -> bool:
        client = self.client
        menu_item = client._find_desktop_control("复制链接", "mmui::XMenuView")
        if menu_item is None:
            try:
                import uiautomation as auto

                desktop = auto.GetRootControl()
                matches = [
                    control
                    for control in [desktop, *client._walk(desktop, 1200)]
                    if _text(getattr(control, "Name", "")).casefold()
                    in {"复制链接", "copy link"}
                    and "xmenu"
                    in _text(getattr(control, "ClassName", "")).casefold()
                ]
                menu_item = matches[0] if len(matches) == 1 else None
            except Exception:
                menu_item = None
        if menu_item is not None:
            return client._click_control_point(menu_item)
        # Some WebView builds do not expose menu text through UIA. OCR is only
        # allowed inside the already verified browser window and must recognize
        # the exact first-item label before a click is issued.
        try:
            import numpy as np
            import win32gui
            from PIL import ImageGrab

            bounds = tuple(int(value) for value in win32gui.GetWindowRect(browser_hwnd))
            image = ImageGrab.grab(bbox=bounds, all_screens=True)
            engine = client._group_sender_ocr._get_engine()
            if engine is None:
                return False
            output = engine(np.asarray(image))
            lines = client._group_sender_ocr._output_lines(output, (bounds[0], bounds[1]))
            matches = [
                line
                for line in lines
                if line.text.strip().casefold() in {"复制链接", "copy link"}
                and line.score >= 0.8
            ]
            if len(matches) != 1:
                return False
            line = matches[0]
            client._left_click_point(
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
        client = self.client
        import win32con
        import win32gui

        # The HWND has already passed the strict WeChatAppEx verification.
        # Do not enumerate the dynamic WebView UIA tree again just to find its
        # titlebar: on feed pages that scan can block for tens of seconds.
        timeout = max(
            0.25,
            min(
                float(
                    client.config.get(
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
                if client._stop_event.wait(0.1):
                    break
            if closed:
                break
        restored = bool(closed and client._restore_main_window_after_viewer(main_hwnd))
        if restored:
            logger.info(
                "[WechatDesktop][share-browser] closed hwnd=%s", browser_hwnd
            )
        return restored


    def fetch_referenced_share_url(self, message: UiaChatMessage) -> str:
        """Open a verified share-card quote and copy its browser URL."""

        client = self.client
        _content, url = client._fetch_referenced_share(message, direct_read=False)
        return url


    def fetch_referenced_share_page(
        self, message: UiaChatMessage
    ) -> tuple[str, str]:
        """Read a quoted share card in WeChat, falling back to its URL."""

        client = self.client
        return client._fetch_referenced_share(message, direct_read=True)


    def _fetch_referenced_share(
        self, message: UiaChatMessage, *, direct_read: bool
    ) -> tuple[str, str]:
        """Open a verified share-card quote and return page text plus fallback URL."""

        client = self.client
        reference = message.reference
        if (
            reference is None
            or reference.message_type != "share_card"
            or not message.bounds
        ):
            return "", ""
        try:
            client.focus_window()
            with client.operation_lock, client._uia_root() as root:
                control = client._find_message_control(root, message)
                click_bounds = _bounds(control) if control is not None else message.bounds
                if not click_bounds:
                    return "", ""
                point = client._reference_image_activation_point(click_bounds)
            return client._fetch_share_from_point(point, direct_read=direct_read)
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] failed to prepare share quote activation: %s",
                exc,
            )
            return "", ""


    def _read_share_browser_content(self, browser_hwnd: int) -> str:
        """Read the rendered document from WeChat UIA, then use clipboard fallback."""

        client = self.client
        value = client._wait_for_share_browser_content(browser_hwnd)
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
            client._left_click_point(content_point)
            client._paced_wait(
                "uia_share_browser_direct_read_settle_ms_min",
                "uia_share_browser_direct_read_settle_ms_max",
                300,
                600,
            )

            def copy_page():
                client._press_shortcut(win32api, win32con, ord("A"))
                client._press_shortcut(win32api, win32con, ord("C"))
                client._paced_wait(
                    "uia_share_browser_clipboard_settle_ms_min",
                    "uia_share_browser_clipboard_settle_ms_max",
                    200,
                    450,
                )

            value = client._normalize_share_browser_content(
                client._clipboard_unicode_after(copy_page)
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

        client = self.client
        try:
            import uiautomation as auto

            maximum = max(
                1,
                int(
                    client.config.get(
                        "uia_share_browser_direct_read_max_chars", 50000
                    )
                ),
            )
            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(browser_hwnd)
                document = None
                for control in [root, *client._walk(root, 128)]:
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
                        value = client._normalize_share_browser_content(
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
                for control in client._walk(document, 256):
                    control_type = _text(
                        getattr(control, "ControlTypeName", "")
                    ).casefold()
                    if "text" not in control_type:
                        continue
                    name = _text(getattr(control, "Name", ""))
                    if name and (not names or names[-1] != name):
                        names.append(name)
                return client._normalize_share_browser_content("\n".join(names))
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

        client = self.client
        timeout = max(
            0.0,
            min(
                float(client.config.get("uia_share_browser_load_timeout_seconds", 6)),
                30.0,
            ),
        )
        poll_ms = max(
            50,
            min(int(client.config.get("uia_share_browser_load_poll_ms", 250)), 2000),
        )
        minimum_wait = max(
            0.0,
            min(
                int(client.config.get("uia_share_browser_load_min_wait_ms", 800)),
                10000,
            )
            / 1000.0,
        )
        stable_required = max(
            1,
            min(
                int(client.config.get("uia_share_browser_content_stable_polls", 2)),
                10,
            ),
        )
        ready_chars = max(
            int(client.config.get("uia_share_browser_direct_read_min_chars", 20)),
            int(client.config.get("uia_share_browser_direct_read_ready_chars", 80)),
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
            value = client._read_share_browser_uia_content_once(browser_hwnd)
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
            if client._stop_event.wait(poll_ms / 1000.0):
                raise RuntimeError("WeChat UI Automation is stopping")


    def _normalize_share_browser_content(self, value: str) -> str:
        client = self.client
        value = str(value or "").replace("\x00", "").replace("\r\n", "\n").strip()
        value = re.sub(r"[\t ]{3,}", "  ", value)
        value = re.sub(r"\n{4,}", "\n\n\n", value)
        minimum = max(
            1,
            int(client.config.get("uia_share_browser_direct_read_min_chars", 20)),
        )
        is_url_only = bool(
            re.fullmatch(r"https?://[^\s]+", value, re.IGNORECASE)
            and client._is_safe_public_url(value)
        )
        if len(value) < minimum or is_url_only:
            return ""
        maximum = max(
            minimum,
            int(client.config.get("uia_share_browser_direct_read_max_chars", 50000)),
        )
        return value[:maximum]


    def fetch_share_url_from_point(self, point: tuple[int, int]) -> str:
        """Open a share card at a verified message point and copy its URL."""

        client = self.client
        _content, url = client._fetch_share_from_point(point, direct_read=False)
        return url


    def fetch_share_page_from_point(
        self, point: tuple[int, int]
    ) -> tuple[str, str]:
        """Read a share card in WeChat, copying its URL only as fallback."""

        client = self.client
        return client._fetch_share_from_point(point, direct_read=True)


    def _fetch_share_from_point(
        self, point: tuple[int, int], *, direct_read: bool
    ) -> tuple[str, str]:
        """Open one verified share browser and return rendered text or fallback URL."""

        client = self.client
        main_hwnd = client.get_owner_window_handle()
        visible_before = client._visible_top_level_window_handles()
        browser_hwnd = 0
        try:
            logger.info(
                "[WechatDesktop][share-browser] opening card direct_read=%s",
                direct_read,
            )
            client._left_click_point(point)
            client._paced_wait(
                "uia_share_browser_open_settle_ms_min",
                "uia_share_browser_open_settle_ms_max",
                600,
                1200,
            )
            browser_hwnd = client._find_opened_share_browser(
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
                client.config.get("uia_share_browser_direct_read_enabled", True)
            ):
                content = client._read_share_browser_content(browser_hwnd)
                if content:
                    return content, ""
                logger.info(
                    "[WechatDesktop][share-browser] direct read unavailable "
                    "hwnd=%s; falling back to copy link",
                    browser_hwnd,
                )
            more, _close = client._verified_share_browser_controls(
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
                client._right_click_point(more_point)
                client._paced_wait(
                    "uia_share_browser_menu_settle_ms_min",
                    "uia_share_browser_menu_settle_ms_max",
                    250,
                    500,
                )
                if not client._click_copy_link_menu(browser_hwnd):
                    return
                client._paced_wait(
                    "uia_share_browser_clipboard_settle_ms_min",
                    "uia_share_browser_clipboard_settle_ms_max",
                    200,
                    450,
                )

            value = client._clipboard_unicode_after(copy_action)
            if not client._is_safe_public_url(value):
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
                if not client._close_share_browser(browser_hwnd, main_hwnd):
                    logger.warning(
                        "[WechatDesktop] verified share browser could not be closed"
                    )
            else:
                client._recover_foreground_after_dependency_failure(
                    "share browser detection failure", main_hwnd
                )


