"""微信菜单查找拒绝遍历其他应用的 UIA 根；大文档不阻塞微信读取。"""

from contextlib import contextmanager

from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import GeometryControl


def _search_client(monkeypatch, *, duplicate=False):
    import uiautomation as auto
    import win32gui
    import win32process

    client = WechatUiaClient({})
    main = GeometryControl((0, 0, 400, 600), "微信", "mmui::MainWindow")
    menu = GeometryControl((20, 20, 100, 100), "定位到原文位置", "mmui::XMenuView")
    second = GeometryControl((20, 20, 100, 100), "定位到原文位置", "mmui::XMenuView")
    main.ProcessId = menu.ProcessId = second.ProcessId = 42
    menu.GetRuntimeId = lambda: (42, 2)
    second.GetRuntimeId = lambda: (42, 4)
    handles = [100, 200, 300] + ([400] if duplicate else [])
    roots = {100: main, 200: menu, 400: second}
    calls = []

    @contextmanager
    def initializer():
        yield

    def root(hwnd):
        calls.append(hwnd)
        assert hwnd != 300, "WPS 等非微信应用不得进入 UIA"
        return roots[hwnd]

    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    monkeypatch.setattr(win32gui, "EnumWindows", lambda callback, extra: [callback(hwnd, extra) for hwnd in handles])
    monkeypatch.setattr(win32gui, "IsWindow", lambda hwnd: hwnd in handles)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: hwnd in handles)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 200)
    monkeypatch.setattr(win32process, "GetWindowThreadProcessId", lambda hwnd: (1, 88 if hwnd == 300 else 42))
    monkeypatch.setattr(auto, "UIAutomationInitializerInThread", initializer)
    monkeypatch.setattr(auto, "ControlFromHandle", root)
    monkeypatch.setattr(auto, "GetRootControl", lambda: (_ for _ in ()).throw(
        AssertionError("禁止读全桌面 UIA 根")))
    return client, main, menu, calls


def test_menu_search_never_enters_wps_or_desktop_root(monkeypatch):
    client, main, menu, calls = _search_client(monkeypatch)
    assert client._find_desktop_control("定位到原文位置", "mmui::XMenuView") is menu
    assert calls == [200, 100]


def test_multiple_same_named_wechat_menus_are_rejected(monkeypatch):
    client, main, menu, calls = _search_client(monkeypatch, duplicate=True)
    assert client._find_desktop_control("定位到原文位置", "mmui::XMenuView") is None
    assert 300 not in calls


def test_main_return_search_never_considers_an_auxiliary_window(monkeypatch):
    client, main, menu, calls = _search_client(monkeypatch)
    back = GeometryControl((10, 500, 200, 550), "回到引用位置", "mmui::UnreadBarView")
    back.ProcessId = 42
    main._children.append(back)

    @contextmanager
    def main_root():
        yield main

    monkeypatch.setattr(client, "_uia_root", main_root)
    assert client._find_main_window_control("回到引用位置", "mmui::UnreadBarView") is back
    assert calls == []


def test_main_return_search_rejects_foreign_pid_in_main_tree(monkeypatch):
    client, main, menu, calls = _search_client(monkeypatch)
    back = GeometryControl((10, 500, 200, 550), "回到引用位置", "mmui::UnreadBarView")
    back.ProcessId = 88
    main._children.append(back)

    @contextmanager
    def main_root():
        yield main

    monkeypatch.setattr(client, "_uia_root", main_root)
    assert client._find_main_window_control("回到引用位置", "mmui::UnreadBarView") is None


def test_cross_process_uia_child_is_never_expanded(monkeypatch):
    client, main, menu, calls = _search_client(monkeypatch)
    foreign = GeometryControl((0, 0, 400, 600), "模拟外部文档", "ExternalDocument")
    foreign.ProcessId = 88
    foreign_calls = []

    def children():
        foreign_calls.append(True)
        raise AssertionError("不得展开外部文档 UIA 子树")

    foreign.GetChildren = children
    main._children.append(foreign)
    assert client._find_desktop_control("定位到原文位置", "mmui::XMenuView") is menu
    assert foreign_calls == []
