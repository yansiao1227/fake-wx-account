"""微信桌面 prompts 回归测试。"""
import threading
import time
from channel.wechat_desktop.models import HeaderInfo, UiaChatMessage, WechatDesktopEvent
from channel.wechat_desktop.uia.driver import WechatUiaDriver
from channel.wechat_desktop.config import DEFAULT_CONFIG
from channel.wechat_desktop.pipeline.prompts import _format_agent_notice, _format_failure_notice, _is_network_reply_error, _is_user_visible_tool_notice, _preflight_tool_notice_data, _tool_notice_subject
from .helpers import FakeClient, FakeHook, incoming, outgoing, row


def test_network_reply_error_detection():
    assert _is_network_reply_error(
        "Agent error: Connection error: SSL: UNEXPECTED_EOF_WHILE_READING"
    )
    assert _is_network_reply_error("request timed out")
    assert not _is_network_reply_error("invalid tool arguments")


def test_driver_private_revalidation_keeps_started_target_relevant():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in events])
    client.histories["Alice"] = [incoming("old", "1"), incoming("new", "2")]

    validation = driver.validate_reply_target(events[0])

    assert validation.valid is True
    assert validation.reason == ""
    assert validation.replacement_event is None


def test_reply_cycle_monitors_private_conversation_without_unread_marker():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in first_events])
    discovery_focus_calls = client.focus_calls
    driver.begin_reply_cycle("Alice", first_events[0].conversation_id)
    client.rows = [row("Alice", unread=0, signature="unchanged")]
    client.histories["Alice"] = [incoming("old", "1"), incoming("new", "2")]

    _, monitored_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in monitored_events])

    assert [event.content for event in monitored_events] == ["new"]
    assert client.focus_calls == discovery_focus_calls
    assert client.history_ensure_conversation[-1] is False


def test_discovery_focus_failure_backs_off_without_failing_observation():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)

    def deny_foreground():
        client.focus_calls += 1
        raise OSError(5, "SetForegroundWindow", "拒绝访问。")

    client.focus_window = deny_foreground
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())

    first_observation, first_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in first_events])
    second_observation, second_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in second_events])

    assert first_observation["error"] == ""
    assert second_observation["error"] == ""
    assert first_events == []
    assert second_events == []
    assert client.focus_calls == 1
    assert client.history_calls == []


def test_reply_cycle_ignores_interim_tool_notice_but_still_finds_followup():
    client = FakeClient()
    client.rows = [row("Alice", unread=1)]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in first_events])
    original = first_events[0]
    driver.begin_reply_cycle("Alice", original.conversation_id)
    notice = "我准备调用 `web_search` tool 查一查，稍等一下 🔧"
    driver.register_interim_text(original.conversation_id, notice)
    client.rows = [row("Alice", unread=0, signature="unchanged")]
    client.histories["Alice"] = [
        incoming("old", "1"),
        outgoing(notice, "2"),
    ]

    _, notice_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in notice_events])

    assert notice_events == []
    assert driver.validate_reply_target(original).valid is True

    client.histories["Alice"].append(incoming("new", "3"))
    _, followup_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in followup_events])

    assert [event.content for event in followup_events] == ["new"]
    assert notice not in [
        item["content"] for item in followup_events[0].history
    ]


def test_tool_notice_subject_recognizes_skill_reads_and_regular_tools():
    assert _tool_notice_subject(
        {
            "tool_name": "read",
            "arguments": {"path": r"C:\cow\skills\web-search\SKILL.md"},
        }
    ) == ("skill", "web-search")
    assert _tool_notice_subject(
        {
            "tool_name": "read",
            "arguments": '{"location":"skills/vision/SKILL.md"}',
        }
    ) == ("skill", "vision")
    assert _tool_notice_subject(
        {
            "tool_name": "read",
            "arguments": {
                "path": r"C:\cow\skills\@user_087fbff2\govwriting\SKILL.md"
            },
        }
    ) == ("skill", "govwriting")
    assert _tool_notice_subject(
        {"tool_name": "web_search", "arguments": {"query": "天气"}}
    ) == ("tool", "web_search")


def test_tool_notice_templates_always_output_the_concrete_tool_name():
    assert all(
        "{tool_name}" in template
        for template in DEFAULT_CONFIG["agent_tool_notice_templates"]
    )
    assert (
        _format_agent_notice(
            ["我准备调用 `{tool_name}` tool"], "tool", "web_search"
        )
        == "我准备调用 `web_search` tool"
    )
    assert (
        _format_agent_notice(["我正在使用工具，请稍等"], "tool", "vision")
        == "我正在使用工具，请稍等 当前工具：`vision`。"
    )
    assert (
        _format_agent_notice(["调用 `{name}` skill"], "skill", "pdf-reader")
        == "调用 `pdf-reader` skill"
    )


def test_failure_notice_has_fun_fallback_when_templates_are_empty():
    assert "小齿轮" in _format_failure_notice([])
    assert _format_failure_notice(["机器人暂时打了个喷嚏 🤖"]) == (
        "机器人暂时打了个喷嚏 🤖"
    )


def test_user_visible_tool_notice_skips_internal_tools_but_keeps_skills():
    assert _is_user_visible_tool_notice({"tool_name": "bash"}) is False
    assert _is_user_visible_tool_notice({"tool_name": "read"}) is False
    assert _is_user_visible_tool_notice({"tool_name": "ls"}) is False
    assert _is_user_visible_tool_notice({"tool_name": "web_search"}) is True
    assert _is_user_visible_tool_notice({"tool_name": "vision"}) is True
    assert _is_user_visible_tool_notice(
        {
            "tool_name": "read",
            "arguments": {"path": r"C:\cow\skills\docx\SKILL.md"},
        }
    ) is True
    assert _is_user_visible_tool_notice(
        {
            "tool_name": "read",
            "arguments": {
                "path": r"C:\cow\skills\@user_087fbff2\govwriting\SKILL.md"
            },
        }
    ) is True
    assert _is_user_visible_tool_notice(
        {"tool_name": "微信内置浏览器", "notice_template_key": "share_browser_notice_templates"}
    ) is False
    assert _is_user_visible_tool_notice(
        {"tool_name": "web_fetch", "notice_template_key": "share_content_fetch_notice_templates"}
    ) is True
    assert _is_user_visible_tool_notice(
        {"tool_name": "web_search"}, silent_tools=["web_search"]
    ) is False


def test_preflight_notice_predicts_attachment_tools_before_llm_turn():
    docx_event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "file", r"C:\tmp\LDAP.docx"
    )
    image_reference_event = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "image", "file_path": r"C:\tmp\quoted.png"},
    )
    unresolved_image_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "image"},
    )
    unresolved_file_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "file"},
    )
    pdf_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "inspect",
        reference={"content_type": "file", "file_path": r"C:\tmp\quoted.pdf"},
    )
    share_reference = WechatDesktopEvent(
        "message",
        "a",
        "Alice",
        "a",
        "Alice",
        "text",
        "讲了什么",
        reference={"content_type": "share_card", "content": "分享标题"},
    )
    standalone_share = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "share_card", "分享标题"
    )
    text_event = WechatDesktopEvent(
        "message", "a", "Alice", "a", "Alice", "text", "hello"
    )

    assert _tool_notice_subject(_preflight_tool_notice_data(docx_event)) == (
        "skill",
        "docx",
    )
    assert _tool_notice_subject(
        _preflight_tool_notice_data(image_reference_event)
    ) == ("tool", "vision")
    assert _preflight_tool_notice_data(unresolved_image_reference) is None
    assert _preflight_tool_notice_data(unresolved_file_reference) is None
    assert _tool_notice_subject(
        _preflight_tool_notice_data(pdf_reference)
    ) == ("skill", "pdf-reader")
    assert _preflight_tool_notice_data(share_reference) is None
    assert _preflight_tool_notice_data(standalone_share) is None
    assert _preflight_tool_notice_data(text_event) is None


def test_reply_cycle_monitors_group_without_session_mention_marker():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("@小牛 old", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, first_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in first_events])
    driver.begin_reply_cycle("项目群", first_events[0].conversation_id)
    client.rows = [row("项目群", unread=0, mention=False, signature="unchanged")]
    client.histories["项目群"] = [
        incoming("@小牛 old", "1"),
        incoming("@小牛 new", "2"),
    ]

    _, monitored_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in monitored_events])

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
        "channel.wechat_desktop.uia.driver.time.monotonic", lambda: 1000.0
    )

    assert driver.wait_for_changes(stop_event) == "stopped"


def test_global_reconcile_scan_can_be_enabled(monkeypatch):
    class IdleHook(FakeHook):
        def wait(self, timeout):
            return False

    ticks = iter([0.0, 2.0, 2.0])
    monkeypatch.setattr(
        "channel.wechat_desktop.uia.driver.time.monotonic", lambda: next(ticks)
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


def test_recent_runtime_identity_prevents_reemit_after_target_temporarily_disappears():
    client = FakeClient()
    client.rows = [row("Alice", unread=1, signature="first")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [
        incoming("一条临时可见的旧消息", "old-runtime"),
        incoming("讲了啥", "question-runtime"),
    ]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, first_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in first_events])
    driver.begin_reply_cycle("Alice", first_events[0].conversation_id)
    client.histories["Alice"] = [incoming("一条临时可见的旧消息", "old-runtime")]
    _, transient_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in transient_events])
    client.histories["Alice"] = [incoming("讲了啥", "question-runtime")]
    _, reappeared_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in reappeared_events])

    assert transient_events == []
    assert reappeared_events == []


def test_same_text_with_new_runtime_identity_is_a_new_message():
    client = FakeClient()
    client.rows = [row("Alice", unread=1, signature="first")]
    client.headers["Alice"] = HeaderInfo("Alice", "private", 1)
    client.histories["Alice"] = [incoming("讲了啥", "first-runtime")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )

    _, first_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in first_events])
    driver.begin_reply_cycle("Alice", first_events[0].conversation_id)
    client.histories["Alice"] = [
        incoming("讲了啥", "first-runtime"),
        incoming("讲了啥", "second-runtime"),
    ]
    _, second_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in second_events])

    assert [event.content for event in second_events] == ["讲了啥"]


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
    driver.acknowledge_events([event.event_id for event in first_events])
    driver.begin_reply_cycle("项目群", first_events[0].conversation_id)
    client.rows = [row("项目群", unread=0, mention=False, signature="shifted")]
    client.histories["项目群"] = [
        incoming("普通消息 2", "2"),
        incoming("@小牛 你还存活吗", "target"),
    ]

    _, shifted_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in shifted_events])

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
    driver.acknowledge_events([event.event_id for event in events])
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
    driver.acknowledge_events([event.event_id for event in events])
    validation = driver.validate_reply_target(events[0])

    assert validation.valid is True
    assert validation.reason == ""


def test_group_revalidation_keeps_older_visible_at_target_valid():
    client = FakeClient()
    client.rows = [row("项目群", unread=1, mention=True, preview_prefix=True)]
    client.headers["项目群"] = HeaderInfo("项目群", "group", 8)
    client.histories["项目群"] = [incoming("@小牛 first", "1")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True}, client=client, shell_hook=FakeHook()
    )
    _, events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in events])
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
    driver.acknowledge_events([event.event_id for event in events])
    conversation_id = events[0].conversation_id
    emitted_target = driver._emitted_targets[conversation_id]

    driver.send_text(conversation_id, "reply")

    assert driver._emitted_targets[conversation_id] == emitted_target
