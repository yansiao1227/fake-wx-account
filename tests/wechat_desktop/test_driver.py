"""微信桌面 driver 回归测试。"""
from channel.wechat_desktop.models import HeaderInfo, UiaChatMessage
from channel.wechat_desktop.uia.shell_hook import WindowsShellHook
from channel.wechat_desktop.uia.client import WechatUiaClient, parse_session_accessible_name
from channel.wechat_desktop.uia.driver import WechatUiaDriver
from .helpers import FakeClient, FakeHook, incoming, outgoing, row


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


def test_group_emits_all_visible_mentions_in_message_order():
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
    driver.acknowledge_events([event.event_id for event in events])

    assert [event.content for event in events] == ["@小牛 第一条", "@小牛 第二条"]
    assert all(event.is_at for event in events)
    assert client.owner_calls >= 1
    assert [item["content"] for item in events[0].history] == [
        "这是后续普通消息",
        "@小牛 第二条",
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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

    assert client.file_fetches == []
    assert len(events) == 1
    assert events[0].content_type == "file"
    assert events[0].content == "report.pdf 8 KB"

    materialized, resolve_count = driver.materialize_event(events[0])

    assert client.file_fetches == ["report.pdf 8 KB"]
    assert resolve_count == 1
    assert materialized.content_type == "file"
    assert materialized.content == str(local_file)


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
    driver.acknowledge_events([event.event_id for event in events])

    assert client.image_fetches == []
    assert len(events) == 1
    assert events[0].content_type == "image"

    materialized, resolve_count = driver.materialize_event(events[0])

    assert client.image_fetches == ["[图片]"]
    assert resolve_count == 1
    assert materialized.content == str(local_image)
    assert materialized.evidence_path == str(local_image)


def test_attachment_history_keeps_previous_and_excludes_all_followups():
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
    driver = WechatUiaDriver({}, client=FakeClient(), shell_hook=FakeHook())

    history = driver._history_snapshot(messages, 6, False)

    assert [item["content"] for item in history] == [
        "before-0",
        "before-1",
        "before-2",
        "before-3",
        "before-4",
        "before-5",
    ]


def test_private_text_target_does_not_resolve_preceding_visible_file(tmp_path):
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
    driver.acknowledge_events([event.event_id for event in events])
    materialized, resolve_count = driver.materialize_event(events[0])

    assert [event.content for event in events] == ["读取这个文件"]
    assert resolve_count == 0
    assert client.file_fetches == []
    assert materialized.history[0]["content_type"] == "file"
    assert str(local_file) not in materialized.history[0]["content"]


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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in events])

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
    driver.acknowledge_events([event.event_id for event in first_events])
    client.rows = [row("Alice", unread=1, signature="second")]
    client.histories["Alice"] = [incoming("second", "recycled-runtime")]
    _, second_events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in second_events])

    assert [event.content for event in first_events] == ["first"]
    assert [event.content for event in second_events] == ["second"]
    assert first_events[0].event_id != second_events[0].event_id


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
