"""引用形态和控制命令都必须遵守数据库原生来源契约。"""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.models import UiaReferencedMessage
from channel.wechat_desktop.pipeline.scan import WechatDesktopScanMixin
from channel.wechat_desktop.send_control import SendNotSubmitted
from .test_db_reader import table
from .test_db_uia_attachment_bridge import bridge, observe_target, reference_xml


@pytest.mark.parametrize("native_has_reference", [False, True])
def test_attachment_correlation_requires_same_reference_presence(bridge, native_has_reference):
    instance, _, _, client, _, _ = bridge
    if native_has_reference:
        body = reference_xml(49, "synthetic.pdf")
        reference = None
    else:
        body = "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>"
        reference = UiaReferencedMessage("Synthetic", "synthetic.pdf", "file")
    event = observe_target(bridge, body, reference=reference)
    history = instance.read_chat_history(event.conversation_id, 50)

    with pytest.raises(SendNotSubmitted, match="attachment_reference_mismatch"):
        WechatDatabaseBackend._correlate_attachment(event, history, client.messages)


def test_native_standalone_file_cannot_be_upgraded_to_uia_reference(bridge):
    instance, _, _, client, _, _ = bridge
    body = "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>"
    event = observe_target(bridge, body, reference=UiaReferencedMessage(
        "Synthetic", "synthetic.pdf", "file"))
    identity = event.source_message_id, event.fingerprint()

    result, count = instance.materialize_event(event)

    assert count == 0 and client.downloads == []
    assert result.attachment_status == "attachment_identity_unavailable"
    assert result.reference == {}
    assert (result.source_message_id, result.fingerprint()) == identity


def control_channel(driver):
    calls = SimpleNamespace(composed=[], produced=[], processed=[], stages=[], finished=[])

    def compose(*args, **kwargs):
        calls.composed.append((args, kwargs))
        return {}

    channel = SimpleNamespace(
        _driver=driver,
        _compose_context=compose,
        produce=calls.produced.append,
        _store=SimpleNamespace(mark_event_processed=lambda *args: calls.processed.append(args)),
        _mark_lifecycle=lambda *args: calls.stages.append(args),
        _finish_lifecycle=lambda *args: calls.finished.append(args),
    )
    return channel, calls


@pytest.mark.parametrize("command", ["/cancel", "/steer 改查测试"])
@pytest.mark.parametrize("change,reason", [
    ("delete", "source_message_deleted"),
    ("replace", "source_message_changed"),
    ("recalled", "source_message_filtered"),
    ("generation", "source_generation_changed"),
])
def test_control_command_revalidates_native_source_before_side_effects(bridge, command, change, reason):
    instance, reader, _, _, _, talker = bridge
    event = observe_target(bridge, command, message_type=1)
    cache = reader.caches["message/message_0.db"]
    if change == "generation":
        cache.status = replace(cache.status, generation="replacement-generation")
    else:
        with sqlite3.connect(cache.path) as connection:
            if change == "delete":
                connection.execute(f'DELETE FROM "{table(talker)}" WHERE local_id=2')
            elif change == "recalled":
                connection.execute(f'UPDATE "{table(talker)}" SET local_type=10000 WHERE local_id=2')
            else:
                connection.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=2',
                                   ("changed synthetic command",))
    cache.changed = True
    channel, calls = control_channel(instance)

    WechatDesktopScanMixin._dispatch_control_event(channel, event)

    assert calls.composed == [] and calls.produced == [] and calls.stages == []
    assert calls.processed == [(event.event_id, "skipped", reason)]
    assert calls.finished == [([event.event_id], "skipped")]


@pytest.mark.parametrize("command", ["/cancel", "/steer 改查测试"])
def test_control_command_with_valid_native_source_is_dispatched(bridge, command):
    instance = bridge[0]
    event = observe_target(bridge, command, message_type=1)
    validations = []
    original_validate = instance.validate_reply_target

    def validate(event):
        validations.append(event.source_message_id)
        return original_validate(event)

    driver = SimpleNamespace(validate_reply_target=validate)
    channel, calls = control_channel(driver)

    WechatDesktopScanMixin._dispatch_control_event(channel, event)

    assert validations == [event.source_message_id]
    assert len(calls.produced) == 1
    assert calls.produced[0]["wechat_desktop_source_event_ids"] == [event.event_id]
    assert calls.processed == [(event.event_id, "completed", "control_command")]


def test_control_command_validation_exception_is_skipped_without_side_effects(bridge):
    event = observe_target(bridge, "/cancel", message_type=1)

    def validate(_event):
        raise RuntimeError("synthetic source unavailable")

    channel, calls = control_channel(SimpleNamespace(validate_reply_target=validate))

    WechatDesktopScanMixin._dispatch_control_event(channel, event)

    assert calls.composed == [] and calls.produced == []
    assert calls.processed == [(event.event_id, "skipped", "source_validation_failed")]
    assert calls.finished == [([event.event_id], "skipped")]
