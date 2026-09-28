"""附件读取：文件缓存与另存为、图片查看器截图；复用客户端的锁和身份校验。"""

from __future__ import annotations
import hashlib
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional
from common.log import logger
from channel.wechat_desktop.models import UiaChatMessage
from channel.wechat_desktop.uia.controls import _text, _bounds, _runtime_id


class WechatAttachmentReader:
    def __init__(self, client):
        self.client = client

    def _copy_control_file_paths(self, control) -> list[str]:
        """Copy a file bubble and return CF_HDROP paths, restoring the clipboard."""
        client = self.client
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
            client._click_and_restore(control)
            client._paced_wait(
                "uia_file_selection_settle_ms_min",
                "uia_file_selection_settle_ms_max",
                100,
                200,
            )
            client._press_shortcut(win32api, win32con, ord("C"))
            client._paced_wait(
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

    def _download_button(self, file_control):
        client = self.client
        for control in client._walk(file_control, 100):
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
        client = self.client
        roots = []
        configured = _text(client.config.get("wechat_files_dir"))
        if configured:
            roots.append(Path(os.path.expandvars(os.path.expanduser(configured))))
        roots.extend(
            [
                Path.home() / "Documents" / "xwechat_files",
                Path.home() / "Documents" / "WeChat Files",
            ]
        )
        roots.extend(client._xwechat_configured_file_roots())
        try:
            import win32api
            import win32con
            import win32process

            _hwnd, process_id = client._window()
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
            roots.append(executable.parent.parent.parent / "Documents" / "xwechat_files")
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

    @staticmethod
    def _xwechat_configured_file_roots() -> list[Path]:
        """Read WeChat 4's custom documents path from xwechat config ini files."""
        appdata = os.environ.get("APPDATA") or ""
        config_dir = Path(appdata) / "Tencent" / "xwechat" / "config"
        roots: list[Path] = []
        try:
            if not config_dir.is_dir():
                return roots
            for ini in config_dir.glob("*.ini"):
                try:
                    raw = ini.read_text(encoding="utf-8", errors="ignore").strip()
                except OSError:
                    continue
                if not raw:
                    continue
                documents = Path(raw.splitlines()[0].strip())
                nested = documents / "xwechat_files"
                if nested.is_dir():
                    roots.append(nested)
                elif documents.is_dir():
                    roots.append(documents)
        except OSError:
            return roots
        return roots

    def _find_cached_message_file(self, content: str) -> str:
        client = self.client
        filename, expected_size, size_token = client._file_card_fields(content)
        if not filename:
            return ""
        wanted_key = client._cached_file_name_key(filename)
        if not wanted_key[0] or not wanted_key[1]:
            return ""
        size_limit = (
            client._cached_file_size_limit(size_token, expected_size)
            if expected_size is not None
            else 0
        )
        candidates = []
        for root in client._wechat_data_roots():
            patterns = (
                "*/msg/file/*/*",
                "*/*/msg/file/*/*",
                "*/FileStorage/File/*/*",
            )
            for pattern in patterns:
                try:
                    paths = root.glob(pattern)
                    for path in paths:
                        if (
                            not path.is_file()
                            or client._cached_file_name_key(path.name) != wanted_key
                        ):
                            continue
                        stat = path.stat()
                        size_gap = (
                            abs(stat.st_size - expected_size)
                            if expected_size is not None
                            else 0
                        )
                        if expected_size is not None and size_gap > size_limit:
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
        client = self.client
        filename, _expected_size = client._file_card_metadata(content)
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
        main_hwnd = client.get_owner_window_handle()
        left, top, right, bottom = bounds
        client._right_click_point(
            (int(left + (right - left) * 0.25), int((top + bottom) / 2))
        )
        client._paced_wait(
            "uia_file_menu_settle_ms_min",
            "uia_file_menu_settle_ms_max",
            250,
            450,
        )
        menu_item = client._find_desktop_control("另存为...", "mmui::XMenuView")
        if menu_item is None:
            client._recover_foreground_after_dependency_failure(
                "missing Save As menu"
            )
            return ""
        client._click_and_restore(menu_item)
        client._paced_wait(
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
                    for item in client._walk(dialog, 500)
                    if _text(item.AutomationId) == "1001"
                    and "edit" in _text(item.ControlTypeName).casefold()
                ),
                None,
            )
            save_button = next(
                (
                    item
                    for item in client._walk(dialog, 500)
                    if _text(item.AutomationId) == "1"
                    and "button" in _text(item.ControlTypeName).casefold()
                ),
                None,
            )
            if filename_input is None or save_button is None:
                return ""
            filename_input.GetValuePattern().SetValue(str(target))
            client._click_and_restore(save_button)
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
                client._try_cancel_verified_native_dialog(dialog_hwnd, main_hwnd)
                client._recover_foreground_after_dependency_failure(
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
        client = self.client

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
                        for item in client._walk(dialog, 500)
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
        client = self.client
        if message.message_type != "file":
            return ""
        target_root = tmp_root or (
            Path(__file__).resolve().parents[2] / "tmp" / "wechat_files"
        )
        timeout = (
            max(0.0, min(float(client.config.get("uia_file_download_timeout_seconds", 10)), 30.0))
            if bool(client.config.get("uia_file_download_enabled", True))
            else 0.0
        )
        deadline = time.time() + timeout
        ui_attempted = False
        while True:
            cached = client._find_cached_message_file(message.content)
            if cached:
                stored = client._store_file_in_tmp(cached, Path(target_root))
                if stored:
                    logger.info("[WechatDesktop] cached file copied to tmp: %s", stored)
                    return stored
            if ui_attempted:
                if time.time() >= deadline:
                    break
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
                continue
            ui_attempted = True
            client.focus_window()
            with client.operation_lock, client._uia_root() as root:
                control = client._find_message_control(root, message)
                if control is None:
                    break
                paths = client._copy_control_file_paths(control)
                for path in paths:
                    try:
                        stored = client._store_file_in_tmp(path, Path(target_root))
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
                    download = client._download_button(control)
                    if download is not None:
                        client._click_and_restore(download)
            if time.time() >= deadline:
                break
        if bool(client.config.get("uia_file_save_as_enabled", True)):
            try:
                client.focus_window()
                with client.operation_lock, client._uia_root() as root:
                    control = client._find_message_control(root, message)
                    if control is not None:
                        saved = client._save_control_file_as(
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
        filename, expected_size = client._file_card_metadata(message.content)
        logger.warning(
            "[WechatDesktop] file bubble was detected but no local file path "
            "was available: name=%s expected_size=%s roots=%s content=%s",
            filename,
            expected_size,
            [str(root) for root in client._wechat_data_roots()],
            message.content,
        )
        return ""

    def _capture_image_viewer_from_point(
        self,
        point: tuple[int, int],
        target: Path,
    ) -> str:
        """点击一个已验证的图片命中区域并截取新打开的微信图片查看器。"""
        client = self.client

        import win32gui
        from PIL import ImageGrab

        main_hwnd = client.get_owner_window_handle()
        visible_before = client._visible_top_level_window_handles()
        client._left_click_point(point)
        client._paced_wait(
            "uia_image_viewer_settle_ms_min",
            "uia_image_viewer_settle_ms_max",
            500,
            900,
        )
        viewer_hwnd = client._find_opened_image_viewer(main_hwnd, visible_before)
        if not viewer_hwnd:
            logger.warning(
                "[WechatDesktop] image click did not open a strictly "
                "verified viewer"
            )
            client._recover_foreground_after_dependency_failure(
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
            client._paced_wait(
                "uia_image_viewer_before_close_ms_min",
                "uia_image_viewer_before_close_ms_max",
                300,
                500,
            )
            viewer_closed = client._close_image_viewer(viewer_hwnd, main_hwnd)
            # Qt may keep the closed viewer HWND alive while asynchronously
            # transferring activation.  Let that transition finish before
            # bringing the main HWND forward, or Windows can immediately undo
            # our foreground request.
            client._paced_wait(
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
                client._recover_foreground_after_dependency_failure(
                    "image viewer close failure",
                    main_hwnd,
                )
            else:
                main_restored = client._restore_main_window_after_viewer(main_hwnd)
                if not main_restored:
                    logger.warning(
                        "[WechatDesktop] main window focus was not restored "
                        "after image viewer close: hwnd=%s",
                        main_hwnd,
                    )
                    client._recover_foreground_after_dependency_failure(
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
        client = self.client

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
            client.focus_window()
            with client.operation_lock, client._uia_root() as root:
                control = client._find_message_control(root, message)
                click_bounds = _bounds(control) if control is not None else message.bounds
                if not click_bounds:
                    return ""
                point = client._reference_image_activation_point(click_bounds)
            # Leave the main-window UIA initializer before clicking.  Viewer
            # discovery creates its own UIA context; nesting the two contexts
            # while WeChat is creating a top-level viewer can deadlock COM.
            logger.info(
                "[WechatDesktop] referenced image activation prepared: "
                "point=%s bounds=%s",
                point,
                click_bounds,
            )
            return client._capture_image_viewer_from_point(point, target)
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
        client = self.client
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
        if prefer_viewer and bool(client.config.get("uia_image_viewer_enabled", True)):
            try:
                client.focus_window()
                with client.operation_lock, client._uia_root() as root:
                    control = client._find_message_control(root, message)
                    click_bounds = client._image_activation_bounds(
                        control, message.bounds
                    )
                    if not click_bounds:
                        raise RuntimeError("image activation bounds are unavailable")
                    click_left, click_top, click_right, click_bottom = click_bounds
                    point = (
                        int((click_left + click_right) / 2),
                        int((click_top + click_bottom) / 2),
                    )
                captured = client._capture_image_viewer_from_point(point, target)
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
