"""PR #3 的跨批来源保存与 Windows 证据持久化回归。"""

import hashlib
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.materialize import WechatDesktopMaterializeMixin
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_db_reader import add_message, table
from .test_materialize_source_validation import BatchHarness, receive_batch
from .test_reference_pipeline_contract import native_channel


@pytest.mark.parametrize("change", ["delete", "rewrite", "unchanged"])
def test_previous_batch_history_is_revalidated_after_fifo_wait(native_channel, change):
    backend, reader, _, talker, _, _ = native_channel
    cache = reader.caches["message/message_0.db"]
    old_url = "https://example.invalid/previous-batch"
    add_message(cache, talker, 1, content=old_url, server_id=71, created=100)
    first = receive_batch(native_channel)[0]
    add_message(cache, talker, 2, content="continue", server_id=72, created=101)
    second = receive_batch(native_channel)[0]
    assert second.source_local_id == 2
    harness = BatchHarness(backend)
    result = harness._materialize_batch([second])
    assert result.task.source_event_ids == [second.event_id]
    assert len(result.task.context_source_events) == 1
    snapshot = result.task.context_source_events[0]
    assert snapshot.source_message_id == first.source_message_id
    assert snapshot.content_signature == first.content_signature
    assert snapshot.history == [] and snapshot.task.context_source_events == []
    queue = WechatReplyQueue()
    assert queue.enqueue(result)
    if change != "unchanged":
        with sqlite3.connect(cache.path) as conn:
            if change == "delete":
                conn.execute(f'DELETE FROM "{table(talker)}" WHERE local_id=1')
            else:
                conn.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=1',
                             ("changed",))
        cache.changed = True
    item = queue.get()
    item.restore_context()
    try:
        AgentReplyCoordinator(harness).dispatch(item.event, item.token)
    finally:
        queue.finish(item, "skipped")
    assert harness.captured
    assert (old_url in harness.captured[0]) == (change == "unchanged")
    assert len(item.event.task.context_source_events) == (1 if change == "unchanged" else 0)


def test_discarded_attachment_history_does_not_leave_context_validation_sources(native_channel):
    backend, reader, _, talker, _, _ = native_channel
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="[image]", message_type=3, server_id=71, created=100)
    receive_batch(native_channel)
    add_message(cache, talker, 2, content="hello", server_id=72, created=101)
    second = receive_batch(native_channel)[0]
    assert second.task.context_source_events
    result = BatchHarness(backend)._materialize_batch([second])
    assert result.history == []
    assert result.task.context_source_events == []


def test_database_event_id_uses_safe_filename_and_survives_temporary_cleanup(tmp_path):
    source = tmp_path / "temporary.png"
    source.write_bytes(b"synthetic evidence")
    store = WechatDesktopStore(str(tmp_path / "ledger.sqlite3"))
    event = WechatDesktopEvent(
        "message", "conversation", "Synthetic", "sender", "Synthetic", "image", str(source),
        event_id="db-message:" + "a" * 64, evidence_path=str(source),
        reference={"content_type": "image", "file_path": str(source)},
    )
    try:
        store.record_event(event)
        WechatDesktopMaterializeMixin._preserve_event_evidence(SimpleNamespace(_store=store), event)
        managed = Path(event.evidence_path)
        assert managed.parent == store.evidence_dir
        assert not re.search(r'[<>:"/\\|?*]', managed.name)
        assert managed.name == hashlib.sha256(event.event_id.encode()).hexdigest() + ".png"
        assert event.content == str(managed)
        assert event.reference["file_path"] == str(managed)
        source.unlink()
        assert managed.read_bytes() == b"synthetic evidence"
    finally:
        store._get_connection().close()
    reopened = WechatDesktopStore(str(tmp_path / "ledger.sqlite3"))
    try:
        saved = reopened._get_connection().execute(
            "SELECT managed_evidence_path FROM events WHERE event_id=?", (event.event_id,)
        ).fetchone()
        assert saved and Path(saved[0]).read_bytes() == b"synthetic evidence"
    finally:
        reopened._get_connection().close()
