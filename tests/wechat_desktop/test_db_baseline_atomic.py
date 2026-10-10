"""账号首次基线原子性回归；仅使用合成分片和临时账本。"""

import os
import sqlite3
import subprocess
import sys

import pytest

from channel.wechat_desktop.db.reader import WechatDatabaseReader
from channel.wechat_desktop.db.source import WechatDatabaseSource
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_db_reader import add_message, add_session, make_reader


@pytest.fixture
def store(tmp_path):
    ledger = WechatDesktopStore(str(tmp_path / "atomic.sqlite3"))
    yield ledger
    ledger._get_connection().close()


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("failure_stage", ["second_stream", "account_marker"])
def test_first_baseline_failure_leaves_no_partial_account(tmp_path, store, restart, failure_stage):
    reader, talker = make_reader(tmp_path, shards=2)
    for cache_name in ("message/message_0.db", "message/message_1.db"):
        add_message(reader.caches[cache_name], talker, 1)
    with store._connect() as db:
        if failure_stage == "second_stream":
            db.execute("CREATE TRIGGER reject_baseline BEFORE INSERT ON source_checkpoints "
                       "WHEN NEW.stream_id LIKE 'message/message_1.db:%' "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic initialization failure'); END")
        else:
            db.execute("CREATE TRIGGER reject_baseline BEFORE INSERT ON source_accounts "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic initialization failure'); END")
    source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    status, events = source.observe_events()
    assert status["db_read_error_code"] == "database_read_failed"
    assert events == []
    assert store.get_source_checkpoints(reader.account_id) == {}
    assert not store.source_account_initialized(reader.account_id)
    with store._connect() as db:
        db.execute("DROP TRIGGER reject_baseline")
    # 失败之后新到达的消息也属于重试时的首次基线。
    for cache_name in ("message/message_0.db", "message/message_1.db"):
        add_message(reader.caches[cache_name], talker, 2)
    if restart:
        reader = WechatDatabaseReader({}, binding=reader.binding, caches=reader.caches)
        source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    assert source.observe_events()[1] == []
    assert store.source_account_initialized(reader.account_id)
    checkpoints = store.get_source_checkpoints(reader.account_id)
    assert len(checkpoints) == 2
    assert all(cp["cursor"] == cp["baseline_high_water"] == 2 for cp in checkpoints.values())
    add_message(reader.caches["message/message_1.db"], talker, 3)
    assert [event.receipt_phase for event in source.observe_events()[1]] == ["live"]


def test_process_crash_during_baseline_rolls_back_every_stream(tmp_path):
    path = tmp_path / "crashed.sqlite3"
    script = """
import os
import sys
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.db.source import WechatDatabaseSource
class Reader:
    account_id = 'synthetic-account'
    def refresh(self): pass
    def get_highwaters(self):
        return {'stream-0': {'cursor': 4}, 'stream-1': {'cursor': 7}}
    def status(self): return {}
    def poll_batch(self, checkpoints, boot_highwaters): return None
store = WechatDesktopStore(sys.argv[1])
db = store._connect()
db.create_function('crash_now', 0, lambda: os._exit(17))
with db:
    db.execute("CREATE TRIGGER crash_baseline BEFORE INSERT ON source_checkpoints "
               "WHEN NEW.stream_id='stream-1' BEGIN SELECT crash_now(); END")
WechatDatabaseSource({}, reader=Reader(), checkpoint_store=store).observe_events()
"""
    process = subprocess.run([sys.executable, "-c", script, str(path)],
                             cwd=os.getcwd(), capture_output=True, timeout=30)
    assert process.returncode == 17, process.stderr.decode(errors="replace")
    ledger = WechatDesktopStore(str(path))
    try:
        assert ledger.get_source_checkpoints("synthetic-account") == {}
        assert not ledger.source_account_initialized("synthetic-account")
        with ledger._connect() as db:
            db.execute("DROP TRIGGER crash_baseline")
        checkpoints = ledger.initialize_source_account("synthetic-account", {
            "stream-0": {"cursor": 5}, "stream-1": {"cursor": 8}})
        assert {stream: cp["cursor"] for stream, cp in checkpoints.items()} == {
            "stream-0": 5, "stream-1": 8}
    finally:
        ledger._get_connection().close()


@pytest.mark.parametrize("startup_unread", [False, True])
def test_first_account_success_keeps_startup_unread_semantics(tmp_path, store, startup_unread):
    reader, talker = make_reader(tmp_path, shards=2)
    add_message(reader.caches["message/message_0.db"], talker, 1, server_id=122, sort_seq=99)
    add_message(reader.caches["message/message_0.db"], talker, 2, server_id=123, sort_seq=100)
    add_message(reader.caches["message/message_1.db"], talker, 1, server_id=200, sort_seq=98)
    add_session(reader, tmp_path, talker)
    source = WechatDatabaseSource({"process_startup_unread_messages": startup_unread},
                                 reader=reader, checkpoint_store=store)
    status, events = source.observe_events()
    assert store.source_account_initialized(reader.account_id)
    assert len(store.get_source_checkpoints(reader.account_id)) == 2
    assert [(event.source_local_id, event.receipt_phase) for event in events] == (
        [(2, "startup_unread")] if startup_unread else [])
    if events:
        store.receive_source_batch(status["source_batch"])
        source.acknowledge_events([status["source_batch"].batch_id])
    add_message(reader.caches["message/message_1.db"], talker, 2, server_id=201, sort_seq=101)
    assert [event.receipt_phase for event in source.observe_events()[1]] == ["live"]


def test_empty_successful_account_remembers_baseline_and_backfills_after_restart(store):
    class Reader:
        account_id = "synthetic-account"
        highwaters = {}

        def refresh(self):
            pass

        def get_highwaters(self):
            return self.highwaters

        def status(self):
            return {}

        def poll_batch(self, checkpoints, boot_highwaters):
            self.checkpoints = checkpoints
            self.boot_highwaters = boot_highwaters
            return None

    reader = Reader()
    assert WechatDatabaseSource({}, reader=reader, checkpoint_store=store).observe_events()[1] == []
    assert store.source_account_initialized(reader.account_id)
    assert store.get_source_checkpoints(reader.account_id) == {}
    reader.highwaters = {"new-stream": {"cursor": 3, "generation": "g"}}
    WechatDatabaseSource({}, reader=reader, checkpoint_store=store).observe_events()
    assert reader.checkpoints["new-stream"]["cursor"] == 0
    assert reader.checkpoints["new-stream"]["baseline_high_water"] == 3
    assert reader.boot_highwaters == reader.highwaters


def test_legacy_checkpoint_migration_preserves_history_and_backfills_new_stream(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE source_checkpoints (account_id TEXT NOT NULL, stream_id TEXT NOT NULL, "
                   "cursor INTEGER NOT NULL, baseline_high_water INTEGER NOT NULL, generation TEXT NOT NULL, "
                   "updated_at REAL NOT NULL, PRIMARY KEY(account_id, stream_id))")
        db.execute("INSERT INTO source_checkpoints VALUES ('legacy-account', 'old-stream', 7, 5, 'g', 1)")
    ledger = WechatDesktopStore(str(path))
    try:
        ledger.append_conversation_history("conversation", "Synthetic", "sender", "incoming", "text", "history")
        assert ledger.source_account_initialized("legacy-account")
        checkpoints = ledger.initialize_source_account("legacy-account", {
            "old-stream": {"cursor": 10, "generation": "g"},
            "new-stream": {"cursor": 4, "generation": "h"}})
        assert checkpoints["old-stream"]["cursor"] == 7
        assert checkpoints["old-stream"]["baseline_high_water"] == 5
        assert checkpoints["new-stream"]["cursor"] == 0
        assert checkpoints["new-stream"]["baseline_high_water"] == 4
        assert ledger.list_conversation_history("conversation")[0]["content"] == "history"
    finally:
        ledger._get_connection().close()


def test_invalid_empty_account_cannot_complete_baseline(store):
    with pytest.raises(ValueError, match="account"):
        store.initialize_source_account("", {})
    assert not store.source_account_initialized("")


def test_zero_highwater_completes_first_baseline(store):
    checkpoints = store.initialize_source_account("synthetic-account", {"stream": {"cursor": 0}})
    assert checkpoints["stream"]["cursor"] == checkpoints["stream"]["baseline_high_water"] == 0
    assert store.source_account_initialized("synthetic-account")


def test_failed_baseline_recomputes_verified_unread_boundary(tmp_path, store):
    reader, talker = make_reader(tmp_path, shards=2)
    add_message(reader.caches["message/message_0.db"], talker, 1, server_id=122, sort_seq=99)
    add_message(reader.caches["message/message_0.db"], talker, 2, server_id=123, sort_seq=100)
    add_session(reader, tmp_path, talker)
    with store._connect() as db:
        db.execute("CREATE TRIGGER reject_baseline BEFORE INSERT ON source_accounts "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic initialization failure'); END")
    source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    assert source.observe_events()[1] == []
    assert store.get_source_checkpoints(reader.account_id) == {}
    with store._connect() as db:
        db.execute("DROP TRIGGER reject_baseline")
    add_message(reader.caches["message/message_1.db"], talker, 1, server_id=124, sort_seq=101)
    session = reader.caches["session/session.db"]
    with sqlite3.connect(session.path) as db:
        db.execute("UPDATE SessionTable SET unread_count=2")
    session.changed = True
    status, events = source.observe_events()
    assert len(events) == 2
    assert all(event.receipt_phase == "startup_unread" for event in events)
    assert store.source_account_initialized(reader.account_id)
    assert len(status["source_batch"].checkpoints) == 2


@pytest.mark.parametrize("completed", [False, True])
def test_explicit_resume_uses_persistent_account_lifecycle_without_replaying_unread(
        tmp_path, store, monkeypatch, completed):
    reader, talker = make_reader(tmp_path, shards=2)
    add_message(reader.caches["message/message_0.db"], talker, 1, server_id=122, sort_seq=99)
    add_message(reader.caches["message/message_0.db"], talker, 2, server_id=123, sort_seq=100)
    add_session(reader, tmp_path, talker)
    if not completed:
        with store._connect() as db:
            db.execute("CREATE TRIGGER reject_baseline BEFORE INSERT ON source_accounts "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic initialization failure'); END")
    source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    first_events = source.observe_events()[1]
    assert bool(first_events) == completed
    monkeypatch.setattr(reader, "close", lambda: None)
    source.close()
    if not completed:
        with store._connect() as db:
            db.execute("DROP TRIGGER reject_baseline")
    replacement = WechatDatabaseReader({}, binding=reader.binding, caches=reader.caches)
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", lambda config: replacement)
    add_message(reader.caches["message/message_1.db"], talker, 1, server_id=124, sort_seq=101)
    source.resume()
    status, events = source.observe_events()
    assert store.source_account_initialized(reader.account_id)
    if completed:
        assert len(events) == 2
        assert all(event.receipt_phase == "offline_backfill" for event in events)
        store.receive_source_batch(status["source_batch"])
        source.acknowledge_events([status["source_batch"].batch_id])
    else:
        assert events == []
        assert all(cp["cursor"] == cp["baseline_high_water"]
                   for cp in store.get_source_checkpoints(reader.account_id).values())
    add_message(reader.caches["message/message_1.db"], talker, 2, server_id=125, sort_seq=102)
    assert [event.receipt_phase for event in source.observe_events()[1]] == ["live"]
