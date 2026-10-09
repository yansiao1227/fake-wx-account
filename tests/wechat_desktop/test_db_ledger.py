"""数据库接收事务与扫描确认契约；只使用合成消息和本地 SQLite。"""

from dataclasses import replace

import pytest

from channel.wechat_desktop.db.types import SourceBatch, SourceCheckpoint, SourceRecord
from channel.wechat_desktop.models import WechatDesktopEvent, WechatDesktopMessage
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_contracts import make_channel


@pytest.fixture
def store(tmp_path):
    result = WechatDesktopStore(str(tmp_path / "source.sqlite3"))
    yield result
    result._get_connection().close()


def source_record(local_id=1, *, stream="message_0/Msg_test", phase="live", reason="", event=True):
    message = WechatDesktopEvent(
        "message", "wxid_test", "测试会话", "wxid_sender", "合成发送者",
        "text", "合成重复正文", source_type="private", native_timestamp=1700000000 + local_id,
    ) if event else None
    return SourceRecord(stream, local_id, f"{stream}/{local_id}", message, reason, phase)


def source_batch(*records, batch_id="batch-1", previous=0, baseline=0):
    grouped = {}
    for record in records:
        grouped.setdefault(record.stream_id, []).append(record.local_id)
    checkpoints = tuple(
        SourceCheckpoint(stream, max(ids), previous, baseline, "synthetic-generation")
        for stream, ids in grouped.items()
    )
    return SourceBatch("wxid_account", batch_id, tuple(records), checkpoints)


class BatchDriver:
    def __init__(self, batch):
        self.batch = batch
        self.acknowledged = []
        self.fail_ack = False

    def observe_events(self):
        return {
            "mode": "db_uia", "source_batch": self.batch,
            "db_read_account_id": self.batch.account_id,
            "db_read_healthy": True, "db_read_cache_stale": False,
            "account_binding": {"pid": 42, "version": "4.1.9.30", "verification": "page_hmac"},
        }, [row.event for row in self.batch.records if row.event is not None]

    def acknowledge_events(self, ids):
        if self.fail_ack:
            raise RuntimeError("synthetic ack lost")
        self.acknowledged.extend(ids)


def make_batch_channel(store, batch):
    driver = BatchDriver(batch)
    channel = make_channel(store, driver)
    routed = []
    channel._route_reply_event = lambda message: routed.append(message.event_id)
    return channel, driver, routed


def table_count(store, table):
    return store._connect().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_identical_content_uses_native_identity_and_shards(store):
    first, second = source_record(1), source_record(2)
    other_shard = source_record(1, stream="message_1/Msg_test")
    receipts = store.receive_source_batch(source_batch(first, second, other_shard))
    assert all(receipt.accepted for receipt in receipts)
    assert len({first.event.fingerprint(), second.event.fingerprint(), other_shard.event.fingerprint()}) == 3
    assert table_count(store, "events") == 3
    assert store.get_source_checkpoint("wxid_account", first.stream_id)["cursor"] == 2
    assert store.get_source_checkpoint("wxid_account", other_shard.stream_id)["cursor"] == 1
    renamed = replace(first.event, conversation_name="改名", sender_name="发送者改名", content="重新解析", bounds=(1, 2, 3, 4))
    assert renamed.fingerprint() == first.event.fingerprint()
    assert replace(first.event, account_id="another-account").fingerprint() != first.event.fingerprint()


def test_filtered_rows_are_committed_with_cursor_without_events(store):
    filtered = source_record(2, reason="outgoing_message", event=False)
    receipts = store.receive_source_batch(source_batch(source_record(1), filtered))
    assert len(receipts) == 1
    assert table_count(store, "source_records") == 2
    assert table_count(store, "events") == 1
    row = store._connect().execute("SELECT filter_reason FROM source_records WHERE local_id=2").fetchone()
    assert row["filter_reason"] == "outgoing_message"
    assert store.get_source_checkpoint("wxid_account", filtered.stream_id)["cursor"] == 2


def test_batch_history_is_committed_before_routing_without_uia_history_write(store, monkeypatch):
    incoming = source_record(1)
    outgoing = source_record(2, reason="outgoing_message")
    outgoing.event.direction = "outgoing"
    batch = source_batch(incoming, outgoing)
    channel, driver, _ = make_batch_channel(store, batch)
    routed = []

    def reject_duplicate_history_write(_event):
        pytest.fail("数据库历史不得在 UIA 事件路由中再次写入")

    def route_after_commit(message):
        assert len(store.list_conversation_history(incoming.event.conversation_id)) == 2
        assert store.get_source_checkpoint(batch.account_id, incoming.stream_id)["cursor"] == 2
        routed.append(message.event_id)

    monkeypatch.setattr(store, "append_event_history", reject_duplicate_history_write)
    channel._route_reply_event = route_after_commit
    channel._poll_once()
    assert routed == [incoming.event.event_id]
    assert driver.acknowledged == [batch.batch_id]
    assert store.event_state(outgoing.event.event_id)["reason"] == "outgoing_message"
    channel._poll_once()
    assert routed == [incoming.event.event_id]
    assert len(store.list_conversation_history(incoming.event.conversation_id)) == 2


def test_checkpoint_failure_rolls_back_events_sources_and_filters(store):
    store.initialize_source_checkpoint("wxid_account", "message_0/Msg_test", 0, 0)
    with store._connect() as db:
        db.execute("CREATE TRIGGER reject_cursor BEFORE UPDATE ON source_checkpoints BEGIN SELECT RAISE(ABORT, 'synthetic cursor failure'); END")
    batch = source_batch(source_record(1), source_record(2, reason="system_message", event=False))
    with pytest.raises(Exception, match="synthetic cursor failure"):
        store.receive_source_batch(batch)
    for table in ("source_records", "source_batches", "events", "event_runs", "conversation_history"):
        assert table_count(store, table) == 0
    assert store.get_source_checkpoint("wxid_account", "message_0/Msg_test")["cursor"] == 0


def test_batch_failure_does_not_route_or_ack_and_retries_whole_batch(store):
    batch = source_batch(source_record(1), source_record(2))
    channel, driver, routed = make_batch_channel(store, batch)
    with store._connect() as db:
        db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON event_runs BEGIN SELECT RAISE(ABORT, 'synthetic receipt failure'); END")
    channel._poll_once()
    assert driver.acknowledged == routed == []
    assert table_count(store, "events") == table_count(store, "source_records") == 0
    assert not store.get_source_checkpoints(batch.account_id)
    with store._connect() as db:
        db.execute("DROP TRIGGER reject_receipt")
    channel._poll_once()
    assert routed == [record.event.event_id for record in batch.records]
    assert driver.acknowledged == [batch.batch_id]


def test_lost_batch_ack_cannot_route_committed_events_twice(store):
    batch = source_batch(source_record(1), source_record(2))
    channel, driver, routed = make_batch_channel(store, batch)
    driver.fail_ack = True
    with pytest.raises(RuntimeError, match="synthetic ack lost"):
        channel._poll_once()
    assert len(routed) == 2
    driver.fail_ack = False
    channel._poll_once()
    assert len(routed) == table_count(store, "events") == 2
    assert driver.acknowledged == [batch.batch_id]
    assert all(store.event_state(event_id)["state"] == "received" for event_id in routed)


def test_duplicate_batch_returns_canonical_identity_and_never_replays(store):
    batch = source_batch(source_record(1))
    first = store.receive_source_batch(batch)[0]
    repeated_record = replace(batch.records[0], event=replace(batch.records[0].event, event_id="fresh-observation"))
    repeated = replace(batch, records=(repeated_record,))
    duplicate = store.receive_source_batch(repeated)[0]
    assert first.accepted and not duplicate.accepted
    assert duplicate.canonical_event_id == first.canonical_event_id
    assert duplicate.observed_event_id == "fresh-observation"


def test_stream_checkpoint_cannot_skip_failed_predecessor(store):
    store.initialize_source_checkpoint("wxid_account", "message_0/Msg_test", 0, 9)
    batch = source_batch(source_record(3), previous=2)
    with pytest.raises(ValueError, match="cursor changed"):
        store.receive_source_batch(batch)
    assert store.get_source_checkpoint(batch.account_id, batch.records[0].stream_id)["cursor"] == 0
    assert table_count(store, "events") == 0


def test_initial_baseline_survives_pagination_and_restart(store):
    stream = "message_0/Msg_test"
    store.initialize_source_checkpoint("wxid_account", stream, 0, 10, "first")
    first = source_batch(source_record(1, phase="baseline"), baseline=10)
    channel, driver, routed = make_batch_channel(store, first)
    channel._poll_once()
    second = source_batch(source_record(2, phase="baseline"), previous=1, baseline=999, batch_id="page-2")
    driver.batch = second
    channel._poll_once()
    assert routed == []
    assert store.get_source_checkpoint("wxid_account", stream)["baseline_high_water"] == 10
    initialized = store.initialize_source_checkpoint("wxid_account", stream, 999, 1000, "restart")
    assert initialized["cursor"] == 2 and initialized["baseline_high_water"] == 10
    assert len(store.list_conversation_history("wxid_test")) == 2


@pytest.mark.parametrize("phase,expected_routes", [("live", 1), ("startup_unread", 1), ("baseline", 0), ("offline_backfill", 0)])
def test_source_phase_is_independent_of_first_poll_and_bootstrap_setting(store, phase, expected_routes):
    batch = source_batch(source_record(1, phase=phase))
    channel, driver, routed = make_batch_channel(store, batch)
    channel.config["bootstrap_existing_messages"] = False
    channel._poll_once()
    assert len(routed) == expected_routes
    assert driver.acknowledged == [batch.batch_id]
    assert len(store.list_conversation_history("wxid_test")) == 1
    if not expected_routes:
        assert store.event_state(batch.records[0].event.event_id)["reason"] == phase


def test_startup_unread_flag_and_outgoing_never_route(store):
    unread = source_record(1, phase="startup_unread")
    outgoing = source_record(2)
    outgoing.event.direction = "outgoing"
    batch = source_batch(unread, outgoing)
    channel, _, routed = make_batch_channel(store, batch)
    channel.config["process_startup_unread_messages"] = False
    channel._poll_once()
    assert routed == []
    assert store.event_state(unread.event.event_id)["reason"] == "startup_unread_disabled"
    assert store.event_state(outgoing.event.event_id)["reason"] == "source_not_incoming"


def test_restart_preserves_committed_cursor_without_replaying_received_event(store):
    batch = source_batch(source_record(1))
    store.receive_source_batch(batch)
    assert len(store.list_conversation_history("wxid_test")) == 1
    assert store.recover_interrupted_events()["interrupted"] == 1
    channel, driver, routed = make_batch_channel(store, batch)
    channel._poll_once()
    assert routed == [] and driver.acknowledged == [batch.batch_id]
    assert store.get_source_checkpoint(batch.account_id, batch.records[0].stream_id)["cursor"] == 1
    assert store.event_state(batch.records[0].event.event_id)["reason"] == "restart_no_replay"
    assert len(store.list_conversation_history("wxid_test")) == 1


def test_native_time_metadata_and_db_health_are_preserved(store):
    batch = source_batch(source_record(1))
    channel, _, _ = make_batch_channel(store, batch)
    channel._poll_once()
    event = batch.records[0].event
    row = store._connect().execute("SELECT * FROM events WHERE event_id=?", (event.event_id,)).fetchone()
    assert row["account_id"] == batch.account_id
    assert row["source_message_id"] == batch.records[0].source_message_id
    assert row["native_timestamp"] == event.native_timestamp
    assert store.list_conversation_history(event.conversation_id)[0]["created_at"] == event.native_timestamp
    assert WechatDesktopMessage(event).create_time == event.native_timestamp
    status = channel._service.status()
    assert status["mode"] == "db_uia" and status["db_read_healthy"]
    assert status["db_read_account_id"] == batch.account_id
    assert status["account_binding"] == {"pid": 42, "version": "4.1.9.30", "verification": "page_hmac"}


def test_empty_filtered_batch_is_acknowledged_without_reply(store):
    batch = source_batch(source_record(1, event=False, reason="system_message"))
    channel, driver, routed = make_batch_channel(store, batch)
    channel._poll_once()
    assert routed == [] and driver.acknowledged == [batch.batch_id]
    assert table_count(store, "events") == 0 and table_count(store, "source_records") == 1


def test_account_mismatch_and_reused_batch_id_are_rejected(store):
    record = source_record(1)
    record.event.account_id = "different-account"
    with pytest.raises(ValueError, match="identity differs"):
        store.receive_source_batch(source_batch(record))
    batch = source_batch(source_record(1))
    store.receive_source_batch(batch)
    changed = replace(batch, records=(replace(batch.records[0], filter_reason="now-filtered"),))
    with pytest.raises(ValueError, match="batch id was reused"):
        store.receive_source_batch(changed)
    assert table_count(store, "events") == 1


def test_different_source_rows_cannot_share_an_event_id(store):
    first, second = source_record(1), source_record(2)
    second.event.event_id = first.event.event_id
    with pytest.raises(ValueError, match="event id collides"):
        store.receive_source_batch(source_batch(first, second))
    assert table_count(store, "events") == table_count(store, "source_records") == 0


def test_source_schema_upgrade_is_repeatable_and_keeps_legacy_history(store):
    legacy = WechatDesktopEvent("message", "legacy", "合成旧会话", "sender", "合成发送者", "text", "合成旧正文")
    assert store.record_event(legacy)
    store.append_event_history(legacy)
    store.initialize_source_checkpoint("wxid_account", "synthetic-stream", 20, 20)
    store._init_schema()
    store._init_schema()
    row = store._connect().execute("SELECT * FROM events WHERE event_id=?", (legacy.event_id,)).fetchone()
    assert row["content"] == legacy.content and row["source_message_id"] == ""
    assert store.list_conversation_history("legacy")[0]["source_event_id"] == legacy.event_id
    assert store.get_source_checkpoint("wxid_account", "synthetic-stream")["cursor"] == 20
