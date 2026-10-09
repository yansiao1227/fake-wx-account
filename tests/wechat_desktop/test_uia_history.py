"""微信桌面 uia_history 回归测试。"""
import time
from contextlib import contextmanager
from pathlib import Path
from channel.wechat_desktop.models import OwnerInfo, UiaChatMessage, WechatHistoryMessage, WechatHistoryReadResult
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from .helpers import FakeClient, GeometryControl, _history_row


def test_history_row_parser_extracts_content_and_timestamp():
    row_control = _history_row("你好 2026年8月20日 14:13")

    message = WechatUiaClient._parse_history_row(row_control)

    assert message.content == "你好"
    assert message.time_text == "2026年8月20日 14:13"
    assert message.timestamp.startswith("2026-08-20T14:13:00")
    assert message.direction == "unknown"
    assert message.sender_name == "unknown"
    assert message.degraded is False


def test_history_row_parser_marks_missing_time_as_degraded():
    message = WechatUiaClient._parse_history_row(_history_row("只有正文"))

    assert message.content == "只有正文"
    assert message.timestamp is None
    assert message.degraded is True


def test_history_row_parser_ignores_non_message_controls():
    control = GeometryControl((0, 0, 100, 30), "日期", "mmui::XButton")
    control.ControlTypeName = "TabItemControl"

    assert WechatUiaClient._parse_history_row(control) is None


def test_history_rows_do_not_use_recycled_runtime_ids_for_identity():
    first = _history_row(
        "较新的消息 2026年8月20日 14:13", runtime_id=(42, 7)
    )
    older = _history_row(
        "更早的消息 2026年8月20日 13:50", runtime_id=(42, 7)
    )
    history_list = GeometryControl(
        (0, 0, 700, 500), children=[first, older]
    )

    rows = WechatUiaClient._read_history_rows(history_list)

    assert len({identity for identity, _message in rows}) == 2


def test_history_scroll_uses_verified_list_wheel_down_when_no_scroll_pattern():
    calls = []

    class HistoryList:
        def GetScrollPattern(self):
            return None

        def WheelDown(self, **kwargs):
            calls.append(kwargs)

    assert WechatUiaClient._scroll_history_table_older(HistoryList()) is True
    assert calls == [
        {"wheelTimes": 8, "interval": 0.05, "waitTime": 0.1}
    ]


def test_explicit_attachment_history_read_uses_gateway_lease(monkeypatch):
    client = FakeClient()
    expected = WechatHistoryReadResult(
        conversation_title="颜料盒",
        conversation_type="private",
        messages=[
            WechatHistoryMessage(
                sender_name="unknown",
                direction="unknown",
                content_type="text",
                content="历史消息",
            )
        ],
        requested_limit=5,
        returned_count=1,
    )
    client.read_current_chat_history = lambda limit: expected
    gateway = WechatUiaGateway({}, client=client)
    states = []

    def read(limit):
        states.append(gateway.reply_pending.is_set())
        return expected

    client.read_current_chat_history = read

    assert gateway.scan(client.read_current_chat_history, 5) == expected
    assert states == [False]


def test_owner_discovery_retries_after_transient_empty_result(monkeypatch):
    client = WechatUiaClient({"uia_owner_failure_cache_seconds": 0})
    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    root = GeometryControl((0, 0, 1000, 800))
    popup_results = iter([("", ""), ("颜料盒bot", "wxid_bot")])
    popup_calls = []

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter(()))

    def read_popup(_root):
        popup_calls.append(True)
        return next(popup_results)

    monkeypatch.setattr(client, "_read_owner_profile_popup", read_popup)

    assert client.get_owner_info() == OwnerInfo("", source="unknown")
    assert client.get_owner_info() == OwnerInfo(
        "颜料盒bot", wx_id="wxid_bot", source="uia"
    )
    assert client.get_owner_info().nick_name == "颜料盒bot"
    assert len(popup_calls) == 2


def test_owner_discovery_failure_is_cached_until_retry_window(monkeypatch):
    client = WechatUiaClient({"uia_owner_failure_cache_seconds": 60})
    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    root = GeometryControl((0, 0, 1000, 800))
    now = [100.0]
    popup_calls = []

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter(()))
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        client,
        "_read_owner_profile_popup",
        lambda _root: popup_calls.append(True) or ("", ""),
    )

    assert client.get_owner_info() == OwnerInfo("", source="unknown")
    assert client.get_owner_info() == OwnerInfo("", source="unknown")
    assert len(popup_calls) == 1

    now[0] = 161.0
    assert client.get_owner_info() == OwnerInfo("", source="unknown")
    assert len(popup_calls) == 2


def test_configured_owner_is_still_verified_through_uia(monkeypatch):
    client = WechatUiaClient({"self_display_name": "颜料盒bot"})
    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    root = GeometryControl((0, 0, 1000, 800))
    popup_calls = []

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter(()))
    monkeypatch.setattr(
        client,
        "_read_owner_profile_popup",
        lambda _root: popup_calls.append(True) or ("颜料盒bot", "wxid_bot"),
    )

    assert client.get_owner_info() == OwnerInfo(
        "颜料盒bot", wx_id="wxid_bot", source="uia"
    )
    assert popup_calls == [True]


def test_owner_profile_popup_uses_same_process_hwnd_without_desktop_uia(monkeypatch):
    import uiautomation as auto
    import win32api
    import win32gui
    import win32process

    display = GeometryControl(
        (500, 200, 650, 230),
        name="颜料盒bot",
        class_name="mmui::XTextView",
        automation_id="right_v_view.nickname_button_view.display_name_text",
    )
    wx_id = GeometryControl(
        (500, 240, 650, 270),
        name="wxid_bot",
        class_name="mmui::ProfileTextView",
    )
    popup = GeometryControl(
        (400, 150, 700, 450),
        class_name="mmui::ProfileUniquePop",
        children=[display, wx_id],
    )
    root = GeometryControl((100, 100, 1000, 800))
    client = WechatUiaClient({"uia_selection_settle_ms": 0})
    clicked = []

    monkeypatch.setattr(client, "get_owner_window_handle", lambda: 100)
    monkeypatch.setattr(client, "_visible_top_level_window_handles", lambda: {100})
    monkeypatch.setattr(auto, "Click", lambda x, y: clicked.append((x, y)))
    monkeypatch.setattr(
        auto,
        "GetRootControl",
        lambda: (_ for _ in ()).throw(
            AssertionError("desktop UIA root must not be enumerated")
        ),
    )
    monkeypatch.setattr(auto, "ControlFromHandle", lambda hwnd: popup if hwnd == 200 else root)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (10, 20))
    monkeypatch.setattr(win32api, "SetCursorPos", lambda _point: None)
    monkeypatch.setattr(win32api, "keybd_event", lambda *_args: None)
    monkeypatch.setattr(win32gui, "IsWindowVisible", lambda hwnd: hwnd in {100, 200, 300})

    def enum_windows(callback, param):
        for hwnd in (100, 200, 300):
            callback(hwnd, param)

    monkeypatch.setattr(win32gui, "EnumWindows", enum_windows)
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda hwnd: (1, 42 if hwnd in {100, 200} else 99),
    )

    assert client._read_owner_profile_popup(root) == ("颜料盒bot", "wxid_bot")
    assert clicked == [(138, 168)]


def test_image_bubble_is_captured_to_tmp(monkeypatch, tmp_path):
    from PIL import Image, ImageGrab

    captures = []

    def grab(*, bbox, all_screens):
        captures.append((bbox, all_screens))
        return Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), "red")

    monkeypatch.setattr(ImageGrab, "grab", grab)
    message = UiaChatMessage(
        "Alice",
        "[图片]",
        message_type="image",
        runtime_id="image-1",
        bounds=(-50, 100, 150, 260),
    )

    image_path = WechatUiaClient({}).fetch_message_image(message, tmp_path)

    assert captures == [((-50, 100, 150, 260), True)]
    assert Path(image_path).parent == tmp_path
    assert Path(image_path).suffix == ".png"
    assert Image.open(image_path).size == (200, 160)


def test_image_viewer_close_control_is_found_by_exact_uia_identity():
    close = GeometryControl((900, 0, 950, 50), "关闭", "mmui::XButton")
    close.ControlTypeName = "ButtonControl"
    wrong_class = GeometryControl((850, 0, 900, 50), "关闭", "QWidget")
    wrong_class.ControlTypeName = "ButtonControl"
    root = GeometryControl(
        (0, 0, 1000, 800),
        "Weixin",
        "mmui::XView",
        children=[wrong_class, close],
    )
    root.ControlTypeName = "WindowControl"

    assert WechatUiaClient._find_image_viewer_close_control(root) is close
