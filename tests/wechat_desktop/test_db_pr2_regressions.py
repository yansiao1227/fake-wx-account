"""PR #2 数据库审查回归；仅使用临时合成账号、快照和接收账本。"""

from dataclasses import replace

import pytest

from channel.wechat_desktop.db.discovery import DatabaseCatalog
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.reader import WechatDatabaseReader
from channel.wechat_desktop.db.source import WechatDatabaseSource
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_db_discovery_auth import OTHER_KEY, _catalog, _contact
from .test_db_reader import add_message, checkpoints, make_reader


@pytest.fixture
def store(tmp_path):
    ledger = WechatDesktopStore(str(tmp_path / "pr2.sqlite3"))
    yield ledger
    ledger._get_connection().close()


@pytest.mark.parametrize("directory", ["Migrate", "MIGRATE", "mIgRaTe"])
def test_migration_directory_case_cannot_authenticate_account(tmp_path, monkeypatch, directory):
    account = tmp_path / "synthetic-account"
    _contact(account, "contact/contact.db", OTHER_KEY)
    _contact(account, f"{directory}/contact/contact.db")
    catalog = _catalog(tmp_path, monkeypatch)
    with pytest.raises(DatabaseReadError) as error:
        catalog.resolve()
    assert error.value.code == "key_not_found"


@pytest.mark.parametrize("directory", ["Migrate", "MIGRATE", "mIgRaTe"])
def test_migration_directory_case_cannot_revalidate_or_supply_databases(
        tmp_path, monkeypatch, directory):
    account = tmp_path / "synthetic-account"
    current = _contact(account, "contact/contact.db")
    catalog = _catalog(tmp_path, monkeypatch)
    binding = catalog.resolve()
    _contact(account, f"{directory}/contact/contact.db")
    migrated_message = account / "db_storage" / directory / "message" / "message_0.db"
    migrated_message.parent.mkdir(parents=True)
    migrated_message.touch()
    assert DatabaseCatalog({}).list_databases(binding) == [current]
    current.write_bytes(OTHER_KEY)
    with pytest.raises(DatabaseReadError) as error:
        catalog.resolve()
    assert error.value.code == "login_process_changed"


def test_runtime_new_shard_freezes_discovery_highwater_across_pages(tmp_path, store):
    all_reader, talker = make_reader(tmp_path, shards=2, batch_size=1)
    caches = dict(all_reader.caches)
    hidden = {"message/message_1.db"}
    reader = WechatDatabaseReader({"db_batch_size": 1}, binding=all_reader.binding,
        caches={name: cache for name, cache in caches.items() if name not in hidden})
    source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    initial, events = source.observe_events()
    assert events == [] and initial["db_read_error_code"] == ""
    new_cache = caches["message/message_1.db"]
    add_message(new_cache, talker, 1)
    add_message(new_cache, talker, 2)
    reader.caches["message/message_1.db"] = new_cache

    observation, events = source.observe_events()
    assert [(event.source_local_id, event.receipt_phase) for event in events] == [(1, "offline_backfill")]
    stream_id = events[0].source_stream_id
    checkpoint = store.get_source_checkpoint(reader.account_id, stream_id)
    assert checkpoint["cursor"] == 0 and checkpoint["baseline_high_water"] == 2
    store.receive_source_batch(observation["source_batch"])
    source.acknowledge_events([observation["source_batch"].batch_id])
    add_message(new_cache, talker, 3)
    for local_id, phase in [(2, "offline_backfill"), (3, "live")]:
        observation, events = source.observe_events()
        assert [(event.source_local_id, event.receipt_phase) for event in events] == [(local_id, phase)]
        store.receive_source_batch(observation["source_batch"])
        source.acknowledge_events([observation["source_batch"].batch_id])
    assert source.observe_events()[1] == []


def test_empty_initialized_account_freezes_first_runtime_stream_highwater(store):
    class Reader:
        account_id = "empty-account"
        highwaters = {}

        def refresh(self):
            pass

        def get_highwaters(self):
            return self.highwaters

        def status(self):
            return {}

        def poll_batch(self, checkpoints, boot_highwaters):
            self.boot_highwaters = {stream: dict(high) for stream, high in boot_highwaters.items()}
            return None

    reader = Reader()
    source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    assert source.observe_events()[1] == []
    reader.highwaters = {"new-stream": {"cursor": 2, "generation": "g"}}
    assert source.observe_events()[1] == []
    assert reader.boot_highwaters == reader.highwaters
    reader.highwaters = {"new-stream": {"cursor": 3, "generation": "g"}}
    assert source.observe_events()[1] == []
    assert reader.boot_highwaters["new-stream"]["cursor"] == 2


def test_runtime_new_stream_baseline_failure_retries_at_new_discovery_highwater(tmp_path, store):
    all_reader, talker = make_reader(tmp_path, shards=2)
    new_cache = all_reader.caches["message/message_1.db"]
    reader = WechatDatabaseReader({}, binding=all_reader.binding,
        caches={name: cache for name, cache in all_reader.caches.items() if name != "message/message_1.db"})
    source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    initial, events = source.observe_events()
    assert events == [] and initial["db_read_error_code"] == ""
    add_message(new_cache, talker, 1)
    reader.caches["message/message_1.db"] = new_cache
    with store._connect() as db:
        db.execute("CREATE TRIGGER reject_new_stream BEFORE INSERT ON source_checkpoints "
                   "WHEN NEW.stream_id LIKE 'message/message_1.db:%' "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic initialization failure'); END")
    status, events = source.observe_events()
    assert events == [] and status["db_read_error_code"] == "database_read_failed"
    assert not any(stream.startswith("message/message_1.db:") for stream in source._boot_highwaters)
    add_message(new_cache, talker, 2)
    with store._connect() as db:
        db.execute("DROP TRIGGER reject_new_stream")
    observation, events = source.observe_events()
    assert [event.receipt_phase for event in events] == ["offline_backfill"] * 2
    assert observation["source_batch"].checkpoints[0].baseline_high_water == 2


@pytest.mark.parametrize("blocked_before_stop", [False, True])
def test_explicit_close_resume_rebinds_account_and_ledger_scope(
        tmp_path, store, monkeypatch, blocked_before_stop):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first, talker = make_reader(first_dir)
    second_base, _ = make_reader(second_dir)
    second = WechatDatabaseReader({}, binding=replace(second_base.binding, account_id="second-account"),
                                   caches=second_base.caches)
    source = WechatDatabaseSource({}, reader=first, checkpoint_store=store)
    source.observe_events()
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", lambda _: second)
    if blocked_before_stop:
        def login_changed():
            raise DatabaseReadError("login_process_changed", "synthetic login changed")
        monkeypatch.setattr(first, "refresh", login_changed)
        assert source.observe_events()[0]["db_read_error_code"] == "login_process_changed"
        assert source.observe_events()[0]["db_read_error_code"] == "account_changed"
        source.resume()  # 运行中的恢复调用不能绕过冻结账号。
        assert source.observe_events()[0]["db_read_error_code"] == "account_changed"
    source.close()
    source.resume()
    add_message(second.caches["message/message_0.db"], talker, 1)
    status, events = source.observe_events()
    assert events == [] and status["db_read_error_code"] == ""
    assert source.get_reader() is second
    assert store.source_account_initialized("second-account")
    assert source._frozen_account_id == "second-account"
    add_message(second.caches["message/message_0.db"], talker, 2)
    assert [event.receipt_phase for event in source.observe_events()[1]] == ["live"]


def test_resume_keeps_source_closed_and_account_frozen_until_cleanup_succeeds(tmp_path, monkeypatch):
    reader, _ = make_reader(tmp_path)
    source = WechatDatabaseSource({}, reader=reader)
    source._account_changed = True

    def fail_cleanup():
        raise PermissionError("synthetic-sensitive-path")

    monkeypatch.setattr(reader, "close", fail_cleanup)
    source.close()
    with pytest.raises(DatabaseReadError) as error:
        source.resume()
    assert error.value.code == "snapshot_cleanup_failed"
    assert "synthetic-sensitive-path" not in str(error.value)
    assert source._closed.is_set()
    assert source._frozen_account_id == reader.account_id and source._account_changed
    assert source.status()["db_read_error_code"] == "source_closed"
    monkeypatch.setattr(reader, "close", lambda: None)
    source.resume()
    assert not source._closed.is_set()
    assert source._frozen_account_id is None and not source._account_changed


@pytest.mark.parametrize("field", ["primary", "fallback", "zstd"])
def test_non_utf8_body_filters_only_bad_row_and_advances_checkpoint(tmp_path, field):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    bad = b"\xff\xfe"
    if field == "zstd":
        import zstandard
        bad = zstandard.ZstdCompressor().compress(bad)
    add_message(cache, talker, 1, content="" if field == "fallback" else bad,
                compressed=bad if field == "fallback" else None)
    add_message(cache, talker, 2, content="next message")
    reader.refresh()
    batch = reader.poll_batch(checkpoints(reader), {})
    assert batch.records[0].event is None
    assert batch.records[0].filter_reason == "content_decode_failed"
    assert batch.records[1].event.content == "next message"
    assert batch.checkpoints[0].cursor == 2
    history = reader.read_history(talker)
    assert history.messages[0].content == "[正文解码失败]"
    assert history.messages[0].degraded
    assert history.messages[1].content == "next message"
