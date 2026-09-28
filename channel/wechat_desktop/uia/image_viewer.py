"""图片查看器：窗口归属验证、安全关闭与主窗口恢复。"""

from __future__ import annotations
import time
from common.log import logger
from channel.wechat_desktop.uia.controls import _text


class WechatImageViewer:
    def __init__(self, client):
        self.client = client

    def _find_image_viewer_close_control(self, root):
        """在图片查看器自身的 UIA 树中查找关闭按钮。

        微信 4.1.9.30 实测将其暴露为：
        ``Name=关闭``、``ClassName=mmui::XButton``、
        ``ControlTypeName=ButtonControl``。英文标题作为兼容项保留。
        """
        client = self.client
        controls = [root, *client._walk(root, 300)]
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
        client = self.client

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
        client = self.client

        try:
            import uiautomation as auto

            with auto.UIAutomationInitializerInThread():
                root = auto.ControlFromHandle(viewer_hwnd)
                return client._find_image_viewer_close_control(root) is not None
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
        client = self.client

        import win32gui

        foreground = int(win32gui.GetForegroundWindow() or 0)
        native_candidates: list[int] = []

        def callback(hwnd, _):
            native_hwnd = int(hwnd or 0)
            if (
                native_hwnd
                and native_hwnd not in visible_before
                and win32gui.IsWindowVisible(native_hwnd)
                and client._is_image_viewer_window(native_hwnd, main_hwnd)
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
            if client._has_image_viewer_close_control(hwnd)
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
        client = self.client

        import uiautomation as auto
        import win32api

        with auto.UIAutomationInitializerInThread():
            root = auto.ControlFromHandle(viewer_hwnd)
            close_button = client._find_image_viewer_close_control(root)
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
                    client._image_viewer_is_open(viewer_hwnd)
                    and time.monotonic() < invoke_deadline
                ):
                    time.sleep(0.03)
                if client._image_viewer_is_open(viewer_hwnd):
                    close_button.Click(simulateMove=False, waitTime=0.1)
            finally:
                win32api.SetCursorPos(cursor)
        if not client._image_viewer_is_open(viewer_hwnd):
            return True
        close_deadline = time.monotonic() + 1.0
        while (
            client._image_viewer_is_open(viewer_hwnd)
            and time.monotonic() < close_deadline
        ):
            time.sleep(0.05)
        return not client._image_viewer_is_open(viewer_hwnd)

    def _close_image_viewer(self, viewer_hwnd: int, main_hwnd: int) -> bool:
        """只点击图片查看器自己的关闭按钮，绝不关闭任何原生窗口。

        全局 Esc 和 WM_CLOSE 都可能在焦点或窗口身份变化时作用于微信主窗口，
        因而这里不提供键盘或窗口消息兜底。UIA 关闭失败时宁可留下查看器，
        也不能终止用户的微信会话。
        """
        client = self.client

        import win32gui

        if not client._is_image_viewer_window(viewer_hwnd, main_hwnd):
            logger.warning(
                "[WechatDesktop] refused to close an unverified image viewer: "
                "viewer_hwnd=%s main_hwnd=%s",
                viewer_hwnd,
                main_hwnd,
            )
            return False

        try:
            closed = client._try_close_image_viewer_with_uia(viewer_hwnd)
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
