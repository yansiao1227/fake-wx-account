"""合并记录图片查看器原像素读取、父窗恢复及失效终结。"""

from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import GeometryControl


def _capture_client(monkeypatch):
    import win32gui
    from PIL import ImageGrab

    client = WechatUiaClient({})
    state = SimpleNamespace(valid=True, calls=[], foreground=300, clicked=False)

    class Image:
        width, height = 1200, 30000

        def save(self, path, **kwargs):
            state.calls.append("save_original")
            path.write_bytes(b"original pixels")

    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(client, "_visible_top_level_window_handles",
                        lambda: {100, 300, 200} if state.clicked else {100, 300})
    monkeypatch.setattr(client, "_find_opened_image_viewer", lambda *args: 200)
    def click(point):
        state.calls.append("open_viewer")
        state.clicked, state.foreground = True, 200

    monkeypatch.setattr(client, "_left_click_point", click)
    monkeypatch.setattr(client, "_paced_wait", lambda *args: None)
    monkeypatch.setattr(client, "_close_image_viewer",
                        lambda *args, **kwargs: state.calls.append("close_viewer") or True)
    monkeypatch.setattr(client._image_viewer, "_native_image_viewer_identity", lambda hwnd, main:
        (200, 42, "图片和视频", "Qt51514QWindowIcon") if hwnd == 200 else None)
    monkeypatch.setattr(client._image_viewer, "_image_viewer_uia_identity",
                        lambda hwnd: ("mmui::XView", "PreviewWindow_test_0_0"))
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: state.foreground)
    monkeypatch.setattr(client._attachment_reader, "_copy_verified_viewer_image", lambda *args, **kwargs: Image())
    monkeypatch.setattr(client._attachment_reader, "_restore_verified_window",
        lambda hwnd, validate: validate() or state.calls.append(("restore", hwnd)) or True)
    monkeypatch.setattr(client, "_restore_main_window_after_viewer", lambda *args: pytest.fail("不能恢复主窗替代记录父窗"))
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda hwnd: (0, 0, 1200, 900))
    monkeypatch.setattr(ImageGrab, "grab", lambda **kwargs: pytest.fail("原图可用时不能取缩小查看器截图"))

    def require():
        if not state.valid:
            raise RuntimeError("attachment_target_changed")

    return client, state, require, Image


def test_native_pixels_restore_verified_record_parent_instead_of_main(monkeypatch, tmp_path):
    client, state, require, _ = _capture_client(monkeypatch)
    metadata = {}
    path = tmp_path / "original.png"
    result = client._capture_image_viewer_from_point((40, 50), path,
        validate_before_click=require, validate_context=require, return_hwnd=300,
        validate_return_window=require, prefer_original=True, capture_metadata=metadata)
    assert result == str(path)
    assert metadata == {"capture_kind": "clipboard_original", "width": 1200, "height": 30000}
    assert state.calls == ["open_viewer", "save_original", "close_viewer", ("restore", 300)]


def test_record_context_invalid_after_capture_never_closes_or_restores_old_windows(monkeypatch, tmp_path):
    client, state, require, Image = _capture_client(monkeypatch)

    class InvalidatingImage(Image):
        def save(self, path, **kwargs):
            super().save(path, **kwargs)
            state.valid = False

    monkeypatch.setattr(client._attachment_reader, "_copy_verified_viewer_image",
                        lambda *args, **kwargs: InvalidatingImage())
    result = client._capture_image_viewer_from_point((40, 50), tmp_path / "original.png",
        validate_before_click=require, validate_context=require, return_hwnd=300,
        validate_return_window=require, prefer_original=True)
    assert result == "" and state.calls == ["open_viewer", "save_original"]


def test_return_window_requires_explicit_ownership_validator_before_any_click(monkeypatch, tmp_path):
    client, state, require, _ = _capture_client(monkeypatch)
    with pytest.raises(RuntimeError, match="image_return_window_validation_required"):
        client._capture_image_viewer_from_point((40, 50), tmp_path / "original.png",
            validate_context=require, return_hwnd=300)
    assert state.calls == []


def test_detection_timeout_still_cleans_verified_new_candidate_and_restores_record(monkeypatch, tmp_path):
    client, state, require, _ = _capture_client(monkeypatch)

    def wait(main, before, **kwargs):
        kwargs["observation"].update(native_identity=(200, 42, "图片和视频", "Qt51514QWindowIcon"),
            uia_identity=("mmui::XView", "PreviewWindow_test_0_0"), attempts=4)
        return 0

    monkeypatch.setattr(client._image_viewer, "_wait_opened_image_viewer", wait)
    metadata = {}
    result = client._capture_image_viewer_from_point((40, 50), tmp_path / "original.png",
        validate_before_click=require, validate_context=require, return_hwnd=300,
        validate_return_window=require, prefer_original=True, capture_metadata=metadata)
    assert result == "" and state.calls == ["open_viewer", "close_viewer", ("restore", 300)]
    assert metadata == {"failure_reason": "image_viewer_detection_timeout", "viewer_left_open": False,
                        "return_window_restored": True, "detection_attempts": 4}


def test_minimized_reusable_viewer_handle_can_be_opened_by_this_click(monkeypatch, tmp_path):
    client, state, require, _ = _capture_client(monkeypatch)
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {100, 300, 200})
    monkeypatch.setattr(client, "_is_image_viewer_window", lambda hwnd, main: hwnd == 200)
    monkeypatch.setattr(client, "_image_viewer_is_open", lambda hwnd: state.clicked)
    path = tmp_path / "original.png"
    assert client._capture_image_viewer_from_point((40, 50), path,
        validate_before_click=require, validate_context=require, return_hwnd=300,
        validate_return_window=require, prefer_original=True) == str(path)
    assert state.calls == ["open_viewer", "save_original", "close_viewer", ("restore", 300)]


def test_existing_visible_viewer_is_not_reopened_or_taken_over(monkeypatch, tmp_path):
    client, state, require, _ = _capture_client(monkeypatch)
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {100, 300, 200})
    monkeypatch.setattr(client, "_is_image_viewer_window", lambda hwnd, main: hwnd == 200)
    monkeypatch.setattr(client, "_image_viewer_is_open", lambda hwnd: True)
    with pytest.raises(RuntimeError, match="chat_record_image_viewer_already_visible"):
        client._capture_image_viewer_from_point((40, 50), tmp_path / "original.png",
            validate_before_click=require, validate_context=require, return_hwnd=300,
            validate_return_window=require, prefer_original=True)
    assert state.calls == []


def test_unidentified_popup_is_not_closed_or_followed_by_record_restore(monkeypatch, tmp_path):
    client, state, require, _ = _capture_client(monkeypatch)
    monkeypatch.setattr(client._image_viewer, "_wait_opened_image_viewer", lambda *args, **kwargs: 0)
    metadata = {}
    result = client._capture_image_viewer_from_point((40, 50), tmp_path / "original.png",
        validate_before_click=require, validate_context=require, return_hwnd=300,
        validate_return_window=require, prefer_original=True, capture_metadata=metadata)
    assert result == "" and state.calls == ["open_viewer"]
    assert metadata["return_window_restored"] is False


def test_copy_returning_none_after_foreground_changed_never_takes_unrelated_screenshot(monkeypatch, tmp_path):
    client, state, require, _ = _capture_client(monkeypatch)

    def copy(*args, **kwargs):
        state.foreground = 999
        return None

    monkeypatch.setattr(client._attachment_reader, "_copy_verified_viewer_image", copy)
    monkeypatch.setattr(client, "_close_image_viewer", lambda *args, **kwargs: False)
    with pytest.raises(RuntimeError, match="image_viewer_identity_changed"):
        client._capture_image_viewer_from_point((40, 50), tmp_path / "original.png",
            validate_before_click=require, validate_context=require, return_hwnd=300,
            validate_return_window=require, prefer_original=True)
    assert state.calls == ["open_viewer"]


def _guarded_close_client(monkeypatch):
    import uiautomation as auto
    import win32api
    import win32gui
    import channel.wechat_desktop.uia.image_viewer as viewer_module

    client = WechatUiaClient({})
    identity = (200, 42, "图片和视频", "Qt51514QWindowIcon")
    state = SimpleNamespace(open=True, valid=True, identity=identity, foreground=200,
                            runtime=(42, 99), now=0., calls=[], on_sleep=None, on_invoke=None)

    class CloseButton(GeometryControl):
        def GetRuntimeId(self):
            return state.runtime

        def GetInvokePattern(self):
            def invoke():
                state.calls.append("invoke")
                if state.on_invoke:
                    state.on_invoke()
            return SimpleNamespace(Invoke=invoke)

        def Click(self, **kwargs):
            state.calls.append("click")
            state.open = False

    close = CloseButton((300, 0, 350, 40), "关闭", "mmui::XButton")
    close.ControlTypeName = "ButtonControl"
    root = GeometryControl((0, 0, 400, 500), children=[close])

    @contextmanager
    def initializer():
        yield

    def sleep(seconds):
        state.now += seconds
        if state.on_sleep:
            state.on_sleep()

    monkeypatch.setattr(auto, "UIAutomationInitializerInThread", initializer)
    monkeypatch.setattr(auto, "ControlFromHandle", lambda hwnd: root)
    monkeypatch.setattr(client, "_image_viewer_is_open", lambda hwnd: state.open)
    monkeypatch.setattr(client._image_viewer, "_native_image_viewer_identity",
                        lambda *args: state.identity if state.open else None)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: state.foreground)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(win32api, "SetCursorPos", lambda point: None)
    monkeypatch.setattr(viewer_module.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(viewer_module.time, "sleep", sleep)

    def require():
        if not state.valid:
            raise RuntimeError("attachment_target_changed")

    return client, state, identity, require, root, close


def test_guarded_close_waits_for_its_own_uia_button_before_invoking(monkeypatch):
    client, state, identity, require, root, close = _guarded_close_client(monkeypatch)
    root._children = []
    state.on_sleep = lambda: root._children.append(close) if not root._children else None
    state.on_invoke = lambda: setattr(state, "open", False)
    assert client._image_viewer._try_close_guarded_image_viewer(200, 100, identity, require)
    assert state.calls == ["invoke"]


@pytest.mark.parametrize("change,error", [
    ("source", "attachment_target_changed"),
    ("window", "image_viewer_identity_changed"),
    ("control", "image_viewer_close_control_changed")])
def test_guarded_close_never_uses_old_control_after_invoke_wait_change(monkeypatch, change, error):
    client, state, identity, require, root, close = _guarded_close_client(monkeypatch)

    def altered():
        if change == "source":
            state.valid = False
        elif change == "window":
            state.identity = (200, 42, "图片和视频", "Qt61514QWindowIcon")
        else:
            state.runtime = (42, 100)

    state.on_sleep = altered
    with pytest.raises(RuntimeError, match=error):
        client._image_viewer._try_close_guarded_image_viewer(200, 100, identity, require)
    assert state.calls == ["invoke"]


def test_guarded_close_rejects_multiple_matching_close_controls(monkeypatch):
    client, state, identity, require, root, close = _guarded_close_client(monkeypatch)
    other = GeometryControl((360, 0, 400, 40), "关闭", "mmui::XButton")
    other.ControlTypeName = "ButtonControl"
    root._children.append(other)
    assert not client._image_viewer._try_close_guarded_image_viewer(200, 100, identity, require)
    assert state.calls == [] and state.now <= 2.


def test_guarded_close_refuses_same_native_window_when_uia_asset_identity_changes(monkeypatch):
    client, state, identity, require, root, close = _guarded_close_client(monkeypatch)
    expected_uia = ("mmui::XView", "PreviewWindow_first_0_0")
    root.ClassName, root.AutomationId = expected_uia
    state.on_sleep = lambda: setattr(root, "AutomationId", "PreviewWindow_second_0_0")
    with pytest.raises(RuntimeError, match="image_viewer_identity_changed"):
        client._image_viewer._try_close_guarded_image_viewer(200, 100, identity, require, expected_uia)
    assert state.calls == ["invoke"]


@pytest.mark.parametrize("clipboard_kind", ["bitmap", "file"])
def test_viewer_clipboard_copy_restores_existing_text_and_yields_full_resolution(monkeypatch, tmp_path, clipboard_kind):
    import win32clipboard
    import win32con
    import win32gui
    import win32process
    from PIL import Image, ImageGrab
    from channel.wechat_desktop.config import DEFAULT_CONFIG

    client = WechatUiaClient({"chat_record_uia_image_max_pixels": 40000000})
    monkeypatch.setitem(DEFAULT_CONFIG, "chat_record_uia_image_max_pixels", 40000000)
    calls = []
    original = Image.new("RGB", (800, 4000))
    original_path = tmp_path / "native-image.png"
    original.save(original_path)
    monkeypatch.setattr(client, "_is_image_viewer_window", lambda *args: True)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 200)
    monkeypatch.setattr(client, "_press_shortcut", lambda *args: calls.append("copy"))
    monkeypatch.setattr(client, "_paced_wait", lambda *args: None)
    monkeypatch.setattr(win32clipboard, "OpenClipboard", lambda: None)
    monkeypatch.setattr(win32clipboard, "CloseClipboard", lambda: None)
    monkeypatch.setattr(win32clipboard, "IsClipboardFormatAvailable", lambda fmt: fmt == win32con.CF_UNICODETEXT)
    monkeypatch.setattr(win32clipboard, "GetClipboardData", lambda fmt: "用户原有剪贴板")
    monkeypatch.setattr(win32clipboard, "EmptyClipboard", lambda: calls.append("empty"))
    monkeypatch.setattr(win32clipboard, "SetClipboardText", lambda value, fmt: calls.append(("restore", value)))
    monkeypatch.setattr(win32clipboard, "GetClipboardOwner", lambda: 200)
    monkeypatch.setattr(win32clipboard, "GetClipboardSequenceNumber", lambda: 1)
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda hwnd: (1, 42))
    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda:
        original if clipboard_kind == "bitmap" else [str(original_path)])
    result = client._attachment_reader._copy_verified_viewer_image(200, 100, validate_context=lambda: None)
    assert result.size == (800, 4000)
    if clipboard_kind == "bitmap":
        assert result is original
    assert calls == ["empty", "copy", "empty", ("restore", "用户原有剪贴板")]
