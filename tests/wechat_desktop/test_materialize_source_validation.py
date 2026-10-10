"""聚合批次中的每个原生来源都要有效，撤回正文不得流入 Agent 上文。"""

import sqlite3
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.materialize import WechatDesktopMaterializeMixin
from .test_db_reader import add_message, table
from .test_reference_pipeline_contract import native_channel, reference_xml


class BatchHarness(WechatDesktopMaterializeMixin):
    def __init__(self, driver):
        self._driver = driver
        self.config = {}
        self.evidence_events = []
        self.lifecycle = []
        self.captured = []

    def _preserve_event_evidence(self, event):
        self.evidence_events.append(event.event_id)

    def _mark_lifecycle(self, event_ids, stage, **metadata):
        self.lifecycle.append((list(event_ids), stage, metadata))

    def _trace(self, *_args, **_kwargs):
        pass

    def _compose_context(self, _ctype, content, **_kwargs):
        self.captured.append(content)


def synthetic_event(local_id, content, **kwargs):
    return WechatDesktopEvent(
        "message", "synthetic-conversation", "Synthetic", "synthetic-sender", "Synthetic",
        "text", content, account_id="synthetic-account",
        source_stream_id="message_0/Msg_synthetic",
        source_message_id=f"message_0/Msg_synthetic/{local_id}", source_local_id=local_id,
        message_stable_id=f"native-stable-{local_id}", **kwargs,
    )


@pytest.mark.parametrize("identity_key", ["_message_stable_id", "source_message_id"])
@pytest.mark.parametrize("invalid_marker", ["attachment_status", "reference_fetch_status"])
def test_invalid_predecessor_is_removed_by_source_identity_and_never_appended(identity_key, invalid_marker):
    first = synthetic_event(1, "read https://example.invalid/old-source")
    if invalid_marker == "attachment_status":
        first.attachment_status = "source_invalid"
    else:
        first.reference = {"content_type": "share_card", "fetch_status": "source_invalid",
                           "url": "https://example.invalid/old-source"}
    identity = first.message_stable_id if identity_key == "_message_stable_id" else first.source_message_id
    second = synthetic_event(2, "What happened to the previous message?", history=[
        {identity_key: identity, "content": "a different stale rendering", "content_type": "text"},
        {"source_message_id": "unrelated-source", "content": "valid unrelated history", "content_type": "text"},
    ])
    harness = BatchHarness(SimpleNamespace(materialize_event=lambda event: (event, 0)))

    result = harness._materialize_batch([first, second])

    assert result is second
    assert result.task.source_event_ids == [first.event_id, second.event_id]
    assert result.history == [{"source_message_id": "unrelated-source",
                               "content": "valid unrelated history", "content_type": "text"}]
    assert first.event_id not in harness.evidence_events
    assert second.event_id in harness.evidence_events
    assert all("old-source" not in str(item) for item in result.history)


def test_valid_predecessor_is_still_aggregated_with_native_history_identity():
    first = synthetic_event(1, "valid predecessor text")
    second = synthetic_event(2, "continue the discussion", history=[{
        "sender_name": first.sender_name, "content": "native preview", "content_type": "text",
        "_message_stable_id": first.message_stable_id, "source_message_id": first.source_message_id,
    }])
    harness = BatchHarness(SimpleNamespace(materialize_event=lambda event: (event, 0)))

    result = harness._materialize_batch([first, second])

    assert result.task.source_event_ids == [first.event_id, second.event_id]
    assert result.history == [{
        "sender_name": first.sender_name, "content": first.content, "content_type": "text",
        "_message_stable_id": first.message_stable_id, "source_message_id": first.source_message_id,
    }]
    assert harness.evidence_events == [first.event_id, second.event_id]


def receive_batch(native_channel):
    backend, _, store, _, _, _ = native_channel
    status, events = backend.observe_events()
    batch = status["source_batch"]
    store.receive_source_batch(batch)
    backend.acknowledge_events([batch.batch_id])
    return sorted(events, key=lambda event: event.source_local_id)


def test_five_native_aggregated_messages_keep_recent_database_window_and_fifo(native_channel):
    backend, reader, _, talker, _, _ = native_channel
    reader.config["reply_context_max_messages"] = 3
    cache = reader.caches["message/message_0.db"]
    for local_id in range(1, 6):
        add_message(cache, talker, local_id, content=f"synthetic {local_id}", created=local_id)
    events = receive_batch(native_channel)
    harness = BatchHarness(backend)
    result = harness._materialize_batch(events)

    assert [item["content"] for item in result.history] == ["synthetic 2", "synthetic 3", "synthetic 4"]
    assert result.task.source_event_ids == [event.event_id for event in events]
    assert [source.source_message_id for source in result.task.context_source_events] == [
        item["source_message_id"] for item in result.history]
    assert [source.source_local_id for source in result.task.source_validation_events] == [1, 2, 3, 4, 5]
    reply_queue = WechatReplyQueue()
    assert reply_queue.enqueue(result)
    queued = reply_queue.get()
    queued.event.history = []
    queued.restore_context()
    try:
        assert [item["content"] for item in queued.event.history] == ["synthetic 2", "synthetic 3", "synthetic 4"]
    finally:
        reply_queue.finish(queued, "skipped")


def change_source(cache, talker, local_ids):
    with sqlite3.connect(cache.path) as connection:
        for local_id in local_ids:
            connection.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=?',
                               ("synthetic changed source", local_id))
    cache.changed = True


def test_changed_native_predecessor_url_cannot_reach_valid_target_agent_context(native_channel):
    backend, reader, _, talker, _, _ = native_channel
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="read https://example.invalid/retracted", server_id=71, created=100)
    add_message(cache, talker, 2, content="what does the previous message mean?", server_id=72, created=101)
    first, second = receive_batch(native_channel)
    # B 的入队历史包含 A 的原生来源；A 随后被改写，B 自身仍合法。
    assert any(item.get("source_message_id") == first.source_message_id for item in second.history)
    change_source(cache, talker, [1])
    harness = BatchHarness(backend)

    result = harness._materialize_batch([first, second])
    AgentReplyCoordinator(harness).dispatch(result)

    assert first.attachment_status == "source_invalid"
    assert result.attachment_status != "source_invalid"
    assert result.task.source_event_ids == [first.event_id, second.event_id]
    assert harness.captured and "retracted" not in harness.captured[0]
    assert all(item.get("source_message_id") != first.source_message_id for item in result.history)
    assert first.event_id not in harness.evidence_events


@pytest.mark.parametrize("invalid_ids", [[2], [1, 2]])
def test_invalid_final_native_target_stays_invalid_and_never_dispatches(native_channel, invalid_ids):
    backend, reader, _, talker, _, _ = native_channel
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="synthetic predecessor", server_id=71, created=100)
    add_message(cache, talker, 2, content="read https://example.invalid/final", server_id=72, created=101)
    first, second = receive_batch(native_channel)
    change_source(cache, talker, invalid_ids)
    harness = BatchHarness(backend)

    result = harness._materialize_batch([first, second])

    assert result is second and result.attachment_status == "source_invalid"
    assert result.task.source_event_ids == [first.event_id, second.event_id]
    assert AgentReplyCoordinator(harness).dispatch(result) is False
    assert harness.captured == []
    if len(invalid_ids) == 2:
        assert result.history == []
        assert harness.evidence_events == []


def test_quoted_target_retains_only_one_reference_without_batch_predecessor_context(native_channel):
    backend, reader, _, talker, _, _ = native_channel
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="synthetic referenced original", server_id=71, created=90)
    add_message(cache, talker, 2, content="unrelated https://example.invalid/predecessor", server_id=72, created=100)
    add_message(cache, talker, 3, content=reference_xml(1, "synthetic referenced", 71),
                message_type=49, server_id=73, created=101)
    original, previous, target = receive_batch(native_channel)
    harness = BatchHarness(backend)

    result = harness._materialize_batch([previous, target])
    AgentReplyCoordinator(harness).dispatch(result)

    assert result.task.source_event_ids == [previous.event_id, target.event_id]
    assert result.reference["depth"] == 1
    assert result.reference["content"] == original.content
    assert len(result.history) == 1 and result.history[0]["is_reference"] is True
    assert "predecessor" not in str(result.history)
    assert harness.captured and original.content in harness.captured[0]
    assert "https://example.invalid/predecessor" not in harness.captured[0]


@pytest.mark.parametrize("change", ["rewrite", "delete"])
def test_queued_context_revalidates_predecessor_after_fifo_restore(native_channel, change):
    backend, reader, _, talker, _, _ = native_channel
    cache = reader.caches["message/message_0.db"]
    old_url = "https://example.invalid/queued-predecessor"
    add_message(cache, talker, 1, content=f"read {old_url}", server_id=71, created=100)
    add_message(cache, talker, 2, content="please discuss the preceding message", server_id=72, created=101)
    first, second = receive_batch(native_channel)
    harness = BatchHarness(backend)
    result = harness._materialize_batch([first, second])
    assert result.attachment_status != "source_invalid"
    assert old_url in str(result.history)
    assert len(result.task.context_source_events) == 1
    predecessor_snapshot = result.task.context_source_events[0]
    assert predecessor_snapshot.source_message_id == first.source_message_id
    assert predecessor_snapshot.history == []
    assert predecessor_snapshot.task.context_source_events == []
    reply_queue = WechatReplyQueue()
    assert reply_queue.enqueue(result)
    if change == "rewrite":
        change_source(cache, talker, [1])
    else:
        with sqlite3.connect(cache.path) as connection:
            connection.execute(f'DELETE FROM "{table(talker)}" WHERE local_id=1')
        cache.changed = True
    queued = reply_queue.get()
    assert queued is not None and reply_queue.is_active(queued.token)
    # 清空可变历史后，恢复真实 FIFO 的入队快照，再校验派发中的来源检查。
    queued.event.history = []
    queued.restore_context()
    assert old_url in str(queued.event.history)

    try:
        AgentReplyCoordinator(harness).dispatch(queued.event, queued.token)
    finally:
        reply_queue.finish(queued, "skipped")

    assert harness.captured and old_url not in harness.captured[0]
    assert "please discuss the preceding message" in harness.captured[0]
    assert queued.event is second
    assert queued.source_event_ids == [first.event_id, second.event_id]
    assert queued.event.task.source_event_ids == [first.event_id, second.event_id]
    assert all(item.get("source_message_id") != first.source_message_id for item in queued.event.history)


def test_unchanged_predecessor_is_preserved_after_fifo_restore_and_dispatch(native_channel):
    backend, reader, _, talker, _, _ = native_channel
    cache = reader.caches["message/message_0.db"]
    url = "https://example.invalid/valid-predecessor"
    add_message(cache, talker, 1, content=f"read {url}", server_id=71, created=100)
    add_message(cache, talker, 2, content="continue this discussion", server_id=72, created=101)
    first, second = receive_batch(native_channel)
    harness = BatchHarness(backend)
    result = harness._materialize_batch([first, second])
    reply_queue = WechatReplyQueue()
    assert reply_queue.enqueue(result)
    queued = reply_queue.get()
    queued.restore_context()

    try:
        AgentReplyCoordinator(harness).dispatch(queued.event, queued.token)
    finally:
        reply_queue.finish(queued, "skipped")

    assert harness.captured and url in harness.captured[0]
    assert "continue this discussion" in harness.captured[0]
    assert queued.source_event_ids == [first.event_id, second.event_id]


def test_quoted_fifo_target_does_not_validate_unrelated_predecessor(native_channel, monkeypatch):
    backend, reader, _, talker, _, _ = native_channel
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="synthetic quoted original", server_id=71, created=90)
    add_message(cache, talker, 2, content="unrelated https://example.invalid/queued-unrelated", server_id=72, created=100)
    add_message(cache, talker, 3, content=reference_xml(1, "synthetic quoted", 71),
                message_type=49, server_id=73, created=101)
    original, previous, target = receive_batch(native_channel)
    harness = BatchHarness(backend)
    result = harness._materialize_batch([previous, target])
    assert result.reference and result.task.context_source_events == []
    reply_queue = WechatReplyQueue()
    assert reply_queue.enqueue(result)
    change_source(cache, talker, [2])
    validations = []
    validate = backend.validate_reply_target

    def capture_validation(event):
        validations.append(event.source_message_id)
        assert event.source_message_id != previous.source_message_id
        return validate(event)

    monkeypatch.setattr(backend, "validate_reply_target", capture_validation)
    queued = reply_queue.get()
    queued.restore_context()

    try:
        AgentReplyCoordinator(harness).dispatch(queued.event, queued.token)
    finally:
        reply_queue.finish(queued, "skipped")

    assert validations == [target.source_message_id]
    assert harness.captured and original.content in harness.captured[0]
    assert "queued-unrelated" not in harness.captured[0]
    assert queued.source_event_ids == [previous.event_id, target.event_id]
