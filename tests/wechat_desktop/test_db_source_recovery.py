"""重启补账与快照清理故障回归；仅使用合成消息和可控清理替身。"""

from pathlib import Path

import pytest

from channel.wechat_desktop.db.cache import EncryptedDatabaseCache
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.reader import WechatDatabaseReader
from channel.wechat_desktop.db.source import WechatDatabaseSource
from channel.wechat_desktop.db import source as source_module
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_contracts import make_channel
from .test_db_reader import add_message, add_session, make_reader


@pytest.fixture
def store(tmp_path):
    ledger = WechatDesktopStore(str(tmp_path / "recovery.sqlite3"))
    yield ledger
    ledger._get_connection().close()


@pytest.mark.parametrize("startup_unread", [False, True])
def test_restart_new_shard_backfills_to_history_without_reply(tmp_path, store, startup_unread):
    all_reader, talker = make_reader(tmp_path, shards=2)
    caches = all_reader.caches
    initial_reader = WechatDatabaseReader({}, binding=all_reader.binding,
        caches={name: cache for name, cache in caches.items() if name != "message/message_1.db"})
    original = WechatDatabaseSource({}, reader=initial_reader, checkpoint_store=store)
    assert original.observe_events()[1] == []
    add_message(caches["message/message_0.db"], talker, 1, sort_seq=100)
    observation, first = original.observe_events()
    assert store.receive_source_batch(observation["source_batch"])[0].accepted
    original.acknowledge_events([observation["source_batch"].batch_id])
    original.close()
    assert store.get_source_checkpoint(initial_reader.account_id, first[0].source_stream_id)["cursor"] == 1

    # 停机期间原流增长，同时创建未有 checkpoint 的新分片。
    add_message(caches["message/message_0.db"], talker, 2, sort_seq=150)
    add_message(caches["message/message_1.db"], talker, 1, server_id=201, sort_seq=201)
    add_message(caches["message/message_1.db"], talker, 2, server_id=202, sort_seq=202)
    restarted_reader = WechatDatabaseReader({}, binding=all_reader.binding, caches=caches)
    if startup_unread:
        add_session(restarted_reader, tmp_path, talker, count=2, first_server_id=201)
    restarted = WechatDatabaseBackend({"process_startup_unread_messages": startup_unread},
        db_reader=restarted_reader, store=store)
    observation, events = restarted.observe_events()
    new_stream = "message/message_1.db:" + first[0].source_stream_id.split(":", 1)[1]
    assert {(event.source_stream_id, event.source_local_id) for event in events} == {
        (first[0].source_stream_id, 2), (new_stream, 1), (new_stream, 2)}
    assert all(event.receipt_phase == "offline_backfill" for event in events)

    channel = make_channel(store, restarted)
    routed = []
    channel._route_reply_event = lambda event: routed.append(event.event_id)
    channel._poll_once()
    assert routed == []
    assert restarted.source.pending_batch is None
    assert store.get_source_checkpoint(initial_reader.account_id, new_stream)["cursor"] == 2
    assert store.get_source_checkpoint(initial_reader.account_id, new_stream)["baseline_high_water"] == 0
    history = store.list_conversation_history(first[0].conversation_id)
    assert len(history) == 4
    assert all(store.event_state(event.event_id)["reason"] == "offline_backfill" for event in events)
    assert restarted.observe_events()[1] == []

    # 高水位必须固定；补账后的原流和新分片消息均重新进入 live。
    add_message(caches["message/message_0.db"], talker, 3, sort_seq=203)
    add_message(caches["message/message_1.db"], talker, 3, sort_seq=204)
    assert [event.receipt_phase for event in restarted.observe_events()[1]] == ["live", "live"]


def test_reader_close_continues_after_failure_and_retries_only_failed_cache(tmp_path, monkeypatch):
    reader, _ = make_reader(tmp_path)
    calls = []
    failing_name = next(iter(reader.caches))
    locked = True

    def close_cache(name):
        calls.append(name)
        if name == failing_name and locked:
            raise PermissionError("synthetic-sensitive-path")

    for name, cache in reader.caches.items():
        monkeypatch.setattr(cache, "close", lambda name=name: close_cache(name), raising=False)
    with pytest.raises(DatabaseReadError) as error:
        reader.close()
    assert error.value.code == "snapshot_cleanup_failed"
    assert "synthetic-sensitive-path" not in str(error.value)
    assert calls == list(reader.caches)
    assert reader.status()["db_cleanup_error_code"] == "snapshot_cleanup_failed"

    calls.clear()
    locked = False
    reader.close()
    assert calls == [failing_name]
    assert reader.status()["db_cleanup_error_code"] == ""
    reader.close()
    assert calls == [failing_name]


@pytest.mark.parametrize("retry_action", ["close", "resume"])
def test_source_close_exposes_cleanup_failure_and_keeps_reader_for_retry(tmp_path, monkeypatch, retry_action):
    reader, _ = make_reader(tmp_path)
    calls = []
    warnings = []
    monkeypatch.setattr(source_module.logger, "warning", lambda *args: warnings.append(args))

    def close_reader():
        calls.append("close")
        if len(calls) == 1:
            raise PermissionError("synthetic-sensitive-path")

    monkeypatch.setattr(reader, "close", close_reader)
    source = WechatDatabaseSource({}, reader=reader)
    source.close()
    status = source.status()
    assert status["db_read_error_code"] == "source_closed"
    assert status["db_cleanup_error_code"] == "snapshot_cleanup_failed"
    assert "synthetic-sensitive-path" not in repr(status)
    assert len(warnings) == 1
    assert "snapshot_cleanup_failed" in warnings[0][0]
    assert "synthetic-sensitive-path" not in repr(warnings)
    getattr(source, retry_action)()
    assert calls == ["close", "close"]
    assert source.status()["db_cleanup_error_code"] == ""


def test_source_cleanup_unlink_failure_preserves_only_failed_snapshot(tmp_path, monkeypatch):
    reader, _ = make_reader(tmp_path)
    locked = EncryptedDatabaseCache(tmp_path / "locked-source.db", bytes(range(32)),
                                    tmp_path / "private" / "locked.db")
    removable = EncryptedDatabaseCache(tmp_path / "free-source.db", bytes(range(32)),
                                       tmp_path / "private" / "free.db")
    locked.path.write_bytes(b"synthetic-private-snapshot")
    removable.path.write_bytes(b"synthetic-private-snapshot")
    reader.caches = {"contact/contact.db": locked, "message/message_0.db": removable}
    unlink = Path.unlink
    blocked = True

    def controlled_unlink(path, *args, **kwargs):
        if path == locked.path and blocked:
            raise PermissionError("synthetic-sensitive-path")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", controlled_unlink)
    source = WechatDatabaseSource({}, reader=reader)
    source.close()
    assert locked.path.exists() and not removable.path.exists()
    assert locked.status.error_code == "snapshot_cleanup_failed"
    assert source.status()["db_cleanup_error_code"] == "snapshot_cleanup_failed"
    assert "synthetic-sensitive-path" not in repr(source.status())
    blocked = False
    source.close()
    assert not locked.path.exists()
    assert source.status()["db_cleanup_error_code"] == ""


def test_relogin_cleanup_failure_remains_visible_and_retryable(tmp_path, store, monkeypatch):
    reader, _ = make_reader(tmp_path)
    calls = []

    def fail_refresh():
        raise DatabaseReadError("login_process_changed", "synthetic process changed")

    def close_reader():
        calls.append("close")
        if len(calls) == 1:
            raise PermissionError("synthetic-sensitive-path")

    monkeypatch.setattr(reader, "refresh", fail_refresh)
    monkeypatch.setattr(reader, "close", close_reader)
    source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    observation, events = source.observe_events()
    assert events == []
    assert observation["db_read_error_code"] == "login_process_changed"
    assert observation["db_cleanup_error_code"] == "snapshot_cleanup_failed"
    assert "synthetic-sensitive-path" not in repr(observation)
    source.close()
    assert calls == ["close", "close"]
    assert source.status()["db_cleanup_error_code"] == ""
