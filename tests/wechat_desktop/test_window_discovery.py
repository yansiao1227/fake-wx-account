"""只模拟窗口与进程 API，验证微信主窗口识别不访问真实桌面。"""

import sys
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.uia.client import WechatUiaClient, parse_session_accessible_name


class FakeProcess:
    def __init__(self, window):
        self.window = window
        self.close_calls = 0

    def Close(self):
        self.close_calls += 1
        if self.window.get("close_error"):
            raise OSError("模拟关闭失败")


def install_window_api(monkeypatch, windows):
    """替换所有枚举路径依赖，避免构造客户端或调用 UIA 属性。"""
    by_handle = {window["hwnd"]: window for window in windows}
    by_pid = {window["pid"]: window for window in windows}
    processes = []
    open_calls = []

    def enum_windows(callback, parameter):
        for window in windows:
            callback(window["hwnd"], parameter)

    def open_process(access, inherit, process_id):
        open_calls.append((access, inherit, process_id))
        window = by_pid[process_id]
        if window.get("open_error"):
            raise OSError("模拟进程查询权限不足")
        process = FakeProcess(window)
        processes.append(process)
        return process

    def executable_name(process, module):
        assert module == 0
        if process.window.get("query_error"):
            raise OSError("模拟进程模块查询失败")
        return process.window.get("exe", "")

    monkeypatch.setattr(WechatUiaClient, "_require_windows", staticmethod(lambda: None))
    monkeypatch.setitem(
        sys.modules,
        "win32api",
        SimpleNamespace(OpenProcess=open_process),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32con",
        SimpleNamespace(PROCESS_QUERY_INFORMATION=0x0400, PROCESS_VM_READ=0x0010),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(
            EnumWindows=enum_windows,
            IsWindowVisible=lambda hwnd: by_handle[hwnd].get("visible", True),
            GetClassName=lambda hwnd: by_handle[hwnd]["class"],
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32process",
        SimpleNamespace(
            GetWindowThreadProcessId=lambda hwnd: (1, by_handle[hwnd]["pid"]),
            GetModuleFileNameEx=executable_name,
        ),
    )
    return processes, open_calls


@pytest.mark.parametrize(
    ("native_class", "extra", "selected"),
    [
        ("Qt51514QWindowIcon", {"exe": "Weixin.exe"}, True),
        ("Qt51514QWindowIcon", {"exe": "WEIXIN.EXE"}, True),
        ("Qt51514QWindowIcon", {"open_error": True}, False),
        ("Qt51514QWindowIcon", {"query_error": True}, False),
        ("Qt51514QWindowIcon", {"exe": "Games.exe"}, False),
        ("mmui::MainWindow", {"open_error": True}, True),
        ("mmui::MainWindow", {"query_error": True}, True),
        ("mmui::MainWindow", {"exe": "Games.exe"}, False),
        ("Qt51514QWindowIcon", {"exe": "Weixin.exe", "visible": False}, False),
        ("OtherWindowClass", {"exe": "Weixin.exe"}, False),
    ],
)
def test_enumeration_requires_wechat_identity(monkeypatch, native_class, extra, selected):
    processes, open_calls = install_window_api(
        monkeypatch,
        [{"hwnd": 101, "pid": 201, "class": native_class, **extra}],
    )

    assert WechatUiaClient._enumerate_main_windows() == ([(101, 201)] if selected else [])
    assert all(process.close_calls == 1 for process in processes)
    if not extra.get("visible", True) or native_class == "OtherWindowClass":
        assert open_calls == []
    else:
        assert open_calls == [(0x0410, False, 201)]


def test_non_wechat_unqueryable_qt_window_does_not_block_wechat(monkeypatch):
    install_window_api(
        monkeypatch,
        [
            {"hwnd": 101, "pid": 201, "class": "mmui::MainWindow", "exe": "Weixin.exe"},
            {"hwnd": 102, "pid": 202, "class": "Qt51514QWindowIcon", "open_error": True},
        ],
    )
    client = WechatUiaClient.__new__(WechatUiaClient)

    assert client._window() == (101, 201)


def test_two_real_wechat_windows_are_still_rejected(monkeypatch):
    install_window_api(
        monkeypatch,
        [
            {"hwnd": 101, "pid": 201, "class": "mmui::MainWindow", "exe": "Weixin.exe"},
            {"hwnd": 102, "pid": 202, "class": "Qt51514QWindowIcon", "exe": "Weixin.exe"},
        ],
    )
    client = WechatUiaClient.__new__(WechatUiaClient)

    with pytest.raises(RuntimeError, match="Exactly one.*found 2"):
        client._window()


def test_close_failure_does_not_erase_confirmed_executable_identity(monkeypatch):
    processes, _ = install_window_api(
        monkeypatch,
        [
            {
                "hwnd": 101,
                "pid": 201,
                "class": "Qt51514QWindowIcon",
                "exe": "Weixin.exe",
                "close_error": True,
            }
        ],
    )

    assert WechatUiaClient._enumerate_main_windows() == [(101, 201)]
    assert processes[0].close_calls == 1


def test_session_parser_redacts_preview_and_reads_markers():
    parsed = parse_session_accessible_name(
        "项目群",
        "项目群\n[3条] 项目成员: secret [有人@我]\n23:31\n已置顶\n消息免打扰",
    )
    assert parsed.conversation_title == "项目群"
    assert parsed.not_read_number == 3
    assert parsed.mentions_self is True
    assert parsed.is_top is True
    assert parsed.is_do_not_disturb is True
    assert "secret" not in parsed.row_signature
    assert parsed.preview_sender == "项目成员"
    assert parsed.preview_has_sender_prefix is True


def test_session_parser_mention_marker_can_appear_inside_preview():
    parsed = parse_session_accessible_name(
        "测试群",
        "测试群\n成员: 这是一条模拟消息 [有人@我] 后面仍有内容\n12:45",
        "session_item_测试群",
    )

    assert parsed.mentions_self is True


def test_session_preview_without_sender_prefix_is_not_marked_as_incoming_sender():
    parsed = parse_session_accessible_name(
        "项目群", "项目群\n[1条] 我刚发送的消息\n23:59"
    )
    assert parsed.preview_sender == ""
    assert parsed.preview_has_sender_prefix is False


def test_white_tree_recovery_stops_after_tree_returns(monkeypatch):
    client = WechatUiaClient({"uia_recovery_attempts": 3, "uia_recovery_settle_ms": 0})
    probes = iter([0, 0, 12])
    clicks = []
    monkeypatch.setattr(client, "probe_tree", lambda: next(probes))
    monkeypatch.setattr(client, "_click_taskbar_button", lambda: clicks.append(1) or True)
    assert client._recover_empty_tree(123) is True
    assert len(clicks) == 2
