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
from channel.wechat_desktop.config import DEFAULT_CONFIG
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
            )
            client._press_shortcut(win32api, win32con, ord("C"))
            client._paced_wait(
                "uia_file_clipboard_settle_ms_min",
                "uia_file_clipboard_settle_ms_max",
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
        self, control, content: str, target_root: Path, timeout: float,
        *, validate_target=None, on_file_materialized=None,
    ) -> str:
        """只操作本次微信新打开、来源仍有效的原生另存为对话框。"""
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
        import uiautomation as auto
        import win32gui
        import win32process

        main_hwnd = int(client.get_owner_window_handle() or 0)
        main_pid = int(client.get_owner_window_process_id() or 0)
        if not main_hwnd or not main_pid or not win32gui.IsWindow(main_hwnd):
            return ""
        main_class = win32gui.GetClassName(main_hwnd)

        def require_source():
            if validate_target is not None and validate_target() is False:
                raise RuntimeError("attachment_target_changed")
            if (not win32gui.IsWindow(main_hwnd)
                    or int(client.get_owner_window_handle() or 0) != main_hwnd
                    or int(client.get_owner_window_process_id() or 0) != main_pid
                    or win32gui.GetClassName(main_hwnd) != main_class
                    or int(win32process.GetWindowThreadProcessId(main_hwnd)[1]) != main_pid):
                raise RuntimeError("attachment_target_changed")

        def foreground_identity():
            hwnd = int(win32gui.GetForegroundWindow() or 0)
            if not hwnd or not win32gui.IsWindow(hwnd):
                raise RuntimeError("attachment_save_dialog_changed")
            return (hwnd, int(win32process.GetWindowThreadProcessId(hwnd)[1]),
                    win32gui.GetClassName(hwnd))

        require_source()
        if foreground_identity()[0] != main_hwnd:
            return ""
        # 包括隐藏窗口，不接管用户预先打开的另存为对话框。
        before = set()
        win32gui.EnumWindows(lambda hwnd, _: before.add(int(hwnd)) or True, None)
        left, top, right, bottom = bounds
        require_source()
        if foreground_identity()[0] != main_hwnd:
            return ""
        client._right_click_point(
            (int(left + (right - left) * 0.25), int((top + bottom) / 2))
        )
        client._paced_wait("uia_file_menu_settle_ms_min", "uia_file_menu_settle_ms_max")
        require_source()
        menu_foreground = foreground_identity()
        if (menu_foreground[1] != main_pid
                or (menu_foreground[0] != main_hwnd and menu_foreground[0] in before)):
            return ""
        menu_item = client._find_desktop_control("另存为...", "mmui::XMenuView")
        if menu_item is None:
            return ""
        require_source()
        if (foreground_identity() != menu_foreground
                or int(getattr(menu_item, "ProcessId", 0) or 0) != main_pid):
            return ""
        client._click_and_restore(menu_item)
        client._paced_wait("uia_file_save_dialog_settle_ms_min", "uia_file_save_dialog_settle_ms_max")
        dialog_hwnd, dialog_identity = 0, None
        saved = False
        try:
            require_source()
            dialog_hwnd = int(win32gui.GetForegroundWindow() or 0)
            if (not dialog_hwnd or dialog_hwnd in before
                    or not win32gui.IsWindow(dialog_hwnd)
                    or not win32gui.IsWindowVisible(dialog_hwnd)
                    or win32gui.GetClassName(dialog_hwnd) != "#32770"
                    or int(win32process.GetWindowThreadProcessId(dialog_hwnd)[1]) != main_pid):
                return ""
            dialog = auto.ControlFromHandle(dialog_hwnd)
            dialog_runtime = _runtime_id(dialog)
            if not dialog_runtime or int(getattr(dialog, "ProcessId", 0) or 0) != main_pid:
                return ""
            dialog_identity = (main_pid, main_class, dialog_runtime)

            def require_dialog(*, source=True):
                if source:
                    require_source()
                if (not win32gui.IsWindow(main_hwnd)
                        or win32gui.GetClassName(main_hwnd) != main_class
                        or int(win32process.GetWindowThreadProcessId(main_hwnd)[1]) != main_pid
                        or not win32gui.IsWindow(dialog_hwnd)
                        or not win32gui.IsWindowVisible(dialog_hwnd)
                        or foreground_identity() != (dialog_hwnd, main_pid, "#32770")
                        or _runtime_id(auto.ControlFromHandle(dialog_hwnd)) != dialog_runtime):
                    raise RuntimeError("attachment_save_dialog_changed")

            require_dialog()
            filename_input = next((item for item in client._walk(dialog, 500)
                if _text(item.AutomationId) == "1001"
                and "edit" in _text(item.ControlTypeName).casefold()), None)
            save_button = next((item for item in client._walk(dialog, 500)
                if _text(item.AutomationId) == "1"
                and "button" in _text(item.ControlTypeName).casefold()), None)
            if filename_input is None or save_button is None:
                return ""
            if any(int(getattr(item, "ProcessId", 0) or 0) != main_pid
                   for item in (filename_input, save_button)):
                return ""
            require_dialog()
            filename_input.GetValuePattern().SetValue(str(target))
            require_dialog()
            client._click_and_restore(save_button)
            deadline = time.time() + max(1.0, min(float(timeout), 30.0))
            while time.time() < deadline:
                # 保存会合法更新 packed_info_data。按钮后不再填写或点击，仅
                # 有界读取输出；完成后先核验本次变化、重签，再完整校验来源。
                if (not win32gui.IsWindow(main_hwnd)
                        or win32gui.GetClassName(main_hwnd) != main_class
                        or int(win32process.GetWindowThreadProcessId(main_hwnd)[1]) != main_pid):
                    raise RuntimeError("attachment_target_changed")
                dialog_exists = win32gui.IsWindow(dialog_hwnd)
                if dialog_exists:
                    if (win32gui.GetClassName(dialog_hwnd) != "#32770"
                            or int(win32process.GetWindowThreadProcessId(dialog_hwnd)[1]) != main_pid
                            or _runtime_id(auto.ControlFromHandle(dialog_hwnd)) != dialog_runtime):
                        raise RuntimeError("attachment_save_dialog_changed")
                dialog_open = dialog_exists and win32gui.IsWindowVisible(dialog_hwnd)
                if dialog_open:
                    require_dialog(source=False)
                elif foreground_identity() != (main_hwnd, main_pid, main_class):
                    raise RuntimeError("attachment_save_dialog_changed")
                if not dialog_open and target.is_file() and target.stat().st_size > 0:
                    if on_file_materialized is not None and on_file_materialized(str(target)) is False:
                        raise RuntimeError("attachment_file_materialization_rejected")
                    require_source()
                    logger.info("[WechatDesktop] file saved from native dialog: %s", target)
                    saved = True
                    return str(target)
                time.sleep(0.25)
        finally:
            if not saved and dialog_identity is not None:
                # 只取消本次仍在前台的同一对话框，不抢焦点、不对其它应用按键。
                self._try_cancel_verified_native_dialog(
                    dialog_hwnd, main_hwnd, expected_identity=dialog_identity,
                    validate_target=require_source)
        return ""

    def _try_cancel_verified_native_dialog(
        self, dialog_hwnd: int, main_hwnd: int,
        *, expected_identity=None, validate_target=None,
    ) -> bool:
        """只有本次已固定身份、来源有效且仍在前台的对话框可取消。"""
        client = self.client
        try:
            import uiautomation as auto
            import win32gui
            import win32process

            def valid():
                if (expected_identity is None or not dialog_hwnd or not main_hwnd
                        or int(dialog_hwnd) == int(main_hwnd)
                        or not win32gui.IsWindow(dialog_hwnd)
                        or not win32gui.IsWindowVisible(dialog_hwnd)
                        or not win32gui.IsWindow(main_hwnd)
                        or int(win32gui.GetForegroundWindow() or 0) != int(dialog_hwnd)
                        or win32gui.GetClassName(dialog_hwnd) != "#32770"):
                    return False
                if validate_target is not None and validate_target() is False:
                    return False
                expected_pid, expected_main_class, expected_runtime = expected_identity
                return (int(win32process.GetWindowThreadProcessId(dialog_hwnd)[1]) == int(expected_pid)
                        and int(win32process.GetWindowThreadProcessId(main_hwnd)[1]) == int(expected_pid)
                        and win32gui.GetClassName(main_hwnd) == expected_main_class
                        and _runtime_id(auto.ControlFromHandle(dialog_hwnd)) == expected_runtime)

            if not valid():
                return False
            with auto.UIAutomationInitializerInThread():
                dialog = auto.ControlFromHandle(dialog_hwnd)
                cancel_button = next((item for item in client._walk(dialog, 500)
                    if _text(getattr(item, "AutomationId", "")) == "2"
                    and "button" in _text(getattr(item, "ControlTypeName", "")).casefold()), None)
                if (cancel_button is None or not valid()
                        or int(getattr(cancel_button, "ProcessId", 0) or 0) != int(expected_identity[0])):
                    return False
                cancel_button.GetInvokePattern().Invoke()
            return True
        except Exception as exc:
            logger.warning("[WechatDesktop] verified native dialog cancel failed: %s", exc)
            return False

    def fetch_message_file(
        self, message: UiaChatMessage, tmp_root: Optional[Path] = None,
        *, allow_filename_cache: bool = True, validate_target=None,
        on_file_materialized=None,
    ) -> str:
        """Copy a visible WeChat file bubble's local file into project tmp."""
        client = self.client
        if message.message_type != "file":
            return ""
        strict_identity = None

        def current_identity():
            return (int(client.get_owner_window_handle()), int(client.get_owner_window_process_id()),
                    str(client.get_title().title or ""))

        def require_target():
            if validate_target is not None and validate_target() is False:
                raise RuntimeError("attachment_target_changed")
            if strict_identity is not None and current_identity() != strict_identity:
                raise RuntimeError("attachment_target_changed")

        def find_control(root):
            control = client._find_message_control(root, message)
            if not allow_filename_cache and (
                    control is None or _runtime_id(control) != message.runtime_id):
                return None
            return control

        if not allow_filename_cache:
            if not message.runtime_id:
                return ""
            require_target()
            strict_identity = current_identity()
            if not strict_identity[0] or not strict_identity[1] or not strict_identity[2]:
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
        if (not allow_filename_cache and on_file_materialized is not None
                and client.config.get("uia_file_save_as_first", DEFAULT_CONFIG["uia_file_save_as_first"])
                and client.config.get("uia_file_save_as_enabled", True)):
            # 已有完整原生文件证据时直接右键 SaveAs；左键选文件会在微信 4.1
            # 启动系统 PDF 阅读器，反复尝试复制路径会积累外部应用窗口。
            require_target()
            client.focus_window()
            require_target()
            with client.operation_lock, client._uia_root() as root:
                require_target()
                control = find_control(root)
                if control is None:
                    return ""
                saved = client._save_control_file_as(
                    control, message.content, Path(target_root), max(timeout, 5.0),
                    validate_target=require_target, on_file_materialized=on_file_materialized)
                if not saved:
                    require_target()
                    return ""
                require_target()
                return saved
        while True:
            cached = client._find_cached_message_file(message.content) if allow_filename_cache else ""
            if cached:
                stored = client._store_file_in_tmp(cached, Path(target_root))
                if stored:
                    logger.info("[WechatDesktop] cached file copied to tmp: %s", stored)
                    return stored
            if ui_attempted and allow_filename_cache:
                if time.time() >= deadline:
                    break
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
                continue
            if ui_attempted and time.time() >= deadline:
                break
            first_attempt = not ui_attempted
            ui_attempted = True
            require_target()
            client.focus_window()
            require_target()
            with client.operation_lock, client._uia_root() as root:
                require_target()
                control = find_control(root)
                if control is None:
                    break
                paths = client._copy_control_file_paths(control)
                require_target()
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
                if first_attempt and timeout > 0:
                    download = client._download_button(control)
                    if download is not None:
                        require_target()
                        client._click_and_restore(download)
            if time.time() >= deadline:
                break
            if not allow_filename_cache:
                # 数据库事件必须重读同一已验证气泡复制出的路径；下载完成后也不能
                # 根据跨账号目录中的同名文件推断归属。下载按钮只在首次读取时点击。
                time.sleep(min(0.5, max(0.0, deadline - time.time())))
        if bool(client.config.get("uia_file_save_as_enabled", True)):
            try:
                require_target()
                client.focus_window()
                require_target()
                with client.operation_lock, client._uia_root() as root:
                    require_target()
                    control = find_control(root)
                    if control is not None:
                        require_target()
                        saved = client._save_control_file_as(
                            control,
                            message.content,
                            Path(target_root),
                            max(timeout, 5.0), validate_target=require_target,
                            on_file_materialized=on_file_materialized if not allow_filename_cache else None,
                        )
                        if saved:
                            require_target()
                            return saved
            except Exception as exc:
                logger.warning(
                    "[WechatDesktop] native Save As fallback failed: %s", exc
                )
        if not allow_filename_cache:
            logger.warning("[WechatDesktop] verified file bubble has no attributable local path")
            return ""
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
        *, validate_before_click=None, validate_context=None, return_hwnd=None,
        validate_return_window=None, prefer_original=False, capture_metadata=None,
        image_asset_cache=None,
    ) -> str:
        """点击一个已验证的图片命中区域并截取新打开的微信图片查看器。"""
        client = self.client

        import win32gui
        from PIL import ImageGrab

        if validate_context is not None:
            validate_context()
        main_hwnd = client.get_owner_window_handle()
        if return_hwnd is not None and validate_return_window is None:
            raise RuntimeError("image_return_window_validation_required")
        visible_before = client._visible_top_level_window_handles()
        # Qt 会复用隐藏/最小化的 HWND；最小化窗在 Win32 仍带可见标志，不能当旧的已打开查看器。
        for hwnd in tuple(visible_before):
            if client._is_image_viewer_window(hwnd, main_hwnd):
                if not client._image_viewer_is_open(hwnd):
                    visible_before.discard(hwnd)
                elif return_hwnd is not None:
                    raise RuntimeError("chat_record_image_viewer_already_visible")
        observation = {}
        viewer_hwnd = 0
        captured = False
        context_valid = True
        viewer_closed = False
        main_restored = False
        captured_path = str(target)
        asset_key = None
        asset_metadata = {}
        image_metadata = {}

        def file_digest(path):
            try:
                digest = hashlib.sha256()
                with Path(path).open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                return digest.hexdigest()
            except OSError:
                return ""

        def require_viewer():
            if validate_context is not None and validate_context() is False:
                raise RuntimeError("attachment_target_changed")
            expected = observation.get("verified_identity")
            if (expected is None or client._image_viewer._native_image_viewer_identity(viewer_hwnd, main_hwnd) != expected
                    or int(win32gui.GetForegroundWindow() or 0) != viewer_hwnd
                    or client._image_viewer._image_viewer_uia_identity(viewer_hwnd) != observation.get("uia_identity")):
                raise RuntimeError("image_viewer_identity_changed")

        try:
            if validate_before_click is not None:
                validate_before_click()
            client._left_click_point(point)
            client._paced_wait("uia_image_viewer_settle_ms_min", "uia_image_viewer_settle_ms_max")
            viewer_hwnd = client._image_viewer._wait_opened_image_viewer(main_hwnd, visible_before,
                validate_context=validate_context, observation=observation,
                strict_viewer_identity=True)
            if not viewer_hwnd:
                if capture_metadata is not None:
                    capture_metadata["failure_reason"] = "image_viewer_detection_timeout"
                logger.warning("[WechatDesktop] image viewer UIA not ready within bounded wait")
                return ""
            require_viewer()
            aid = observation["uia_identity"][1]
            if (image_asset_cache is not None and re.fullmatch(
                    r"PreviewWindow_[0-9a-fA-F]{32}_\d+_\d+", aid)):
                # 仅本轮来源校验范围内的完整原生 asset ID；HWND 和窗口标题不作为图片键。
                asset_key = (observation["verified_identity"][1], aid)
                asset_metadata = {"asset_key_hash": hashlib.sha256(aid.encode("utf-8")).hexdigest()[:12],
                                  "asset_cache_hit": False}
                cached = image_asset_cache.get(asset_key)
                if cached:
                    cached_path = Path(cached["path"])
                    if (cached_path.is_file() and cached_path.resolve().parent == target.resolve().parent
                            and cached.get("digest") and file_digest(cached_path) == cached["digest"]):
                        require_viewer()
                        captured_path, captured = str(cached_path), True
                        asset_metadata["asset_cache_hit"] = True
                        image_metadata.update(cached["metadata"])
                        if capture_metadata is not None:
                            capture_metadata.update(image_metadata)
            if capture_metadata is not None:
                capture_metadata.update(asset_metadata)
            if not captured:
                viewer_bounds = tuple(
                    int(value) for value in win32gui.GetWindowRect(viewer_hwnd)
                )
                original = None
                if prefer_original:
                    try:
                        original = self._copy_verified_viewer_image(viewer_hwnd, main_hwnd,
                            validate_context=require_viewer)
                    except Exception as exc:
                        require_viewer()
                        logger.warning("[WechatDesktop] viewer original copy unavailable (%s)", type(exc).__name__)
                # Ctrl+C 返回 None 也必须重新证明查看器身份及前台，不能截其它应用。
                require_viewer()
                image = original if original is not None else ImageGrab.grab(bbox=viewer_bounds, all_screens=True)
                require_viewer()
                if image.width >= 2 and image.height >= 2:
                    image.save(target, format="PNG")
                    captured = True
                    image_metadata.update(capture_kind="clipboard_original" if original is not None else "viewer_screenshot",
                        width=image.width, height=image.height)
                    if capture_metadata is not None:
                        capture_metadata.update(image_metadata)
                    logger.info("[WechatDesktop] image viewer captured to tmp: %s", target)
        finally:
            # The viewer is a separate WeChat top-level window.  Let its UI and
            # image surface stabilize before touching its own close control.
            # In particular, never race a just-opened referenced-image viewer
            # with a global key or a native window-close message.
            client._paced_wait(
                "uia_image_viewer_before_close_ms_min",
                "uia_image_viewer_before_close_ms_max",
            )
            if validate_context is not None:
                try:
                    validate_context()
                except Exception:
                    context_valid = False
            expected = observation.get("verified_identity") or observation.get("native_identity")
            cleanup_hwnd = expected[0] if expected is not None else 0
            if cleanup_hwnd and (context_valid or return_hwnd is None):
                viewer_closed = client._close_image_viewer(cleanup_hwnd, main_hwnd,
                    validate_context=validate_context if context_valid else None, expected_identity=expected,
                    expected_uia_identity=observation.get("uia_identity"))
            if validate_context is not None and context_valid:
                try:
                    if validate_context() is False:
                        context_valid = False
                except Exception:
                    context_valid = False
            # Qt may keep the closed viewer HWND alive while asynchronously
            # transferring activation.  Let that transition finish before
            # bringing the main HWND forward, or Windows can immediately undo
            # our foreground request.
            client._paced_wait(
                "uia_image_viewer_close_settle_ms_min",
                "uia_image_viewer_close_settle_ms_max",
            )
            if not context_valid:
                # 仍仅关闭本次已验证查看器；账号/会话变化后不再抢前台恢复旧窗口。
                pass
            elif not cleanup_hwnd:
                # 没有本次新窗口的身份时不关闭或接管任何旧查看器/其它前台窗口。
                main_restored = bool(return_hwnd is not None
                    and int(win32gui.GetForegroundWindow() or 0) == int(return_hwnd))
                if return_hwnd is None:
                    client._recover_foreground_after_dependency_failure("image viewer detection failure", main_hwnd)
            elif not viewer_closed:
                logger.warning(
                    "[WechatDesktop] image viewer remained open: hwnd=%s",
                    viewer_hwnd,
                )
                if return_hwnd is None:
                    client._recover_foreground_after_dependency_failure(
                        "image viewer close failure", main_hwnd)
            else:
                main_restored = (self._restore_verified_window(int(return_hwnd), validate_return_window)
                    if return_hwnd is not None else client._restore_main_window_after_viewer(main_hwnd))
                if not main_restored:
                    logger.warning(
                        "[WechatDesktop] main window focus was not restored "
                        "after image viewer close: hwnd=%s",
                        main_hwnd,
                    )
                    if return_hwnd is None:
                        client._recover_foreground_after_dependency_failure(
                            "image viewer focus restore failure", main_hwnd)
            if capture_metadata is not None and not (captured and viewer_closed and main_restored and context_valid):
                capture_metadata.setdefault("failure_reason", "image_viewer_close_failed" if not viewer_closed
                    else "image_return_window_not_foreground")
                capture_metadata["viewer_left_open"] = bool(cleanup_hwnd and not viewer_closed)
                capture_metadata["return_window_restored"] = bool(main_restored and context_valid)
                capture_metadata["detection_attempts"] = observation.get("attempts", 0)
        if captured and viewer_closed and main_restored and context_valid:
            if validate_context is not None and validate_context() is False:
                raise RuntimeError("attachment_target_changed")
            if asset_key is not None and not asset_metadata["asset_cache_hit"]:
                digest = file_digest(captured_path)
                if digest:
                    image_asset_cache[asset_key] = {"path": captured_path, "digest": digest,
                        "metadata": dict(image_metadata)}
            return captured_path
        return ""

    def _copy_verified_viewer_image(self, viewer_hwnd, main_hwnd, *, validate_context):
        """仅在已验证查看器前台 Ctrl+C 取得实际像素，随后恢复用户剪贴板。"""
        import win32api
        import win32clipboard
        import win32con
        import win32gui
        import win32process
        from PIL import Image, ImageGrab
        from channel.wechat_desktop.config import DEFAULT_CONFIG

        def require():
            if validate_context is not None and validate_context() is False:
                raise RuntimeError("attachment_target_changed")
            if (not self.client._is_image_viewer_window(viewer_hwnd, main_hwnd)
                    or int(win32gui.GetForegroundWindow() or 0) != viewer_hwnd):
                raise RuntimeError("image_viewer_identity_changed")

        saved = []
        clipboard_restore_allowed = True
        clipboard_sequence = None
        require()
        formats = (win32con.CF_UNICODETEXT, win32con.CF_TEXT, win32con.CF_HDROP,
                   win32con.CF_DIB, getattr(win32con, "CF_DIBV5", 17))
        win32clipboard.OpenClipboard()
        try:
            for fmt in formats:
                try:
                    if win32clipboard.IsClipboardFormatAvailable(fmt):
                        saved.append((fmt, win32clipboard.GetClipboardData(fmt)))
                except Exception:
                    pass
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()
        try:
            require()
            self.client._press_shortcut(win32api, win32con, ord("C"))
            require()
            self.client._paced_wait("uia_file_clipboard_settle_ms_min", "uia_file_clipboard_settle_ms_max")
            require()
            clipboard_owner = int(win32clipboard.GetClipboardOwner() or 0)
            if not clipboard_owner:
                return None
            _, clipboard_pid = win32process.GetWindowThreadProcessId(clipboard_owner)
            _, main_pid = win32process.GetWindowThreadProcessId(main_hwnd)
            if int(clipboard_pid) != int(main_pid):
                clipboard_restore_allowed = False
                return None
            clipboard_sequence = win32clipboard.GetClipboardSequenceNumber()
            image = ImageGrab.grabclipboard()
            require()
            if win32clipboard.GetClipboardSequenceNumber() != clipboard_sequence:
                clipboard_restore_allowed = False
                return None
            if isinstance(image, list) and len(image) == 1:
                # 部分版本复制原图为 CF_HDROP，只读取图像文件，不执行剪贴板路径。
                image_path = Path(str(image[0])).expanduser().resolve()
                if image_path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}:
                    return None
                try:
                    with Image.open(image_path) as source:
                        pixel_budget = int(self.client.config.get("chat_record_uia_image_max_pixels",
                            DEFAULT_CONFIG["chat_record_uia_image_max_pixels"]))
                        if not 1 < source.width * source.height <= pixel_budget:
                            return None
                        image = source.copy()
                except (OSError, ValueError):
                    return None
                require()
            if not hasattr(image, "width") or not hasattr(image, "height"):
                return None
            max_pixels = int(self.client.config.get("chat_record_uia_image_max_pixels",
                DEFAULT_CONFIG["chat_record_uia_image_max_pixels"]))
            return image if 1 < image.width * image.height <= max_pixels else None
        finally:
            if (clipboard_restore_allowed and (clipboard_sequence is None
                    or win32clipboard.GetClipboardSequenceNumber() == clipboard_sequence)):
                win32clipboard.OpenClipboard()
                try:
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
                    win32clipboard.CloseClipboard()
            # 用户/其他应用已更新剪贴板时保留新内容，也不吞掉来源失效异常。

    @staticmethod
    def _restore_verified_window(hwnd, validate):
        import win32con
        import win32gui

        def require():
            if validate() is False:
                raise RuntimeError("image_return_window_changed")

        require()
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        require()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            require()
            if int(win32gui.GetForegroundWindow() or 0) == hwnd:
                return True
            require()
            win32gui.BringWindowToTop(hwnd)
            require()
            win32gui.SetForegroundWindow(hwnd)
            require()
            time.sleep(.03)
        require()
        return int(win32gui.GetForegroundWindow() or 0) == hwnd

    def _bound_image_guard(self, message, validate_target):
        client = self.client
        if not message.runtime_id:
            raise RuntimeError("attachment_control_identity_unavailable")

        def identity():
            return (int(client.get_owner_window_handle()), int(client.get_owner_window_process_id()),
                    str(client.get_title().title or ""))

        if validate_target is not None and validate_target() is False:
            raise RuntimeError("attachment_target_changed")
        expected = identity()
        if not all(expected):
            raise RuntimeError("attachment_target_unavailable")

        def require_context():
            if validate_target is not None and validate_target() is False:
                raise RuntimeError("attachment_target_changed")
            if identity() != expected:
                raise RuntimeError("attachment_target_changed")

        return require_context

    def _bound_image_control(self, root, message):
        control = self.client._find_message_control(root, message)
        if control is None or _runtime_id(control) != message.runtime_id:
            return None
        return control

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
        *, strict: bool = False, validate_target=None,
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
            require_context = self._bound_image_guard(message, validate_target) if strict else None
            if require_context is not None:
                require_context()
            client.focus_window()
            with client.operation_lock, client._uia_root() as root:
                if require_context is not None:
                    require_context()
                control = (self._bound_image_control(root, message) if strict else
                           client._find_message_control(root, message))
                if strict and control is None:
                    return ""
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
            if strict:
                def before_click():
                    require_context()
                    with client.operation_lock, client._uia_root() as root:
                        current = self._bound_image_control(root, message)
                        if current is None or _bounds(current) != click_bounds:
                            raise RuntimeError("attachment_image_control_changed")
                return client._capture_image_viewer_from_point(
                    point, target, validate_before_click=before_click, validate_context=require_context)
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
        *, strict: bool = False, validate_target=None,
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
        require_context = None
        if strict:
            try:
                require_context = self._bound_image_guard(message, validate_target)
            except Exception:
                return ""
        if prefer_viewer and bool(client.config.get("uia_image_viewer_enabled", True)):
            try:
                if require_context is not None:
                    require_context()
                client.focus_window()
                with client.operation_lock, client._uia_root() as root:
                    if require_context is not None:
                        require_context()
                    control = (self._bound_image_control(root, message) if strict else
                               client._find_message_control(root, message))
                    if strict and control is None:
                        return ""
                    click_bounds = client._image_activation_bounds(
                        control, _bounds(control) if strict else message.bounds
                    )
                    if not click_bounds:
                        raise RuntimeError("image activation bounds are unavailable")
                    click_left, click_top, click_right, click_bottom = click_bounds
                    point = (
                        int((click_left + click_right) / 2),
                        int((click_top + click_bottom) / 2),
                    )
                if strict:
                    def before_click():
                        require_context()
                        with client.operation_lock, client._uia_root() as root:
                            current = self._bound_image_control(root, message)
                            current_bounds = (client._image_activation_bounds(current, _bounds(current))
                                              if current is not None else None)
                            if current_bounds != click_bounds:
                                raise RuntimeError("attachment_image_control_changed")
                    captured = client._capture_image_viewer_from_point(
                        point, target, validate_before_click=before_click, validate_context=require_context)
                else:
                    captured = client._capture_image_viewer_from_point(point, target)
                if captured:
                    return captured
                if strict:
                    return ""
            except Exception as exc:
                if strict:
                    return ""
                logger.warning(
                    "[WechatDesktop] image viewer capture failed; using bubble: %s",
                    exc,
                )
        try:
            if strict:
                require_context()
                client.focus_window()
                with client.operation_lock, client._uia_root() as root:
                    require_context()
                    control = self._bound_image_control(root, message)
                    if control is None:
                        return ""
                    current_bounds = client._image_activation_bounds(control, _bounds(control))
                    if not current_bounds:
                        return ""
                    left, top, right, bottom = current_bounds
                import win32gui
                if int(win32gui.GetForegroundWindow()) != int(client.get_owner_window_handle()):
                    return ""
            # Bounding rectangles returned by UIA use virtual-screen coordinates.
            # Pillow's all_screens flag preserves negative coordinates on multi-monitor
            # Windows setups and captures only the image-card anchor, not the full row.
            from PIL import ImageGrab

            image = ImageGrab.grab(
                bbox=(left, top, right, bottom),
                all_screens=True,
            )
            if require_context is not None:
                require_context()
                with client.operation_lock, client._uia_root() as root:
                    current = self._bound_image_control(root, message)
                    bounds = (client._image_activation_bounds(current, _bounds(current))
                              if current is not None else None)
                    if bounds != (left, top, right, bottom):
                        return ""
                if int(win32gui.GetForegroundWindow()) != int(client.get_owner_window_handle()):
                    return ""
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
