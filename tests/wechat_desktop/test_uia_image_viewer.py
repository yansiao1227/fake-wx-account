"""图片查看器归属验证、关闭与主窗口恢复。"""

from contextlib import contextmanager
from types import SimpleNamespace
from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import GeometryControl


def test_image_viewer_uia_close_clicks_when_invoke_is_a_noop(monkeypatch):
    import sys
    import win32api
    import win32gui

    alive = {200: True}
    calls = []

    class InvokePattern:
        def Invoke(self):
            calls.append("invoke")

    class CloseButton(GeometryControl):
        def GetInvokePattern(self):
            return InvokePattern()

        def Click(self, **kwargs):
            calls.append(("click", kwargs))
            alive[200] = False

    close = CloseButton((900, 0, 950, 50), "关闭", "mmui::XButton")
    close.ControlTypeName = "ButtonControl"
    root = GeometryControl(
        (0, 0, 1000, 800),
        "Weixin",
        "mmui::XView",
        children=[close],
    )

    @contextmanager
    def uia_thread():
        yield

    fake_auto = SimpleNamespace(
        UIAutomationInitializerInThread=uia_thread,
        ControlFromHandle=lambda hwnd: root,
    )
    ticks = iter([0.0, 1.0])
    monkeypatch.setitem(sys.modules, "uiautomation", fake_auto)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(
        win32api,
        "SetCursorPos",
        lambda point: calls.append(("restore", point)),
    )
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: alive.get(hwnd, False))
    monkeypatch.setattr(
        win32gui, "IsWindowVisible", lambda hwnd: alive.get(hwnd, False)
    )
    monkeypatch.setattr(
        "channel.wechat_desktop.uia.client.time.monotonic",
        lambda: next(ticks),
    )

    assert WechatUiaClient({})._try_close_image_viewer_with_uia(200) is True
    assert calls == [
        "invoke",
        ("click", {"simulateMove": False, "waitTime": 0.1}),
        ("restore", (10, 20)),
    ]


def test_image_viewer_uia_close_accepts_hidden_reusable_hwnd(monkeypatch):
    import sys
    import win32api
    import win32gui

    visible = {200: True}
    calls = []

    class InvokePattern:
        def Invoke(self):
            calls.append("invoke")
            visible[200] = False

    class CloseButton(GeometryControl):
        def GetInvokePattern(self):
            return InvokePattern()

        def Click(self, **_kwargs):
            raise AssertionError("a hidden reusable viewer must not be clicked twice")

    close = CloseButton((900, 0, 950, 50), "关闭", "mmui::XButton")
    close.ControlTypeName = "ButtonControl"
    root = GeometryControl(
        (0, 0, 1000, 800),
        "Weixin",
        "mmui::XView",
        children=[close],
    )

    @contextmanager
    def uia_thread():
        yield

    monkeypatch.setitem(
        sys.modules,
        "uiautomation",
        SimpleNamespace(
            UIAutomationInitializerInThread=uia_thread,
            ControlFromHandle=lambda _hwnd: root,
        ),
    )
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(
        win32api,
        "SetCursorPos",
        lambda point: calls.append(("restore", point)),
    )
    monkeypatch.setattr(win32gui, "IsWindow", lambda _hwnd: True)
    monkeypatch.setattr(
        win32gui, "IsWindowVisible", lambda hwnd: visible.get(hwnd, False)
    )

    assert WechatUiaClient({})._try_close_image_viewer_with_uia(200) is True
    assert calls == ["invoke", ("restore", (10, 20))]


def test_restore_after_viewer_targets_visible_non_minimized_main_window(
    monkeypatch,
):
    import win32con
    import win32gui

    foreground = {"hwnd": 200}
    main = {"visible": False, "iconic": True}
    calls = []

    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: hwnd in {100, 200})
    monkeypatch.setattr(
        win32gui,
        "IsWindowVisible",
        lambda hwnd: main["visible"] if hwnd == 100 else True,
    )
    monkeypatch.setattr(
        win32gui, "IsIconic", lambda hwnd: main["iconic"] if hwnd == 100 else False
    )
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: foreground["hwnd"])

    def show_window(hwnd, command):
        calls.append(("show", hwnd, command))
        main.update(visible=True, iconic=False)

    def set_foreground(hwnd):
        calls.append(("foreground", hwnd))
        foreground["hwnd"] = hwnd

    monkeypatch.setattr(win32gui, "ShowWindow", show_window)
    monkeypatch.setattr(
        win32gui,
        "BringWindowToTop",
        lambda hwnd: calls.append(("top", hwnd)),
    )
    monkeypatch.setattr(win32gui, "SetForegroundWindow", set_foreground)

    assert WechatUiaClient._restore_main_window_after_viewer(100) is True
    assert calls == [
        ("show", 100, win32con.SW_RESTORE),
        ("top", 100),
        ("foreground", 100),
    ]


def test_image_viewer_close_rejects_unverified_window(monkeypatch):
    import win32gui

    client = WechatUiaClient({})
    posted = []
    monkeypatch.setattr(client, "_is_image_viewer_window", lambda *_args: False)
    monkeypatch.setattr(
        client,
        "_try_close_image_viewer_with_uia",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("UIA must not touch an unverified window")
        ),
    )
    monkeypatch.setattr(
        win32gui,
        "PostMessage",
        lambda *args: posted.append(args),
    )

    assert client._close_image_viewer(200, 100) is False
    assert posted == []


def test_image_viewer_native_identity_rejects_main_and_other_process(monkeypatch):
    import win32gui
    import win32process

    client = WechatUiaClient({})
    alive = {100, 200, 300}
    process_ids = {100: 42, 200: 42, 300: 99}
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: hwnd in alive)
    monkeypatch.setattr(
        win32gui,
        "GetWindowText",
        lambda hwnd: "图片和视频" if hwnd in {200, 300} else "微信",
    )
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda hwnd: (1, process_ids[hwnd]),
    )

    assert client._is_image_viewer_window(100, 100) is False
    assert client._is_image_viewer_window(300, 100) is False
    assert client._is_image_viewer_window(200, 100) is True


def test_image_viewer_close_prefers_uia_without_global_keyboard(monkeypatch):
    import win32gui

    client = WechatUiaClient({})
    alive = {100: True, 200: True}
    calls = []
    monkeypatch.setattr(client, "_is_image_viewer_window", lambda *_args: True)

    def close_with_uia(hwnd):
        calls.append(("uia", hwnd))
        alive[hwnd] = False
        return True

    monkeypatch.setattr(client, "_try_close_image_viewer_with_uia", close_with_uia)
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: alive.get(hwnd, False))
    monkeypatch.setattr(
        win32gui,
        "PostMessage",
        lambda *args: calls.append(("wm_close", *args)),
    )
    monkeypatch.setattr(
        client,
        "_press_escape_key",
        lambda: (_ for _ in ()).throw(
            AssertionError("image viewer must never use global Escape")
        ),
    )

    assert client._close_image_viewer(200, 100) is True
    assert calls == [("uia", 200)]


def test_image_viewer_close_never_falls_back_to_native_window_close(monkeypatch):
    import win32gui

    client = WechatUiaClient({})
    alive = {100: True, 200: True}
    posted = []
    monkeypatch.setattr(client, "_is_image_viewer_window", lambda *_args: True)
    monkeypatch.setattr(client, "_try_close_image_viewer_with_uia", lambda *_args: False)
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: alive.get(hwnd, False))

    monkeypatch.setattr(
        win32gui,
        "PostMessage",
        lambda *args: posted.append(args),
    )

    assert client._close_image_viewer(200, 100) is False
    assert alive == {100: True, 200: True}
    assert posted == []


def test_opened_image_viewer_requires_new_window_and_exact_uia_identity(monkeypatch):
    import win32gui

    client = WechatUiaClient({})
    visible = {100, 200, 300}
    in_enum_callback = False

    def enum_windows(callback, param):
        nonlocal in_enum_callback
        for hwnd in sorted(visible):
            in_enum_callback = True
            try:
                callback(hwnd, param)
            finally:
                in_enum_callback = False

    monkeypatch.setattr(win32gui, "EnumWindows", enum_windows)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: hwnd in visible)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 200)
    monkeypatch.setattr(
        client,
        "_is_image_viewer_window",
        lambda hwnd, main_hwnd: hwnd in {200, 300} and main_hwnd == 100,
    )
    monkeypatch.setattr(
        client,
        "_has_image_viewer_close_control",
        lambda hwnd: (
            (_ for _ in ()).throw(
                AssertionError("viewer UIA must run after EnumWindows returns")
            )
            if in_enum_callback
            else hwnd == 200
        ),
    )

    assert client._find_opened_image_viewer(100, {100, 300}) == 200


def test_opened_image_viewer_does_not_accept_same_process_popup(monkeypatch):
    import win32gui

    client = WechatUiaClient({})

    def enum_windows(callback, param):
        callback(100, param)
        callback(200, param)

    monkeypatch.setattr(win32gui, "EnumWindows", enum_windows)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda _hwnd: True)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 200)
    monkeypatch.setattr(
        client,
        "_is_image_viewer_window",
        lambda _hwnd, _main_hwnd: False,
    )
    monkeypatch.setattr(
        client,
        "_has_image_viewer_close_control",
        lambda _hwnd: (_ for _ in ()).throw(
            AssertionError("non-viewer popup must not be inspected or closed")
        ),
    )

    assert client._find_opened_image_viewer(100, {100}) == 0
