import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from bridge.context import ContextType
from channel.wechat_desktop.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.models import (
    ConversationInfo,
    HeaderInfo,
    OwnerInfo,
    UiaChatMessage,
    WechatDesktopEvent,
)
from channel.wechat_desktop.shell_hook import WindowsShellHook
from channel.wechat_desktop.uia_client import WechatUiaClient, parse_session_accessible_name
from channel.wechat_desktop.uia_driver import WechatUiaDriver
from channel.wechat_desktop.wechat_desktop_message import WechatDesktopMessage
from channel.wechat_desktop.wechat_desktop_channel import _is_network_reply_error


def test_send_button_uses_real_click_and_restores_cursor(monkeypatch):
    import win32api

    calls = []
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (123, 456))
    monkeypatch.setattr(
        win32api,
        "SetCursorPos",
        lambda position: calls.append(("restore", position)),
    )

    class Button:
        def Click(self, **kwargs):
            calls.append(("click", kwargs))

        def GetInvokePattern(self):
            raise AssertionError("WeChat 4.1.9.30 InvokePattern must not be used")

    WechatUiaClient({})._click_send_button(Button())

    assert calls == [
        ("click", {"simulateMove": False, "waitTime": 0.1}),
        ("restore", (123, 456)),
    ]


def test_startup_foreground_check_does_not_use_uia_tree(monkeypatch):
    import win32gui
    import win32process

    client = WechatUiaClient({})
    calls = []
    foreground = {"hwnd": 200}

    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    monkeypatch.setattr(
        client,
        "probe_tree",
        lambda: (_ for _ in ()).throw(AssertionError("UIA tree must not be read")),
    )
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: foreground["hwnd"])
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda hwnd: (1, 42 if hwnd == 100 else 99),
    )
    monkeypatch.setattr(
        win32gui,
        "ShowWindow",
        lambda hwnd, command: calls.append(("restore", hwnd, command)),
    )

    def set_foreground(hwnd):
        calls.append(("foreground", hwnd))
        foreground["hwnd"] = hwnd

    monkeypatch.setattr(win32gui, "SetForegroundWindow", set_foreground)

    assert client.ensure_foreground_window() is True
    assert [item[0] for item in calls] == ["restore", "foreground"]


def test_startup_foreground_check_accepts_any_wechat_process_window(monkeypatch):
    import win32gui
    import win32process

    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 101)
    monkeypatch.setattr(
        win32process,
        "GetWindowThreadProcessId",
        lambda _hwnd: (1, 42),
    )
    monkeypatch.setattr(
        win32gui,
        "ShowWindow",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("foreground WeChat must not be restored")
        ),
    )

    assert client.ensure_foreground_window() is False


def test_input_text_normalization_handles_uia_line_endings_and_spaces():
    normalize = WechatUiaClient._normalize_input_text

    assert normalize("第一行\r\n第二行\u00a0文本") == normalize(
        "第一行\n第二行 文本"
    )
    assert normalize("e\u0301") == normalize("é")


def test_paste_sends_nonempty_text_even_when_uia_value_differs(monkeypatch):
    import win32api

    state = {"value": "", "paste_count": 0, "click_count": 0}

    class Input(GeometryControl):
        def SetFocus(self):
            pass

        def GetValuePattern(self):
            return SimpleNamespace(Value=state["value"])

    class Button(GeometryControl):
        def Click(self, **_kwargs):
            state["click_count"] += 1
            state["value"] = ""

    input_control = Input((0, 0, 100, 50), automation_id="chat_input_field")
    send_button = Button(
        (100, 0, 150, 50), name="发送", class_name="mmui::XOutlineButton"
    )
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield object()

    def keybd_event(key, _scan, flags, _extra):
        if key == ord("V") and flags == 0:
            state["paste_count"] += 1
            state["value"] = "UIA 返回的可见片段"

    monkeypatch.setattr(win32api, "keybd_event", keybd_event)
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter((input_control, send_button)))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)

    client._paste_and_send("完整的原始文本")

    assert state == {"value": "", "paste_count": 1, "click_count": 1}


def test_paste_retries_once_when_input_remains_empty(monkeypatch):
    import win32api

    state = {"value": "", "paste_count": 0, "click_count": 0}

    class Input(GeometryControl):
        def SetFocus(self):
            pass

        def GetValuePattern(self):
            return SimpleNamespace(Value=state["value"])

    class Button(GeometryControl):
        def Click(self, **_kwargs):
            state["click_count"] += 1
            state["value"] = ""

    input_control = Input((0, 0, 100, 50), automation_id="chat_input_field")
    send_button = Button(
        (100, 0, 150, 50), name="发送", class_name="mmui::XOutlineButton"
    )
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield object()

    def keybd_event(key, _scan, flags, _extra):
        if key == ord("V") and flags == 0:
            state["paste_count"] += 1
            if state["paste_count"] == 2:
                state["value"] = "第二次粘贴成功"

    monkeypatch.setattr(win32api, "keybd_event", keybd_event)
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter((input_control, send_button)))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(
        client,
        "_wait_for_input_value",
        lambda control, predicate, _timeout: predicate(client._input_value(control)),
    )

    client._paste_and_send("第二次粘贴成功")

    assert state == {"value": "", "paste_count": 2, "click_count": 1}


def test_long_message_split_prefers_readable_boundaries():
    text = ("A" * 280) + "。" + ("B" * 280) + "\n\n" + ("C" * 280)

    chunks = WechatUiaClient._split_message_text(text, 500)

    assert len(chunks) == 2
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_paste_retry_reacquires_window_and_input(monkeypatch):
    import win32api

    state = {"value": "", "paste_count": 0, "focus_count": 0, "root_count": 0}

    class Input(GeometryControl):
        HasKeyboardFocus = True

        def SetFocus(self):
            pass

        def GetValuePattern(self):
            return SimpleNamespace(Value=state["value"])

    class Button(GeometryControl):
        def Click(self, **_kwargs):
            state["value"] = ""

    input_control = Input((0, 0, 100, 50), automation_id="chat_input_field")
    send_button = Button(
        (100, 0, 150, 50), name="发送", class_name="mmui::XOutlineButton"
    )
    client = WechatUiaClient({"uia_paste_attempts": 3})

    @contextmanager
    def fake_root():
        state["root_count"] += 1
        yield object()

    def keybd_event(key, _scan, flags, _extra):
        if key == ord("V") and flags == 0:
            state["paste_count"] += 1
            if state["paste_count"] == 2:
                state["value"] = "retry succeeded"

    def focus_window():
        state["focus_count"] += 1

    monkeypatch.setattr(win32api, "keybd_event", keybd_event)
    monkeypatch.setattr(client, "focus_window", focus_window)
    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_walk", lambda _root: iter((input_control, send_button)))
    monkeypatch.setattr(client, "_paced_wait", lambda *_args: None)
    monkeypatch.setattr(
        client,
        "_wait_for_input_value",
        lambda control, predicate, _timeout: predicate(client._input_value(control)),
    )

    client._paste_and_send("retry succeeded")

    assert state["paste_count"] == 2
    assert state["focus_count"] == 2
    assert state["root_count"] == 2


def test_send_message_splits_and_verifies_each_chunk(monkeypatch):
    client = WechatUiaClient({"uia_text_chunk_chars": 100})
    pasted = []
    verified = []

    @contextmanager
    def fake_clipboard(unicode_text=None, files=None):
        assert files is None
        pasted.append(unicode_text)
        yield

    monkeypatch.setattr(client, "_wait_for_send_slot", lambda _who: None)
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *_args: True)
    monkeypatch.setattr(client, "get_chat_history", lambda **_kwargs: [])
    monkeypatch.setattr(client, "_clipboard", fake_clipboard)
    monkeypatch.setattr(client, "_paste_and_send", lambda expected_text: None)

    def verify(_who, _before, text="", expected_type=""):
        assert not expected_type
        verified.append(text)
        return {"success": True, "verified": True}

    monkeypatch.setattr(client, "_verify_send", verify)

    result = client.send_message("target", "X" * 240)

    assert [len(chunk) for chunk in pasted] == [100, 100, 40]
    assert verified == pasted
    assert result == {
        "success": True,
        "verified": True,
        "chunks": 3,
        "message": "",
    }


class GeometryControl:
    def __init__(
        self,
        bounds,
        name="",
        class_name="",
        children=None,
        automation_id="",
    ):
        self.BoundingRectangle = SimpleNamespace(
            left=bounds[0], top=bounds[1], right=bounds[2], bottom=bounds[3]
        )
        self.Name = name
        self.ClassName = class_name
        self.AutomationId = automation_id
        self.ControlTypeName = "CustomControl"
        self._children = list(children or [])

    def GetChildren(self):
        return list(self._children)


class ClickableGeometryControl(GeometryControl):
    def __init__(self, *args, on_click=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.click_count = 0
        self.on_click = on_click

    def Click(self):
        self.click_count += 1
        if self.on_click:
            self.on_click()


def test_owner_discovery_retries_after_transient_empty_result(monkeypatch):
    client = WechatUiaClient({})
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


def test_message_direction_uses_bubble_child_not_full_width_row():
    left_bubble = GeometryControl(
        (120, 120, 360, 180), "hello", "mmui::ChatTextBubble"
    )
    left_row = GeometryControl(
        (100, 100, 900, 200), "hello", "mmui::ChatItemView", [left_bubble]
    )
    right_bubble = GeometryControl(
        (640, 220, 880, 280), "reply", "mmui::ChatTextBubble"
    )
    right_row = GeometryControl(
        (100, 200, 900, 300), "reply", "mmui::ChatItemView", [right_bubble]
    )

    assert WechatUiaClient._message_direction(left_row, (100, 50, 900, 700))[0] == "incoming"
    assert WechatUiaClient._message_direction(right_row, (100, 50, 900, 700))[0] == "outgoing"


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


def test_message_direction_fails_closed_for_centered_control():
    centered = GeometryControl(
        (390, 100, 610, 160), "system", "mmui::ChatItemView"
    )
    assert WechatUiaClient._message_direction(centered, (100, 50, 900, 700))[0] == "unknown"


def test_file_message_type_prefers_uia_control_class_over_suffix():
    classify = WechatUiaClient._message_type

    assert classify("mmui::ChatFileItemView", "季度报告.xlsx 12 KB") == "file"
    assert classify("mmui::ChatBubbleItemView", "文件\n季度报告.xlsx\n12 KB\n微信电脑版") == "file"
    assert classify("mmui::ChatTextItemView", "请阅读 report.pdf") == "text"
    assert classify("mmui::ChatTextItemView", "please send the file") == "text"


def test_referenced_image_card_is_classified_from_wechat_uia_shape():
    classify = WechatUiaClient._message_type

    assert classify("mmui::ChatBubbleReferItemView", "图片") == "image"
    assert classify("mmui::ChatBubbleReferItemView", "Image") == "image"
    assert classify("mmui::ChatTextItemView", "图片") == "text"


def test_file_card_metadata_parses_wechat_binary_units():
    filename, size = WechatUiaClient._file_card_metadata(
        "文件\n季度报告.xlsx\n684.2K\n微信电脑版"
    )

    assert filename == "季度报告.xlsx"
    assert size == 700620


def test_cached_file_match_uses_displayed_size_and_accepts_duplicate_suffix(
    tmp_path, monkeypatch
):
    month = tmp_path / "wxid_example" / "msg" / "file" / "2026-07"
    month.mkdir(parents=True)
    older = month / "报告.pdf"
    current = month / "报告(1).pdf"
    older.write_bytes(b"x" * 712215)
    current.write_bytes(b"x" * 700636)
    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_wechat_data_roots", lambda: [tmp_path])

    matched = client._find_cached_message_file("文件\n报告.pdf\n684.2K\n微信电脑版")

    assert matched == str(current)


def test_store_file_in_project_tmp_preserves_bytes_and_deduplicates(tmp_path):
    source = tmp_path / "source" / "报告.txt"
    source.parent.mkdir()
    source.write_bytes(b"wechat attachment")
    target_root = tmp_path / "tmp" / "wechat_files"

    first = WechatUiaClient._store_file_in_tmp(str(source), target_root)
    second = WechatUiaClient._store_file_in_tmp(str(source), target_root)

    assert first == second
    assert Path(first).parent == target_root
    assert Path(first).read_bytes() == b"wechat attachment"
    assert Path(first).name.endswith("_报告.txt")


def test_downloaded_file_event_becomes_file_context(tmp_path):
    attachment = tmp_path / "attachment.txt"
    attachment.write_text("hello", encoding="utf-8")
    event = WechatDesktopEvent(
        "message",
        "session",
        "Alice",
        "alice",
        "Alice",
        "file",
        str(attachment),
    )

    message = WechatDesktopMessage(event)

    assert message.ctype == ContextType.FILE
    assert message.content == str(attachment)


def test_message_sender_uses_geometry_direction_for_self_and_private_peer():
    owner = "当前账号"
    header = HeaderInfo("颜料盒", "private", 1)
    incoming_bubble = GeometryControl(
        (120, 120, 360, 180), "对方消息", "mmui::ChatTextBubble"
    )
    incoming_row = GeometryControl(
        (100, 100, 900, 200),
        "对方消息",
        "mmui::ChatTextItemView",
        [incoming_bubble],
    )
    outgoing_bubble = GeometryControl(
        (640, 220, 880, 280), "自己的消息", "mmui::ChatTextBubble"
    )
    outgoing_row = GeometryControl(
        (100, 200, 900, 300),
        "自己的消息",
        "mmui::ChatTextItemView",
        [outgoing_bubble],
    )
    list_bounds = (100, 50, 900, 700)

    incoming_direction = WechatUiaClient._message_direction(
        incoming_row, list_bounds
    )[0]
    outgoing_direction = WechatUiaClient._message_direction(
        outgoing_row, list_bounds
    )[0]

    assert incoming_direction == "incoming"
    assert WechatUiaClient._message_sender(
        incoming_direction, header, owner, incoming_row, "对方消息"
    ) == "颜料盒"
    assert outgoing_direction == "outgoing"
    assert WechatUiaClient._message_sender(
        outgoing_direction, header, owner, outgoing_row, "自己的消息"
    ) == owner


def test_message_sender_reads_group_member_only_for_incoming_message():
    member = GeometryControl((130, 100, 220, 120), "群成员甲", "Text")
    bubble = GeometryControl((120, 130, 420, 190), "群消息", "ChatTextBubble")
    row = GeometryControl(
        (100, 90, 900, 210), "群消息", "mmui::ChatTextItemView", [member, bubble]
    )

    assert WechatUiaClient._message_sender(
        "incoming", HeaderInfo("测试群", "group", 3), "当前账号", row, "群消息"
    ) == "群成员甲"
    assert WechatUiaClient._message_sender(
        "outgoing", HeaderInfo("测试群", "group", 3), "当前账号", row, "群消息"
    ) == "当前账号"


def test_history_member_filter_selects_unique_self_candidate():
    assert WechatUiaClient._choose_self_member([["我发送的消息"]], []) == 0


def test_history_message_content_removes_full_or_time_only_suffix():
    assert WechatUiaClient._history_message_content(
        "正文 2026年7月28日 12:36"
    ) == "正文"
    assert WechatUiaClient._history_message_content(
        "[smoke] 2026-07-28 12:36:31 +0800 12:36"
    ) == "[smoke] 2026-07-28 12:36:31 +0800"


def test_history_member_filter_uses_known_outgoing_anchor_for_same_name():
    histories = [["另一个同名成员的消息"], ["CowAgent 已发送的回复"]]
    assert WechatUiaClient._choose_self_member(
        histories, ["CowAgent 已发送的回复"]
    ) == 1
    assert WechatUiaClient._choose_self_member(histories, []) is None


def test_history_direction_resolution_fails_closed_for_duplicate_content():
    messages = [
        UiaChatMessage("", "仅对方发送", direction="unknown", runtime_id="1"),
        UiaChatMessage("", "仅自己发送", direction="unknown", runtime_id="2"),
        UiaChatMessage("", "双方相同正文", direction="unknown", runtime_id="3"),
        UiaChatMessage("", "双方相同正文", direction="unknown", runtime_id="4"),
    ]
    resolved = WechatUiaClient._apply_self_history_directions(
        messages, ["仅自己发送", "双方相同正文"]
    )

    assert [item.direction for item in resolved] == [
        "incoming",
        "outgoing",
        "unknown",
        "unknown",
    ]


def test_history_direction_resolution_fails_closed_for_empty_filter_result():
    messages = [
        UiaChatMessage("", "无法确认方向", direction="unknown", runtime_id="1")
    ]

    assert WechatUiaClient._apply_self_history_directions(messages, []) == messages


def _selection_tree(active_title, messages, row):
    header = GeometryControl(
        (400, 100, 700, 130),
        active_title,
        "Text",
        automation_id="current_chat_name_label",
    )
    message_list = GeometryControl(
        (400, 140, 900, 700),
        class_name="List",
        children=messages,
        automation_id="chat_message_list",
    )
    root = GeometryControl((0, 0, 1000, 800), children=[header, message_list, row])
    return root, header, message_list


def test_locate_conversation_does_not_click_populated_active_chat(monkeypatch):
    row = ClickableGeometryControl(
        (0, 100, 300, 160),
        "颜料盒",
        "mmui::ChatSessionCell",
        automation_id="session_item_颜料盒",
    )
    message = GeometryControl(
        (400, 200, 900, 260), "已有消息", "mmui::ChatTextItemView"
    )
    root, _, _ = _selection_tree("颜料盒", [message], row)
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)

    assert client.locate_conversation("颜料盒") is True
    assert row.click_count == 0


def test_locate_conversation_clicks_when_active_chat_is_blank(monkeypatch):
    message = GeometryControl(
        (400, 200, 900, 260), "恢复后的消息", "mmui::ChatTextItemView"
    )
    row = None

    def activate():
        header.Name = "颜料盒"
        message_list._children[:] = [message]

    row = ClickableGeometryControl(
        (0, 100, 300, 160),
        "颜料盒",
        "mmui::ChatSessionCell",
        automation_id="session_item_颜料盒",
        on_click=activate,
    )
    root, header, message_list = _selection_tree("颜料盒", [], row)
    client = WechatUiaClient({})

    @contextmanager
    def fake_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", fake_root)
    monkeypatch.setattr(client, "_paced_wait", lambda *args, **kwargs: None)

    assert client.locate_conversation("颜料盒") is True
    assert row.click_count == 1


class FakeClient:
    def __init__(self):
        self.operation_lock = threading.RLock()
        self.rows = []
        self.histories = {}
        self.headers = {}
        self.history_calls = []
        self.focus_calls = 0
        self.owner_calls = 0
        self.seeded_outgoing = {}
        self.direction_resolution_calls = []
        self.resolved_histories = {}
        self.file_paths = {}
        self.file_fetches = []
        self.image_paths = {}
        self.image_fetches = []

    def allowed_process_ids(self):
        return {42}

    def get_owner_info(self):
        self.owner_calls += 1
        return OwnerInfo("小牛", source="config")

    def get_owner_window_process_id(self):
        return 42

    def probe_tree(self):
        return 10

    def focus_window(self):
        self.focus_calls += 1

    def get_visible_conversations(self):
        return list(self.rows)

    def locate_conversation(self, name, runtime_id="", row_index=-1):
        self.selected = name
        self.selected_key = runtime_id or name
        return self.selected_key in self.headers or name in self.headers

    def get_title(self):
        name = getattr(self, "selected", "")
        if name:
            key = getattr(self, "selected_key", "")
            return self.headers[key] if key in self.headers else self.headers[name]
        return next(iter(self.headers.values()), HeaderInfo("", "unknown", 1))

    def get_chat_history(self, name=None, limit=5, runtime_id="", row_index=-1):
        if name:
            self.selected = name
            self.selected_key = runtime_id or name
        self.history_calls.append(name)
        return list(self.histories.get(runtime_id or name, []))[-limit:]

    def fetch_message_file(self, message):
        self.file_fetches.append(message.content)
        return self.file_paths.get(message.content, "")

    def fetch_message_image(self, message):
        self.image_fetches.append(message.content)
        return self.image_paths.get(message.content, "")

    def seed_outgoing_texts(self, conversation, texts):
        self.seeded_outgoing[conversation] = list(texts)

    def resolve_group_message_directions(self, conversation, messages, owner):
        self.direction_resolution_calls.append((conversation, owner))
        return list(self.resolved_histories.get(conversation, messages))

    def send_message(self, who, text, runtime_id="", row_index=-1):
        return {"success": True, "verified": True}

    def send_file(self, who, files, runtime_id="", row_index=-1):
        return {"success": True, "verified": True}


class FakeHook:
    available = True
    error = ""

    def start(self):
        return True

    def close(self):
        pass


def row(
    name,
    unread=1,
    mention=False,
    signature="new",
    runtime_id="",
    row_index=0,
    preview_prefix=False,
):
    return ConversationInfo(
        name,
        not_read_number=unread,
        mentions_self=mention,
        row_signature=signature,
        automation_id=f"session_item_{name}",
        runtime_id=runtime_id,
        row_index=row_index,
        preview_sender="成员" if preview_prefix else "",
        preview_has_sender_prefix=preview_prefix,
    )


def incoming(content, runtime_id):
    return UiaChatMessage(
        "成员",
        content,
        direction="incoming",
        runtime_id=runtime_id,
    )


def outgoing(content, runtime_id):
    return UiaChatMessage(
        "小牛",
        content,
        direction="outgoing",
        runtime_id=runtime_id,
    )


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


def test_group_selects_only_latest_mention_and_uses_other_bubbles_as_history():
    client = FakeClient()
    client.rows = [row("项目群", unread=3, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [
        incoming("@小牛 第一条", "1"),
        incoming("这是后续普通消息", "2"),
        incoming("@小牛 第二条", "3"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True},
        client=client,
        shell_hook=FakeHook(),
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["@小牛 第二条"]
    assert events[0].is_at is True
    assert client.owner_calls >= 1
    assert [item["content"] for item in events[0].history] == [
        "@小牛 第一条",
        "这是后续普通消息",
    ]


def test_known_group_without_mention_never_reads_history():
    client = FakeClient()
    client.rows = [row("项目群", unread=2, mention=False)]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True, "auto_reply_groups": ["项目群"]},
        client=client,
        shell_hook=FakeHook(),
    )

    _, events = driver.observe_events()

    assert events == []
    assert client.history_calls == []


def test_group_mention_followed_by_normal_message_replies_only_to_mention():
    client = FakeClient()
    client.rows = [row("项目群", unread=2, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [
        incoming("@小牛 请确认", "1"),
        incoming("后续普通消息", "2"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["@小牛 请确认"]
    assert [item["content"] for item in events[0].history] == ["后续普通消息"]


def test_group_marker_without_locatable_mention_fails_closed():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("普通消息", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert events == []


def test_group_selects_first_at_message_without_using_direction():
    client = FakeClient()
    client.rows = [row("项目群", unread=0, mention=True, preview_prefix=False)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [
        incoming("@小牛 请确认", "1"),
        outgoing("已确认", "2"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["@小牛 请确认"]
    assert [item["content"] for item in events[0].history] == ["已确认"]


def test_same_name_conversations_use_runtime_id_instead_of_title():
    client = FakeClient()
    client.rows = [
        row("同名用户", runtime_id="runtime-1", row_index=0, signature="a"),
        row("同名用户", runtime_id="runtime-2", row_index=1, signature="b"),
    ]
    client.headers["runtime-1"] = HeaderInfo("同名用户", "private", 1)
    client.headers["runtime-2"] = HeaderInfo("同名用户", "private", 1)
    client.histories["runtime-1"] = [incoming("first account", "m1")]
    client.histories["runtime-2"] = [incoming("second account", "m2")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["first account", "second account"]
    assert len({event.conversation_id for event in events}) == 2
    assert all(event.conversation_name == "同名用户" for event in events)


def test_private_burst_selects_only_latest_and_keeps_earlier_messages_as_history():
    client = FakeClient()
    client.rows = [row("Alice", unread=3)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        incoming("one", "1"),
        incoming("two", "2"),
        incoming("three", "3"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["three"]
    assert [item["content"] for item in events[0].history] == ["one", "two"]
    assert all(item["sender_name"] == "" for item in events[0].history)


def test_private_file_target_is_fetched_and_event_uses_local_path(tmp_path):
    local_file = tmp_path / "tmp" / "wechat_files" / "report.pdf"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"%PDF-test")
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "report.pdf 8 KB",
            message_type="file",
            direction="incoming",
            runtime_id="file-1",
        )
    ]
    client.file_paths["report.pdf 8 KB"] = str(local_file)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert client.file_fetches == ["report.pdf 8 KB"]
    assert len(events) == 1
    assert events[0].content_type == "file"
    assert events[0].content == str(local_file)


def test_private_image_target_is_captured_and_event_uses_local_path(tmp_path):
    local_image = tmp_path / "tmp" / "wechat_images" / "photo.png"
    local_image.parent.mkdir(parents=True)
    local_image.write_bytes(b"png")
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "[图片]",
            message_type="image",
            direction="incoming",
            runtime_id="image-1",
            bounds=(100, 100, 300, 260),
        )
    ]
    client.image_paths["[图片]"] = str(local_image)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert client.image_fetches == ["[图片]"]
    assert len(events) == 1
    assert events[0].content_type == "image"
    assert events[0].content == str(local_image)
    assert events[0].evidence_path == str(local_image)


def test_attachment_history_keeps_all_previous_and_limits_followups():
    messages = [incoming(f"before-{index}", str(index)) for index in range(6)]
    messages.append(
        UiaChatMessage(
            "Alice",
            "[图片]",
            message_type="image",
            direction="incoming",
            runtime_id="image-target",
            file_path="C:/tmp/wechat_images/photo.png",
        )
    )
    messages.extend(incoming(f"after-{index}", f"a{index}") for index in range(6))
    driver = WechatUiaDriver(
        {"attachment_context_window_messages": 2},
        client=FakeClient(),
        shell_hook=FakeHook(),
    )

    history = driver._history_snapshot(messages, 6, False)

    assert [item["content"] for item in history] == [
        "before-0",
        "before-1",
        "before-2",
        "before-3",
        "before-4",
        "before-5",
        "after-0",
        "after-1",
    ]


def test_attachment_history_can_disable_followup_absorption():
    messages = [
        incoming("before", "before"),
        UiaChatMessage(
            "Alice",
            "report.pdf",
            message_type="file",
            direction="incoming",
            runtime_id="file-target",
        ),
        incoming("after", "after"),
    ]
    driver = WechatUiaDriver(
        {"attachment_context_window_messages": 0},
        client=FakeClient(),
        shell_hook=FakeHook(),
    )

    history = driver._history_snapshot(messages, 1, False)

    assert [item["content"] for item in history] == ["before"]


def test_private_text_target_includes_preceding_downloaded_file_in_history(tmp_path):
    local_file = tmp_path / "tmp" / "wechat_files" / "report.pdf"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"%PDF-test")
    client = FakeClient()
    client.rows = [row("Alice", unread=2)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage(
            "Alice",
            "文件\nreport.pdf\n8 KB\n微信电脑版",
            message_type="file",
            direction="incoming",
            runtime_id="file-1",
        ),
        incoming("读取这个文件", "text-1"),
    ]
    client.file_paths["文件\nreport.pdf\n8 KB\n微信电脑版"] = str(local_file)
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["读取这个文件"]
    assert events[0].history[0]["content_type"] == "file"
    assert events[0].history[0]["content"] == f"[文件: {local_file}]"


def test_private_message_after_outgoing_is_the_only_reply_target():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        incoming("old", "1"),
        outgoing("answered", "2"),
        incoming("new", "3"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["new"]
    assert [item["content"] for item in events[0].history] == ["old", "answered"]
    assert all("direction" not in item for item in events[0].history)


def test_private_latest_outgoing_means_nothing_needs_reply():
    client = FakeClient()
    client.rows = [row("Alice", unread=0)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("question", "1"), outgoing("reply", "2")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert events == []


def test_unknown_geometry_is_never_coerced_to_incoming():
    client = FakeClient()
    client.rows = [row("Alice", unread=0)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage("", "ambiguous", direction="unknown", runtime_id="1")
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert events == []


def test_private_unread_marker_classifies_only_latest_unknown_bubble():
    client = FakeClient()
    client.rows = [row("Alice", unread=2)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage("", "older", direction="unknown", runtime_id="1"),
        UiaChatMessage("", "newest", direction="unknown", runtime_id="2"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["newest"]
    assert [item["content"] for item in events[0].history] == ["older"]
    assert "direction" not in events[0].history[0]


def test_private_context_keeps_every_visible_message_before_reply_target():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        incoming(f"context-{index}", f"runtime-{index}")
        for index in range(12)
    ] + [incoming("reply-target", "runtime-target")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["reply-target"]
    assert [item["content"] for item in events[0].history] == [
        f"context-{index}" for index in range(12)
    ]


def test_private_row_change_without_unread_does_not_open_or_enqueue_chat():
    client = FakeClient()
    client.rows = [row("Alice", unread=0, signature="changed")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("already-read", "runtime-1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert events == []
    assert client.history_calls == []


def test_startup_processes_private_unread_from_session_list_by_default():
    client = FakeClient()
    client.rows = [row("Alice", unread=3, signature="startup")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        incoming("older unread", "1"),
        incoming("latest unread", "2"),
    ]
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["latest unread"]
    assert events[0].session_unread_count == 3
    assert events[0].history[-1]["content"] == "older unread"


def test_startup_does_not_open_private_session_without_unread_marker():
    client = FakeClient()
    client.rows = [row("Alice", unread=0, signature="startup")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("already read", "1")]
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())

    _, events = driver.observe_events()

    assert events == []
    assert client.history_calls == []


def test_startup_unread_processing_can_be_disabled():
    client = FakeClient()
    client.rows = [row("Alice", unread=2, signature="startup")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("unread", "1")]
    driver = WechatUiaDriver(
        {"process_startup_unread_messages": False},
        client=client,
        shell_hook=FakeHook(),
    )

    _, events = driver.observe_events()

    assert events == []
    assert client.history_calls == []


def test_reply_target_key_does_not_use_recyclable_runtime_id():
    first = incoming("same message", "runtime-a")
    second = incoming("same message", "runtime-b")

    assert WechatUiaDriver._target_key(first, 3) == WechatUiaDriver._target_key(
        second, 3
    )


def test_reused_runtime_id_produces_new_private_message_event():
    client = FakeClient()
    client.rows = [row("Alice", unread=1, signature="first")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("first", "recycled-runtime")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, first_events = driver.observe_events()
    client.rows = [row("Alice", unread=1, signature="second")]
    client.histories["Alice"] = [incoming("second", "recycled-runtime")]
    _, second_events = driver.observe_events()

    assert [event.content for event in first_events] == ["first"]
    assert [event.content for event in second_events] == ["second"]
    assert first_events[0].event_id != second_events[0].event_id


def test_reply_queue_is_global_fifo_and_rejects_duplicates():
    reply_queue = WechatReplyQueue()
    second = WechatDesktopEvent(
        "message", "b", "Bob", "b", "Bob", "text", "second", observed_at=2
    )
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first", observed_at=1
    )
    assert reply_queue.enqueue_many([second, first, first]) == 2
    first_item = reply_queue.get()
    assert first_item.event.content == "first"
    reply_queue.finish(first_item, "completed")
    second_item = reply_queue.get()
    assert second_item.event.content == "second"
    reply_queue.finish(second_item, "completed")
    assert reply_queue.status()["queue_completed"] == 2
    assert "queue_approval" not in reply_queue.status()


def test_reply_queue_replaces_pending_target_from_same_conversation():
    reply_queue = WechatReplyQueue()
    old = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "old"
    )
    new = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "new"
    )

    assert reply_queue.enqueue(old).action == "added"
    assert reply_queue.enqueue(new).action == "replaced_pending"
    item = reply_queue.get()

    assert item.event.content == "new"
    assert item.superseded_event_ids == [old.event_id]


def test_reply_queue_collects_private_followup_outside_global_fifo():
    reply_queue = WechatReplyQueue()
    old = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "old"
    )
    new = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "new"
    )
    reply_queue.enqueue(old)
    active = reply_queue.get()

    result = reply_queue.enqueue(new)

    assert result.action == "collected_active_private"
    assert result.cancel_event_id == old.event_id
    assert active.expired is True
    assert active.terminal == "superseded"
    reply_queue.finish(active, "superseded")
    assert reply_queue.get().event.content == "new"
    assert reply_queue.status()["queue_superseded"] == 1


def test_reply_queue_merges_multiple_private_followups():
    reply_queue = WechatReplyQueue()
    first = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "first"
    )
    second = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "second"
    )
    third = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "third"
    )
    reply_queue.enqueue(first)
    active = reply_queue.get()

    assert reply_queue.enqueue(second).action == "collected_active_private"
    assert reply_queue.enqueue(third).action == "merged_active_private"
    reply_queue.finish(active, "superseded")
    merged = reply_queue.get()

    assert merged.event.content == "third"
    assert [item["content"] for item in merged.event.history] == [
        "first",
        "second",
    ]
    assert reply_queue.status()["queue_depth"] == 0


def test_reply_queue_collects_group_mentions_in_local_fifo_without_canceling_active():
    reply_queue = WechatReplyQueue()
    first = WechatDesktopEvent(
        "message", "g", "项目群", "a", "成员A", "text", "@小牛 first", is_group=True
    )
    second = WechatDesktopEvent(
        "message", "g", "项目群", "b", "成员B", "text", "@小牛 second", is_group=True
    )
    third = WechatDesktopEvent(
        "message", "g", "项目群", "c", "成员C", "text", "@小牛 third", is_group=True
    )
    reply_queue.enqueue(first)
    active = reply_queue.get()

    assert reply_queue.enqueue(second).action == "collected_active_group"
    assert reply_queue.enqueue(third).action == "collected_active_group"
    assert active.expired is False
    assert active.done.is_set() is False
    reply_queue.finish(active, "completed")

    next_item = reply_queue.get()
    assert next_item.event.content == "@小牛 second"
    reply_queue.finish(next_item, "completed")
    assert reply_queue.get().event.content == "@小牛 third"


def test_reply_queue_yields_to_global_queue_after_conversation_burst_limit():
    reply_queue = WechatReplyQueue(conversation_burst_limit=2)
    first = WechatDesktopEvent(
        "message", "g", "项目群", "a", "成员A", "text", "@小牛 1", is_group=True
    )
    second = WechatDesktopEvent(
        "message", "g", "项目群", "b", "成员B", "text", "@小牛 2", is_group=True
    )
    third = WechatDesktopEvent(
        "message", "g", "项目群", "c", "成员C", "text", "@小牛 3", is_group=True
    )
    other = WechatDesktopEvent(
        "message", "p", "Bob", "p", "Bob", "text", "hello"
    )
    reply_queue.enqueue(first)
    active = reply_queue.get()
    reply_queue.enqueue(other)
    reply_queue.enqueue(second)
    reply_queue.finish(active, "completed")
    active = reply_queue.get()
    assert active.event is second
    reply_queue.enqueue(third)
    reply_queue.finish(active, "completed")

    yielded = reply_queue.get()

    assert yielded.event is other
    reply_queue.finish(yielded, "completed")
    assert reply_queue.get().event is third


def test_reply_queue_can_clear_global_and_active_conversation_pending_work():
    reply_queue = WechatReplyQueue()
    active_event = WechatDesktopEvent(
        "message", "g", "项目群", "a", "成员A", "text", "@小牛 1", is_group=True
    )
    local_event = WechatDesktopEvent(
        "message", "g", "项目群", "b", "成员B", "text", "@小牛 2", is_group=True
    )
    global_event = WechatDesktopEvent(
        "message", "p", "Bob", "p", "Bob", "text", "hello"
    )
    reply_queue.enqueue(active_event)
    active = reply_queue.get()
    reply_queue.enqueue(local_event)
    reply_queue.enqueue(global_event)

    discarded = reply_queue.clear_pending()

    assert set(discarded) == {local_event.event_id, global_event.event_id}
    assert reply_queue.status()["queue_depth"] == 0
    assert reply_queue.is_active(active.token) is True
    reply_queue.finish(active, "failed")
    assert reply_queue.get(timeout=0.01) is None


def test_network_reply_error_detection():
    assert _is_network_reply_error(
        "Agent error: Connection error: SSL: UNEXPECTED_EOF_WHILE_READING"
    )
    assert _is_network_reply_error("request timed out")
    assert not _is_network_reply_error("invalid tool arguments")


def test_driver_revalidation_rejects_target_replaced_during_generation():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    client.histories["Alice"] = [incoming("old", "1"), incoming("new", "2")]

    validation = driver.validate_reply_target(events[0])
    valid, reason = validation

    assert valid is False
    assert "newer" in reason
    assert validation.replacement_event is not None
    assert validation.replacement_event.content == "new"
    assert validation.replacement_event.history[-1]["content"] == "old"


def test_reply_cycle_monitors_private_conversation_without_unread_marker():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    driver.begin_reply_cycle("Alice", first_events[0].conversation_id)
    client.rows = [row("Alice", unread=0, signature="unchanged")]
    client.histories["Alice"] = [incoming("old", "1"), incoming("new", "2")]

    _, monitored_events = driver.observe_events()

    assert [event.content for event in monitored_events] == ["new"]


def test_reply_cycle_monitors_group_without_session_mention_marker():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("@小牛 old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    driver.begin_reply_cycle("项目群", first_events[0].conversation_id)
    client.rows = [row("项目群", unread=0, mention=False, signature="unchanged")]
    client.histories["项目群"] = [
        incoming("@小牛 old", "1"),
        incoming("@小牛 new", "2"),
    ]

    _, monitored_events = driver.observe_events()

    assert [event.content for event in monitored_events] == ["@小牛 new"]


def test_reply_cycle_changes_wait_deadline_to_monitor_interval():
    class IdleHook(FakeHook):
        def wait(self, timeout):
            return False

    driver = WechatUiaDriver(
        {
            "shell_hook_reconcile_seconds": 15,
            "reply_monitor_interval_seconds": 0.25,
        },
        client=FakeClient(),
        shell_hook=IdleHook(),
    )
    driver.begin_reply_cycle("Alice", "uia-session:alice")

    started = time.monotonic()
    reason = driver.wait_for_changes(threading.Event())

    assert reason == "reply-monitor"
    assert time.monotonic() - started < 1.0


def test_global_reconcile_scan_is_disabled_by_default(monkeypatch):
    stop_event = threading.Event()

    class StopHook(FakeHook):
        def wait(self, timeout):
            stop_event.set()
            return False

    driver = WechatUiaDriver(
        {"shell_hook_reconcile_seconds": 1},
        client=FakeClient(),
        shell_hook=StopHook(),
    )
    monkeypatch.setattr(
        "channel.wechat_desktop.uia_driver.time.monotonic", lambda: 1000.0
    )

    assert driver.wait_for_changes(stop_event) == "stopped"


def test_global_reconcile_scan_can_be_enabled(monkeypatch):
    class IdleHook(FakeHook):
        def wait(self, timeout):
            return False

    ticks = iter([0.0, 2.0, 2.0])
    monkeypatch.setattr(
        "channel.wechat_desktop.uia_driver.time.monotonic", lambda: next(ticks)
    )
    driver = WechatUiaDriver(
        {
            "shell_hook_reconcile_enabled": True,
            "shell_hook_reconcile_seconds": 1,
        },
        client=FakeClient(),
        shell_hook=IdleHook(),
    )

    assert driver.wait_for_changes(threading.Event()) == "reconcile"


def test_reply_target_key_ignores_bounds_changes():
    first = UiaChatMessage(
        "Alice", "same", bounds=(10, 20, 100, 60), runtime_id="1"
    )
    moved = UiaChatMessage(
        "Alice", "same", bounds=(30, 40, 120, 80), runtime_id="1"
    )

    assert WechatUiaDriver._target_key(first, 3) == WechatUiaDriver._target_key(
        moved, 3
    )


def test_reply_target_key_ignores_visible_index_changes():
    message = UiaChatMessage("Alice", "same", runtime_id="42.1")

    assert WechatUiaDriver._target_key(message, 4) == WechatUiaDriver._target_key(
        message, 0
    )


def test_reply_target_key_distinguishes_identical_messages_by_stable_id():
    first = UiaChatMessage("Alice", "same", runtime_id="42.1", stable_id="first")
    second = UiaChatMessage("Alice", "same", runtime_id="42.2", stable_id="second")

    assert WechatUiaDriver._target_key(first, 4) != WechatUiaDriver._target_key(
        second, 4
    )


def test_message_snapshot_keeps_identity_when_runtime_id_changes():
    driver = WechatUiaDriver({}, client=FakeClient(), shell_hook=FakeHook())
    first = driver._stabilize_messages(
        "conversation", [incoming("same", "runtime-a")]
    )
    recreated = driver._stabilize_messages(
        "conversation", [incoming("same", "runtime-b")]
    )

    assert recreated[0].stable_id == first[0].stable_id


def test_message_snapshot_distinguishes_new_identical_message():
    driver = WechatUiaDriver({}, client=FakeClient(), shell_hook=FakeHook())
    first = driver._stabilize_messages(
        "conversation", [incoming("same", "runtime-a")]
    )
    next_snapshot = driver._stabilize_messages(
        "conversation",
        [incoming("same", "runtime-a"), incoming("same", "runtime-b")],
    )

    assert next_snapshot[0].stable_id == first[0].stable_id
    assert next_snapshot[1].stable_id != first[0].stable_id


def test_group_reply_monitor_does_not_reemit_target_when_visible_index_shifts():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [
        incoming("普通消息 1", "1"),
        incoming("普通消息 2", "2"),
        incoming("@小牛 你还存活吗", "target"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    driver.begin_reply_cycle("项目群", first_events[0].conversation_id)
    client.rows = [row("项目群", unread=0, mention=False, signature="shifted")]
    client.histories["项目群"] = [
        incoming("普通消息 2", "2"),
        incoming("@小牛 你还存活吗", "target"),
    ]

    _, shifted_events = driver.observe_events()

    assert shifted_events == []


def test_revalidation_accepts_same_target_after_layout_moves():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage("Alice", "same", bounds=(10, 20, 100, 60))
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    client.histories["Alice"] = [
        UiaChatMessage("Alice", "same", bounds=(30, 40, 120, 80))
    ]

    validation = driver.validate_reply_target(events[0])

    assert validation.valid is True
    assert validation.replacement_event is None


def test_group_revalidation_uses_first_visible_at_message_without_direction():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    unknown_message = UiaChatMessage(
        "", "@小牛 请确认", direction="unknown", runtime_id="1"
    )
    client.histories["项目群"] = [unknown_message]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()
    valid, reason = driver.validate_reply_target(events[0])

    assert valid is True
    assert reason == ""


def test_group_revalidation_keeps_older_visible_at_target_valid():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("@小牛 first", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    client.histories["项目群"] = [
        incoming("@小牛 first", "1"),
        incoming("@小牛 second", "2"),
    ]

    validation = driver.validate_reply_target(events[0])

    assert validation.valid is True
    assert validation.replacement_event is None


def test_sending_does_not_clear_a_collected_group_target():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("@小牛 first", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    conversation_id = events[0].conversation_id
    emitted_target = driver._emitted_targets[conversation_id]

    driver.send_text(conversation_id, "reply")

    assert driver._emitted_targets[conversation_id] == emitted_target


def test_expired_queue_token_rejects_late_result():
    reply_queue = WechatReplyQueue()
    event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "hello"
    )
    reply_queue.enqueue(event)
    item = reply_queue.get()
    assert reply_queue.is_active(item.token)
    reply_queue.expire(item.token)
    assert not reply_queue.is_active(item.token)
    reply_queue.signal(item.token, "completed")
    assert item.terminal == ""


def test_shell_hook_filters_process_and_debounces_injected_signals():
    hook = WindowsShellHook(lambda: {42}, debounce_ms=250)
    hook.notify_for_tests(100, 7, created_at=1.0)
    assert hook.consume().signaled is False
    hook.notify_for_tests(100, 42, created_at=2.0)
    hook.notify_for_tests(100, 42, created_at=2.1)
    signal = hook.consume()
    assert signal.signaled is True
    assert signal.created_at == 2.0


def test_white_tree_recovery_stops_after_tree_returns(monkeypatch):
    client = WechatUiaClient({"uia_recovery_attempts": 3, "uia_recovery_settle_ms": 0})
    probes = iter([0, 0, 12])
    clicks = []
    monkeypatch.setattr(client, "probe_tree", lambda: next(probes))
    monkeypatch.setattr(client, "_click_taskbar_button", lambda: clicks.append(1) or True)
    assert client._recover_empty_tree(123) is True
    assert len(clicks) == 2
