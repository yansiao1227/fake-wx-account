"""附件滚动只作用于已核验的消息列表，Qt 无 ScrollPattern 仍须验证。"""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.send_control import SendNotSubmitted
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.controls import MESSAGE_LIST_ID
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from .helpers import GeometryControl
from .test_db_uia_attachment_bridge import AttachmentClient


def scroll_client(monkeypatch, *, count=1, pattern=None, settings=None):
    client = WechatUiaClient(settings or {})
    calls = []
    lists = [GeometryControl((0, 0, 500, 500), automation_id=MESSAGE_LIST_ID)
             for _ in range(count)]
    for index, control in enumerate(lists):
        control.GetScrollPattern = lambda: pattern
        control.WheelDown = lambda _index=index, **kwargs: calls.append((_index, kwargs))
    editor = GeometryControl((0, 500, 500, 600), automation_id="chat_input")
    editor.WheelDown = lambda **kwargs: pytest.fail("附件滚动不得作用于输入框")
    root = GeometryControl((0, 0, 500, 600), children=[editor, *lists])

    @contextmanager
    def current_root():
        yield root

    monkeypatch.setattr(client, "_uia_root", current_root)
    return client, lists, calls


@pytest.mark.parametrize("configured,expected", [(0, 1), (8, 8), (999, 30)])
def test_qt_wheel_fallback_is_bounded_and_uses_only_unique_message_list(monkeypatch, configured, expected):
    client, lists, calls = scroll_client(
        monkeypatch, settings={"uia_attachment_scroll_wheel_times": configured})
    validations = []

    def validate():
        validations.append(len(calls))

    client.scroll_chat_to_latest_for_attachment(validate=validate)

    assert validations == [0, 0, 1]
    assert len(calls) == 1 and calls[0][0] == 0
    assert calls[0][1]["wheelTimes"] == expected


def test_scroll_pattern_goes_to_latest_without_wheel_or_editor_input(monkeypatch):
    pattern_calls = []
    pattern = SimpleNamespace(VerticallyScrollable=True,
                              SetScrollPercent=lambda *args: pattern_calls.append(args))
    client, lists, calls = scroll_client(monkeypatch, pattern=pattern)
    validations = []

    client.scroll_chat_to_latest_for_attachment(
        validate=lambda: validations.append(len(pattern_calls)))

    assert pattern_calls == [(-1, 100)]
    assert validations == [0, 0, 1]
    assert calls == []


def test_missing_scroll_pattern_exception_uses_same_bounded_list(monkeypatch):
    client, lists, calls = scroll_client(monkeypatch)

    def unavailable():
        raise RuntimeError("Qt does not expose ScrollPattern")

    lists[0].GetScrollPattern = unavailable
    client.scroll_chat_to_latest_for_attachment(validate=lambda: None)

    assert len(calls) == 1 and calls[0][1]["wheelTimes"] <= 30


@pytest.mark.parametrize("count", [0, 2])
def test_absent_or_ambiguous_message_lists_refuse_all_scroll_actions(monkeypatch, count):
    client, lists, calls = scroll_client(monkeypatch, count=count)

    with pytest.raises(RuntimeError, match="attachment_message_list_unavailable"):
        client.scroll_chat_to_latest_for_attachment(validate=lambda: None)

    assert calls == []


@pytest.mark.parametrize("failed_validation,actions", [(1, 0), (2, 0), (3, 1)])
def test_attachment_source_or_target_change_is_propagated_before_and_after_scroll(
        monkeypatch, failed_validation, actions):
    client, lists, calls = scroll_client(monkeypatch)
    count = 0

    def validate():
        nonlocal count
        count += 1
        if count == failed_validation:
            raise SendNotSubmitted("attachment_target_changed")

    with pytest.raises(SendNotSubmitted, match="attachment_target_changed"):
        client.scroll_chat_to_latest_for_attachment(validate=validate)

    assert len(calls) == actions
    assert count == failed_validation


@pytest.mark.parametrize("before,after", [(None, None), (True, True)])
def test_snapshot_accepts_stable_unknown_or_bottom_state_without_claiming_identity(tmp_path, before, after):
    client = AttachmentClient(tmp_path / "unused.pdf")
    positions = iter([before, after])
    client.get_chat_scroll_position_passive = lambda: next(positions)
    client.messages = [SimpleNamespace(content="synthetic bubble")]
    gateway = WechatUiaGateway({}, client=client)
    gateway.bind_target("synthetic-session", client.rows[0])
    try:
        assert gateway.read_attachment_snapshot("synthetic-session") == tuple(client.messages)
        assert client.downloads == []
    finally:
        gateway.close()


@pytest.mark.parametrize("before,after", [(None, True), (True, None), (False, False),
                                         (False, None), (None, False), (False, True), (True, False)])
def test_snapshot_refuses_changed_or_known_non_bottom_state(tmp_path, before, after):
    client = AttachmentClient(tmp_path / "unused.pdf")
    positions = iter([before, after])
    client.get_chat_scroll_position_passive = lambda: next(positions)
    gateway = WechatUiaGateway({}, client=client)
    gateway.bind_target("synthetic-session", client.rows[0])
    try:
        with pytest.raises(SendNotSubmitted, match="attachment_bottom_unverified"):
            gateway.read_attachment_snapshot("synthetic-session")
        assert client.downloads == []
    finally:
        gateway.close()
