import threading
import time
from types import SimpleNamespace

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
            raise AssertionError("WeChat 4.x InvokePattern must not be used")

    WechatUiaClient({})._click_send_button(Button())

    assert calls == [
        ("click", {"simulateMove": False, "waitTime": 0.1}),
        ("restore", (123, 456)),
    ]


class GeometryControl:
    def __init__(self, bounds, name="", class_name="", children=None):
        self.BoundingRectangle = SimpleNamespace(
            left=bounds[0], top=bounds[1], right=bounds[2], bottom=bounds[3]
        )
        self.Name = name
        self.ClassName = class_name
        self.AutomationId = ""
        self.ControlTypeName = "CustomControl"
        self._children = list(children or [])

    def GetChildren(self):
        return list(self._children)


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


def test_message_direction_fails_closed_for_centered_control():
    centered = GeometryControl(
        (390, 100, 610, 160), "system", "mmui::ChatItemView"
    )
    assert WechatUiaClient._message_direction(centered, (100, 50, 900, 700))[0] == "unknown"


class FakeClient:
    def __init__(self):
        self.operation_lock = threading.RLock()
        self.rows = []
        self.histories = {}
        self.headers = {}
        self.history_calls = []
        self.focus_calls = 0

    def allowed_process_ids(self):
        return {42}

    def get_owner_info(self):
        return OwnerInfo("小牛", source="config", confidence="medium")

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
        confidence="high",
    )


def outgoing(content, runtime_id):
    return UiaChatMessage(
        "小牛",
        content,
        direction="outgoing",
        runtime_id=runtime_id,
        confidence="high",
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
        {"bootstrap_existing_messages": True, "uia_message_limit": 5},
        client=client,
        shell_hook=FakeHook(),
    )

    _, events = driver.observe_events()

    assert [event.content for event in events] == ["@小牛 第二条"]
    assert events[0].is_at is True
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


def test_group_outgoing_after_mention_means_it_is_already_answered():
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

    assert events == []


def test_group_outgoing_bubble_is_not_enqueued_even_without_sender_prefix():
    client = FakeClient()
    client.rows = [row("项目群", unread=0, mention=True, preview_prefix=False)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [outgoing("@小牛 自己发送的测试", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True, "auto_reply_groups": ["项目群"]},
        client=client,
        shell_hook=FakeHook(),
    )
    _, events = driver.observe_events()
    assert events == []


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
    assert [item["direction"] for item in events[0].history] == [
        "incoming",
        "outgoing",
    ]


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
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        UiaChatMessage("", "ambiguous", direction="unknown", runtime_id="1")
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, events = driver.observe_events()

    assert events == []


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


def test_reply_queue_expires_active_target_when_newer_message_arrives():
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

    assert result.action == "superseded_active"
    assert result.cancel_event_id == old.event_id
    assert active.expired is True
    assert active.terminal == "superseded"
    reply_queue.finish(active, "superseded")
    assert reply_queue.get().event.content == "new"
    assert reply_queue.status()["queue_superseded"] == 1


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

    valid, reason = driver.validate_reply_target(events[0])

    assert valid is False
    assert "newer" in reason


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
