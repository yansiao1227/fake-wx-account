"""原生近期附件只允许经已证实尾部进入有限、只读的历史定位。"""

import sqlite3
from dataclasses import replace

import pytest

from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.models import UiaChatMessage, UiaReferencedMessage
from channel.wechat_desktop.send_control import SendNotSubmitted
from .test_attachment_latest_scroll import scroll_client
from .test_db_reader import add_message, table
from .test_db_uia_attachment_bridge import bridge, observe_target, reference_xml


def hidden_target(bridge, *, reference=False, pages=None):
    instance, reader, _, client, _, talker = bridge
    body = (reference_xml(3, "&lt;img/&gt;") if reference else
            "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")
    event = observe_target(bridge, body, reference=(
        UiaReferencedMessage("Synthetic", "图片", degraded=True) if reference else None))
    full = list(client.messages)
    for local_id in range(4, 10):
        content = f"synthetic later {local_id}"
        add_message(reader.caches["message/message_0.db"], talker, local_id,
                    content=content, created=1000 + local_id, sort_seq=local_id)
        full.append(UiaChatMessage("Synthetic", content, direction="incoming",
                                   runtime_id=f"bubble-{local_id}"))
    client.messages = full[-2:]
    client.scrolls = 0
    client.get_attachment_message_list_identity = lambda: "verified-message-list"
    next_pages = iter(pages if pages is not None else [full[5:7], full[:3]])

    def scroll(*, expected_list_identity, validate):
        assert expected_list_identity == "verified-message-list"
        validate()
        client.scrolls += 1
        client.bottom = False
        client.messages = list(next(next_pages))
        validate()

    client.scroll_chat_history_for_attachment = scroll
    return event, full


@pytest.mark.parametrize("reference", [False, True])
def test_hidden_recent_file_or_reference_is_located_once_from_proven_tail(bridge, reference):
    event, full = hidden_target(bridge, reference=reference)

    result, count = bridge[0].materialize_event(event)

    assert count == 1 and result.attachment_status == "materialized"
    assert bridge[3].scrolls == 2
    assert bridge[3].downloads == [("reference_image" if reference else "file", "bubble-2")]
    assert bridge[3].bottom is False


def test_locator_and_materializer_guards_never_refocus_or_reselect_conversation(bridge, monkeypatch):
    event, _ = hidden_target(bridge)
    client, gateway = bridge[3], bridge[4]
    verifies = []
    selections = []
    original_verify = gateway.verify_target
    original_select = client.locate_conversation
    def verify(*args, **kwargs):
        verifies.append(True)
        assert len(verifies) == 1, "来源 guard 不得重复交互验证会话"
        return original_verify(*args, **kwargs)
    def select(*args, **kwargs):
        selections.append(True)
        assert len(selections) <= 2, "有限定位期间不得重新选择会话"
        return original_select(*args, **kwargs)
    monkeypatch.setattr(gateway, "verify_target", verify)
    monkeypatch.setattr(client, "locate_conversation", select)
    result, count = bridge[0].materialize_event(event)
    assert count == 1 and result.attachment_status == "materialized"
    assert len(verifies) == 1 and len(selections) == 2
    assert client.focus_count == 2 and client.scrolls == 2


def test_locator_checks_full_database_only_twice_per_page_and_never_inside_scroll(bridge, monkeypatch):
    event, _ = hidden_target(bridge)
    instance, reader, _, client, gateway, _ = bridge
    active = [False]
    counts = {"history": 0, "source": 0, "refresh": 0}
    elapsed = [0.]
    original_locate = instance._locate_attachment
    original_refresh = reader.refresh
    original_history = instance.source.read_chat_history
    original_source = instance.source.validate_event
    original_scroll = gateway.scroll_attachment_history
    def locate(*args, **kwargs):
        active[0] = True
        try:
            return original_locate(*args, **kwargs)
        finally:
            active[0] = False
    def refresh(*args, **kwargs):
        if active[0]:
            counts["refresh"] += 1
            elapsed[0] += .25
        return original_refresh(*args, **kwargs)
    def history(*args, **kwargs):
        if active[0]:
            counts["history"] += 1
        return original_history(*args, **kwargs)
    def source(*args, **kwargs):
        if active[0]:
            counts["source"] += 1
        return original_source(*args, **kwargs)
    def scroll(*args, **kwargs):
        before = dict(counts)
        result = original_scroll(*args, **kwargs)
        assert counts == before, "滚动内部的原子身份 guard 不得探测或读取数据库"
        return result
    monkeypatch.setattr(instance, "_locate_attachment", locate)
    monkeypatch.setattr(reader, "refresh", refresh)
    monkeypatch.setattr(instance.source, "read_chat_history", history)
    monkeypatch.setattr(instance.source, "validate_event", source)
    monkeypatch.setattr(gateway, "scroll_attachment_history", scroll)
    monkeypatch.setattr("channel.wechat_desktop.hybrid.time.monotonic", lambda: elapsed[0])
    result, count = instance.materialize_event(event)
    assert count == 1 and result.attachment_status == "materialized"
    assert client.scrolls == 2
    assert counts["history"] == counts["source"] == 2 * client.scrolls
    # read_chat_history 当前含 reader.read_history 的再刷新；原生校验另一次。
    # 绑定复用该原子段刚刷新的快照，且不随多个廉价 UI guard 增加探测次数。
    assert counts["refresh"] <= 6 * client.scrolls
    assert elapsed[0] <= 3.


def test_target_first_seen_clipped_continues_one_more_step_until_fully_visible(bridge):
    event, full = hidden_target(bridge)
    partial = list(full[:3])
    partial[1] = replace(partial[1], bounds=(10, -30, 500, 70))
    next_pages = iter([partial, full[:3]])
    def scroll(*, expected_list_identity, validate):
        validate()
        bridge[3].scrolls += 1
        bridge[3].bottom = False
        bridge[3].messages = list(next(next_pages))
        validate()
    bridge[3].scroll_chat_history_for_attachment = scroll
    result, count = bridge[0].materialize_event(event)
    assert count == 1 and result.attachment_status == "materialized"
    assert bridge[3].scrolls == 2
    assert bridge[3].downloads == [("file", "bubble-2")]


def test_initial_proven_tail_with_clipped_target_also_requires_bounded_locator(bridge):
    event = observe_target(bridge, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>")
    client = bridge[3]
    full = list(client.messages)
    client.messages[1] = replace(client.messages[1], bounds=(10, -20, 500, 80))
    client.scrolls = 0
    def scroll(*, expected_list_identity, validate):
        validate()
        client.scrolls += 1
        client.bottom = False
        client.messages = full
        validate()
    client.scroll_chat_history_for_attachment = scroll
    assert bridge[0].materialize_event(event)[1] == 1
    assert client.scrolls == 1 and client.downloads == [("file", "bubble-2")]


def test_persistently_clipped_target_never_reaches_attachment_click(bridge):
    event, full = hidden_target(bridge)
    bridge[0].config["uia_attachment_scroll_wheel_times"] = 1
    def scroll(*, expected_list_identity, validate):
        bridge[3].scrolls += 1
        bridge[3].messages = list(full[:3])
        bridge[3].messages[1] = replace(full[1], bounds=(10, -20, 500, 80))
    bridge[3].scroll_chat_history_for_attachment = scroll
    assert bridge[0].materialize_event(event)[1] == 0
    assert bridge[3].scrolls == 1 and bridge[3].downloads == []


def test_target_clipped_after_locate_is_rejected_before_capture(bridge, monkeypatch):
    event, _ = hidden_target(bridge)
    gateway = bridge[4]
    original = gateway.materialize_event
    def clipped(*args, **kwargs):
        bridge[3].messages[1] = replace(bridge[3].messages[1], bounds=(10, -20, 500, 80))
        return original(*args, **kwargs)
    monkeypatch.setattr(gateway, "materialize_event", clipped)
    assert bridge[0].materialize_event(event)[1] == 0
    assert bridge[3].downloads == []


def test_non_tail_requires_explicit_locator_mode_and_aligns_exact_slice(bridge):
    event, full = hidden_target(bridge)
    history = bridge[0].source.read_chat_history(event.conversation_id, 50)
    with pytest.raises(SendNotSubmitted, match="attachment_sequence_ambiguous"):
        WechatDatabaseBackend._correlate_attachment(event, history, full[:3])
    matched = WechatDatabaseBackend._correlate_attachment(event, history, full[:3], allow_non_tail=True)
    assert matched.runtime_id == "bubble-2"


def test_initial_non_bottom_is_not_upgraded_to_history_locator(bridge):
    event, _ = hidden_target(bridge)
    bridge[3].bottom = False
    assert bridge[0].materialize_event(event)[1] == 0
    assert bridge[3].scrolls == 0 and bridge[3].downloads == []


def test_initial_old_sequence_without_native_tail_proof_never_scrolls(bridge):
    event, full = hidden_target(bridge)
    bridge[3].messages = full[3:5]
    assert bridge[0].materialize_event(event)[1] == 0
    assert bridge[3].scrolls == 0 and bridge[3].downloads == []


def test_stuck_viewport_stops_after_single_scroll(bridge):
    event, full = hidden_target(bridge)
    bridge[3].scroll_chat_history_for_attachment = lambda **kwargs: setattr(
        bridge[3], "scrolls", bridge[3].scrolls + 1)
    result, count = bridge[0].materialize_event(event)
    assert count == 0 and result.reference == {}
    assert bridge[3].scrolls == 1 and bridge[3].downloads == []


def test_scroll_limit_stops_before_further_pages(bridge):
    event, _ = hidden_target(bridge)
    bridge[0].config["uia_attachment_scroll_wheel_times"] = 1
    assert bridge[0].materialize_event(event)[1] == 0
    assert bridge[3].scrolls == 1 and bridge[3].downloads == []


def test_hard_scroll_cap_prevents_unbounded_configuration(bridge):
    event, full = hidden_target(bridge)
    bridge[0].config["uia_attachment_scroll_wheel_times"] = 10000
    bridge[0].config["uia_attachment_history_timeout_seconds"] = 30
    def scroll(*, expected_list_identity, validate):
        validate()
        bridge[3].scrolls += 1
        bridge[3].messages = [replace(item, bounds=(0, bridge[3].scrolls, 50, 100))
                               for item in full[-2:]]
        validate()
    bridge[3].scroll_chat_history_for_attachment = scroll
    assert bridge[0].materialize_event(event)[1] == 0
    assert bridge[3].scrolls == 30 and bridge[3].downloads == []


def test_deadline_expiring_after_scroll_prevents_attachment_action(bridge, monkeypatch):
    bridge[0].config["uia_attachment_history_timeout_seconds"] = 20
    event, full = hidden_target(bridge, pages=[])
    ticks = iter([0., 0., 0., 25.])
    monkeypatch.setattr("channel.wechat_desktop.hybrid.time.monotonic", lambda: next(ticks, 25.))
    def scroll(*, expected_list_identity, validate):
        bridge[3].scrolls += 1
        bridge[3].messages = full[:3]
    bridge[3].scroll_chat_history_for_attachment = scroll
    assert bridge[0].materialize_event(event)[1] == 0
    assert bridge[3].scrolls == 1 and bridge[3].downloads == []


@pytest.mark.parametrize("change", ["native_source", "native_history", "list_identity"])
def test_changes_during_scroll_are_rejected_before_download(bridge, change):
    event, full = hidden_target(bridge)
    instance, reader, _, client, _, talker = bridge
    def scroll(*, expected_list_identity, validate):
        validate()
        client.scrolls += 1
        client.bottom = False
        client.messages = full[:3]
        cache = reader.caches["message/message_0.db"]
        if change == "native_source":
            with sqlite3.connect(cache.path) as conn:
                conn.execute(f"UPDATE {table(talker)} SET message_content=? WHERE local_id=2", ("changed source",))
            cache.changed = True
        elif change == "native_history":
            add_message(cache, talker, 10, content="new database tail", created=1010, sort_seq=10)
        else:
            client.get_attachment_message_list_identity = lambda: "replacement-list"
        validate()
    client.scroll_chat_history_for_attachment = scroll
    result, count = instance.materialize_event(event)
    assert count == 0 and client.downloads == []
    if change == "native_source":
        assert result.attachment_status == "source_invalid"


@pytest.mark.parametrize("change", ["window", "account", "row", "messages", "database", "list_identity",
                                    "group_type", "bound_selector"])
def test_after_locate_validators_keep_original_window_native_and_visible_mapping(bridge, monkeypatch, change):
    event, _ = hidden_target(bridge)
    instance, reader, _, client, gateway, talker = bridge
    original = gateway.materialize_event
    def changed(*args, **kwargs):
        if change == "window":
            client.hwnd += 1
        elif change == "account":
            client.wxid = "other-account"
        elif change == "row":
            client.rows = [replace(client.rows[0], runtime_id="other-row")]
        elif change == "messages":
            client.messages[1] = replace(client.messages[1], runtime_id="replacement-bubble")
        elif change == "database":
            add_message(reader.caches["message/message_0.db"], talker, 10,
                        content="new database tail", created=1010, sort_seq=10)
        elif change == "group_type":
            client.header_type = "group"
        elif change == "bound_selector":
            gateway.bind_target(event.conversation_id, replace(client.rows[0], runtime_id="different-selector"))
        else:
            client.get_attachment_message_list_identity = lambda: "replacement-list"
        return original(*args, **kwargs)
    monkeypatch.setattr(gateway, "materialize_event", changed)
    assert instance.materialize_event(event)[1] == 0
    assert client.downloads == []


@pytest.mark.parametrize("change", ["token", "runtime", "reference", "sender", "type"])
def test_non_tail_target_remains_strictly_unique_and_reference_bound(bridge, change):
    event, full = hidden_target(bridge, reference=True)
    history = bridge[0].source.read_chat_history(event.conversation_id, 50)
    current = list(full[:3])
    if change == "token":
        history.messages[-1] = replace(history.messages[-1], content=event.content,
                                        content_type=event.content_type)
    elif change == "runtime":
        current[0] = replace(current[0], runtime_id=current[1].runtime_id)
    elif change == "reference":
        current[1] = replace(current[1], reference=replace(current[1].reference, content="different image"))
    elif change == "sender":
        current[1] = replace(current[1], reference=replace(current[1].reference, sender_name="Other sender"))
    else:
        current[1] = replace(current[1], message_type="file")
    with pytest.raises(SendNotSubmitted):
        WechatDatabaseBackend._correlate_attachment(event, history, current, allow_non_tail=True)


def test_duplicate_native_source_id_refuses_locator_before_scroll(bridge, monkeypatch):
    event, _ = hidden_target(bridge)
    original = bridge[0].source.read_chat_history
    def duplicated(*args, **kwargs):
        history = original(*args, **kwargs)
        history.messages[-1] = replace(history.messages[-1], source_message_id=event.source_message_id)
        return history
    monkeypatch.setattr(bridge[0].source, "read_chat_history", duplicated)
    assert bridge[0].materialize_event(event)[1] == 0
    assert bridge[3].scrolls == 0 and bridge[3].downloads == []


def test_non_bottom_snapshot_refuses_same_boolean_state_with_moving_bubbles(bridge):
    event, _ = hidden_target(bridge)
    client, gateway = bridge[3], bridge[4]
    gateway.bind_target(event.conversation_id, client.rows[0])
    client.bottom = False
    original = client.get_send_bubble_snapshot
    calls = 0
    def moving(*args, **kwargs):
        nonlocal calls
        calls += 1
        return [replace(item, bounds=(0, calls, 50, 100)) for item in original(*args, **kwargs)]
    client.get_send_bubble_snapshot = moving
    with pytest.raises(SendNotSubmitted, match="attachment_visible_sequence_changed"):
        gateway.read_attachment_snapshot(event.conversation_id, allow_non_bottom=True)


def test_history_scroll_acts_once_on_verified_list_and_revalidates(monkeypatch):
    client, lists, _ = scroll_client(monkeypatch)
    lists[0].GetRuntimeId = lambda: [42, 100]
    actions = []
    lists[0].WheelUp = lambda **kwargs: actions.append(kwargs)
    validations = []
    identity = client.get_attachment_message_list_identity()
    client.scroll_chat_history_for_attachment(expected_list_identity=identity,
        validate=lambda: validations.append(len(actions)))
    assert identity == "42.100"
    assert validations == [0, 0, 1]
    assert actions == [{"wheelTimes": 1, "interval": .02, "waitTime": .1}]


@pytest.mark.parametrize("count", [0, 2])
def test_history_scroll_rejects_missing_or_ambiguous_lists(monkeypatch, count):
    client, lists, _ = scroll_client(monkeypatch, count=count)
    for control in lists:
        control.GetRuntimeId = lambda: [42, 100]
        control.WheelUp = lambda **kwargs: pytest.fail("不得滚动未确认列表")
    with pytest.raises(RuntimeError, match="attachment_message_list_changed"):
        client.scroll_chat_history_for_attachment(expected_list_identity="42.100", validate=lambda: None)


def test_history_scroll_rejects_replaced_runtime_before_action(monkeypatch):
    client, lists, _ = scroll_client(monkeypatch)
    lists[0].GetRuntimeId = lambda: [42, 101]
    lists[0].WheelUp = lambda **kwargs: pytest.fail("不得滚动替换后的列表")
    with pytest.raises(RuntimeError, match="attachment_message_list_changed"):
        client.scroll_chat_history_for_attachment(expected_list_identity="42.100", validate=lambda: None)


@pytest.mark.parametrize("bounds,expected", [((10, 10, 490, 490), True), ((10, -1, 490, 99), False),
                                            ((10, 450, 490, 550), False), ((-1, 10, 490, 100), False)])
def test_native_fully_visible_check_uses_same_list_and_exact_target_bounds(monkeypatch, bounds, expected):
    from .helpers import GeometryControl
    client, lists, _ = scroll_client(monkeypatch)
    lists[0].GetRuntimeId = lambda: [42, 100]
    target = GeometryControl(bounds, name="synthetic target", class_name="mmui::ChatTextItemView")
    target.GetRuntimeId = lambda: [42, 200]
    target.Click = lambda: pytest.fail("可见性检查只能读取控件")
    lists[0]._children = [target]
    message = UiaChatMessage("Synthetic", "synthetic target", runtime_id="42.200", bounds=bounds)
    assert client.is_attachment_message_fully_visible(message, expected_list_identity="42.100") is expected


@pytest.mark.parametrize("change", ["bounds", "runtime", "name", "duplicate"])
def test_native_visibility_rejects_changed_or_recycled_control_evidence(monkeypatch, change):
    from .helpers import GeometryControl
    client, lists, _ = scroll_client(monkeypatch)
    lists[0].GetRuntimeId = lambda: [42, 100]
    target = GeometryControl((10, 10, 490, 100), name="synthetic target", class_name="mmui::ChatTextItemView")
    target.GetRuntimeId = lambda: [42, 200]
    lists[0]._children = [target]
    message = UiaChatMessage("Synthetic", "synthetic target", runtime_id="42.200", bounds=(10, 10, 490, 100))
    if change == "bounds":
        message = replace(message, bounds=(10, 9, 490, 100))
    elif change == "runtime":
        target.GetRuntimeId = lambda: [42, 201]
    elif change == "name":
        target.Name = "changed content"
    else:
        lists[0]._children.append(target)
    with pytest.raises(RuntimeError):
        client.is_attachment_message_fully_visible(message, expected_list_identity="42.100")
