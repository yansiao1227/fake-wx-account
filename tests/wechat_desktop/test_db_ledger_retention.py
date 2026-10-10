"""来源账本保留期与永久游标去重，仅使用合成 SQLite 数据。"""

from dataclasses import replace

import pytest

from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_db_ledger import source_batch, source_record, store, table_count


def aged_batch(store, monkeypatch, *, state="completed", event=True):
    record = source_record(event=event, reason="" if event else "system_message")
    if event:
        record.event.observed_at = 1
    batch = source_batch(record)
    with monkeypatch.context() as patch:
        patch.setattr("channel.wechat_desktop.storage.store.time.time", lambda: 1)
        store.receive_source_batch(batch)
        if event and state != "received":
            store.set_event_state([record.event.event_id], state)
    return batch


def test_cleanup_removes_committed_old_source_ledger_without_replaying_after_restart(store, monkeypatch):
    batch = aged_batch(store, monkeypatch)
    checkpoint = store.get_source_checkpoint(batch.account_id, batch.records[0].stream_id)
    store.cleanup()
    assert table_count(store, "source_records") == table_count(store, "source_batches") == 0
    assert table_count(store, "events") == 0
    assert store.get_source_checkpoint(batch.account_id, batch.records[0].stream_id) == checkpoint
    reopened = WechatDesktopStore(store.path)
    try:
        for replay in (batch, replace(batch, batch_id="new-observation")):
            receipt = reopened.receive_source_batch(replay)[0]
            assert not receipt.accepted
        assert table_count(reopened, "events") == table_count(reopened, "source_records") == 0
        assert reopened.get_source_checkpoint(batch.account_id, batch.records[0].stream_id) == checkpoint
        next_batch = source_batch(source_record(2), previous=1, batch_id="next-live")
        assert reopened.receive_source_batch(next_batch)[0].accepted
        assert reopened.get_source_checkpoint(batch.account_id, batch.records[0].stream_id)["cursor"] == 2
    finally:
        reopened._get_connection().close()


@pytest.mark.parametrize("state", ["received", "queued", "running", "sending"])
def test_cleanup_preserves_pending_source_receipts_and_event_payload(store, monkeypatch, state):
    batch = aged_batch(store, monkeypatch, state=state)
    store.cleanup()
    assert table_count(store, "source_records") == table_count(store, "source_batches") == 1
    assert table_count(store, "events") == 1
    assert store.event_state(batch.records[0].event.event_id)["state"] == state
    assert not store.receive_source_batch(batch)[0].accepted


@pytest.mark.parametrize("checkpoint_change", ["missing", "behind", "generation"])
def test_cleanup_preserves_source_ledger_not_covered_by_checkpoint(store, monkeypatch, checkpoint_change):
    aged_batch(store, monkeypatch)
    with store._connect() as db:
        if checkpoint_change == "missing":
            db.execute("DELETE FROM source_checkpoints")
        elif checkpoint_change == "behind":
            db.execute("UPDATE source_checkpoints SET cursor=0")
        else:
            db.execute("UPDATE source_checkpoints SET generation='replacement'")
    store.cleanup()
    assert table_count(store, "source_records") == table_count(store, "source_batches") == 1


def test_cleanup_removes_old_filtered_source_rows_but_keeps_recent_receipts(store, monkeypatch):
    aged_batch(store, monkeypatch, event=False)
    recent = source_batch(source_record(2, event=False, reason="system_message"), previous=1, batch_id="recent")
    store.receive_source_batch(recent)
    store.cleanup()
    assert table_count(store, "source_records") == table_count(store, "source_batches") == 1
    row = store._connect().execute("SELECT local_id FROM source_records").fetchone()
    assert row["local_id"] == 2
    assert store.receive_source_batch(recent) == ()


@pytest.mark.parametrize("state,remaining", [("completed", 0), ("received", 1)])
def test_cleanup_handles_legacy_batch_without_checkpoint_metadata(store, monkeypatch, state, remaining):
    aged_batch(store, monkeypatch, state=state)
    with store._connect() as db:
        db.execute("UPDATE source_batches SET checkpoint_ranges=''")
    store.cleanup()
    assert table_count(store, "source_batches") == table_count(store, "source_records") == remaining


def test_cleanup_does_not_allow_mixed_stale_and_new_batch_to_skip_predecessor(store, monkeypatch):
    aged_batch(store, monkeypatch)
    store.cleanup()
    batch = source_batch(source_record(1), source_record(2), batch_id="overlapping-scan")
    with pytest.raises(ValueError, match="cursor changed"):
        store.receive_source_batch(batch)
    assert table_count(store, "events") == table_count(store, "source_records") == 0


def test_cleanup_waits_for_every_stream_and_pending_record_in_a_batch(store, monkeypatch):
    first, other = source_record(), source_record(stream="message_1/Msg_test")
    first.event.observed_at = other.event.observed_at = 1
    batch = source_batch(first, other)
    with monkeypatch.context() as patch:
        patch.setattr("channel.wechat_desktop.storage.store.time.time", lambda: 1)
        store.receive_source_batch(batch)
        store.set_event_state([first.event.event_id], "completed")
    store.cleanup()
    assert table_count(store, "source_batches") == 1
    assert table_count(store, "source_records") == table_count(store, "events") == 2
    store.set_event_state([other.event.event_id], "completed")
    with store._connect() as db:
        db.execute("UPDATE source_checkpoints SET cursor=0 WHERE stream_id=?", (other.stream_id,))
    store.cleanup()
    assert table_count(store, "source_batches") == 1
    assert table_count(store, "source_records") == 2
    with store._connect() as db:
        db.execute("UPDATE source_checkpoints SET cursor=1 WHERE stream_id=?", (other.stream_id,))
    store.cleanup()
    assert table_count(store, "source_batches") == table_count(store, "source_records") == 0


def test_schema_upgrade_adds_retention_metadata_to_legacy_source_tables(tmp_path):
    import sqlite3

    path = str(tmp_path / "legacy-source.sqlite3")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE source_records (account_id TEXT,stream_id TEXT,local_id INTEGER,"
                   "source_message_id TEXT,event_id TEXT,filter_reason TEXT,receipt_phase TEXT,received_at REAL,"
                   "PRIMARY KEY(account_id,stream_id,local_id))")
        db.execute("CREATE TABLE source_batches (account_id TEXT,batch_id TEXT,source_signature TEXT,received_at REAL,"
                   "PRIMARY KEY(account_id,batch_id))")
        db.execute("INSERT INTO source_records VALUES ('account','stream',1,'message','','system','live',1)")
        db.execute("INSERT INTO source_batches VALUES ('account','legacy','signature',1)")
    ledger = WechatDesktopStore(path)
    try:
        ledger._init_schema()
        ledger.initialize_source_checkpoint("account", "stream", 1, 1)
        ledger.cleanup()
        assert table_count(ledger, "source_records") == table_count(ledger, "source_batches") == 0
        assert ledger.get_source_checkpoint("account", "stream")["cursor"] == 1
    finally:
        ledger._get_connection().close()


@pytest.mark.parametrize("ranges", ["not-json", "{}", "[{}]", '["invalid-range"]',
                                   '[{"stream_id":"message_0/Msg_test","cursor":1}]'])
def test_cleanup_keeps_batches_with_invalid_checkpoint_metadata(store, monkeypatch, ranges):
    aged_batch(store, monkeypatch)
    with store._connect() as db:
        db.execute("UPDATE source_batches SET checkpoint_ranges=?", (ranges,))
    store.cleanup()
    assert table_count(store, "source_batches") == table_count(store, "source_records") == 1


def test_cleanup_keeps_orphan_legacy_batch_without_proof_of_checkpoint_coverage(store):
    store.initialize_source_checkpoint("wxid_account", "other-stream", 100, 100)
    with store._connect() as db:
        db.execute("INSERT INTO source_batches(account_id,batch_id,source_signature,received_at) "
                   "VALUES ('wxid_account','unknown','signature',1)")
    store.cleanup()
    assert table_count(store, "source_batches") == 1
