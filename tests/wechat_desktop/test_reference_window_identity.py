"""图片查看器打开期间，引用读取仍绑定同一个微信主窗口。"""

from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageGrab

from channel.wechat_desktop.models import HeaderInfo, UiaChatMessage, UiaReferencedMessage
from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import GeometryControl
from .test_window_discovery import install_window_api


def test_strict_reference_capture_keeps_main_identity_while_viewer_is_visible(monkeypatch, tmp_path):
    main = {"hwnd": 101, "pid": 201, "class": "Qt51514QWindowIcon",
            "exe": "Weixin.exe", "title": "微信"}
    viewer = {"hwnd": 102, "pid": 201, "class": "Qt51514QWindowIcon",
              "exe": "Weixin.exe", "title": "图片和视频", "visible": False}
    install_window_api(monkeypatch, [main, viewer])
    import win32gui

    windows = {101: main, 102: viewer}
    monkeypatch.setattr(win32gui, "GetWindowText", lambda hwnd: windows[hwnd]["title"], raising=False)
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: hwnd in windows, raising=False)
    monkeypatch.setattr(win32gui, "IsIconic", lambda hwnd: False, raising=False)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 102 if viewer["visible"] else 101, raising=False)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda hwnd: (0, 0, 200, 200), raising=False)
    control = GeometryControl((10, 10, 300, 140), "这是什么引用 Synthetic 的消息 : 图片",
                              "mmui::ChatTextItemView")
    control.GetRuntimeId = lambda: (42, 10)
    root = GeometryControl((0, 0, 800, 800), children=[control])
    message = UiaChatMessage("Synthetic", "这是什么", "text", "incoming", "42.10",
                             (10, 10, 300, 140),
                             reference=UiaReferencedMessage("Synthetic", "图片", "image"))
    client = WechatUiaClient({})
    actions = []

    @contextmanager
    def fake_root():
        yield root

    def activate(point):
        viewer["visible"] = True
        actions.append("open")

    def capture(**kwargs):
        # 不替换 _window/get_owner_window_handle：截图时仍真实执行主窗口枚举。
        assert client._window() == (101, 201)
        assert viewer["visible"]
        actions.append("capture")
        return Image.new("RGB", (200, 200), "red")

    def close(hwnd):
        assert hwnd == 102
        viewer["visible"] = False
        actions.append("close")
        return True

    viewer_uia = ("mmui::XView", "PreviewWindow_0123456789abcdef0123456789abcdef_0_0")

    def guarded_close(hwnd, main_hwnd, expected_identity, validate_context,
                      expected_uia_identity=None):
        assert hwnd == 102 and main_hwnd == 101
        assert expected_identity == (102, 201, "图片和视频", "Qt51514QWindowIcon")
        assert expected_uia_identity == viewer_uia
        validate_context()
        assert client._image_viewer._native_image_viewer_identity(hwnd, main_hwnd) == expected_identity
        assert win32gui.GetForegroundWindow() == hwnd
        return close(hwnd)

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "get_title", lambda: HeaderInfo("Synthetic", "private"))
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(client, "_left_click_point", activate)
    monkeypatch.setattr(client, "_has_image_viewer_close_control", lambda hwnd: hwnd == 102)
    monkeypatch.setattr(client._image_viewer, "_image_viewer_uia_identity", lambda hwnd: viewer_uia)
    monkeypatch.setattr(client._image_viewer, "_try_close_guarded_image_viewer", guarded_close)
    monkeypatch.setattr(client, "_restore_main_window_after_viewer", lambda hwnd: hwnd == 101)
    monkeypatch.setattr(ImageGrab, "grab", capture)

    result = client.fetch_referenced_message_image(message, tmp_path, strict=True)

    assert result and Path(result).is_file()
    assert actions == ["open", "capture", "close"]
    assert client._window() == (101, 201)
