"""混合后端不观察窗口，只有显式定位或发送才绑定 UIA。"""

import sqlite3
import threading
import builtins
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.contracts import SendResult, SendStatus, TargetStatus
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.reader import WechatDatabaseReader
from channel.wechat_desktop.models import ConversationInfo, HeaderInfo, OwnerInfo, WechatHistoryReadError
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.backend import create_wechat_desktop_backend
from channel.wechat_desktop.send_control import SendNotSubmitted
from .test_db_reader import PlainCache, add_message, add_session, make_reader, table


class Client:
    def __init__(self):
        self.pid = 42
        self.wxid = ""
        self.rows = [ConversationInfo("Synthetic", runtime_id="row-1", row_index=0)]
        self.ui_calls = []
        self.sent = []

    def get_owner_window_process_id(self):
        self.ui_calls.append("pid")
        return self.pid

    def get_owner_info(self):
        self.ui_calls.append("owner")
        return OwnerInfo("Owner", self.wxid)

    def get_visible_conversations(self):
        self.ui_calls.append("rows")
        return list(self.rows)

    def get_title(self):
        self.ui_calls.append("header")
        return HeaderInfo("Synthetic", "private")


class Gateway:
    def __init__(self):
        self.client = Client()
        self.bindings = {}
        self.before_send = None
        self.closed = False

    def close(self):
        self.closed = True
        self.bindings.clear()

    def resume(self):
        self.closed = False

    def operation(self):
        return nullcontext()

    def inspect_account(self):
        return self.client.get_owner_window_process_id(), self.client.get_owner_info()

    def list_conversations(self):
        return self.client.get_visible_conversations()

    def read_current_title(self):
        return self.client.get_title()

    def bind_target(self, conversation_id, row):
        self.bindings[conversation_id] = row

    def send_text(self, conversation, text, *, validate=None):
        if self.before_send:
            self.before_send()
        if validate:
            try:
                validate()
            except SendNotSubmitted as exc:
                return SendResult(SendStatus.NOT_SENT, str(exc))
        selector = self.bindings[conversation]
        self.client.sent.append((conversation, text, selector.runtime_id))
        return SendResult(SendStatus.SENT, chunks=1, submitted_chunks=1, verified_chunks=1)


@pytest.fixture
def backend(tmp_path):
    reader, talker = make_reader(tmp_path)
    store = WechatDesktopStore(str(tmp_path / "ledger.sqlite3"))
    driver = Gateway()
    instance = WechatDatabaseBackend({}, db_reader=reader, store=store, uia_gateway=driver)
    yield instance, reader, store, driver, talker
    store._get_connection().close()


def test_observation_and_redelivery_never_call_uia(backend):
    instance, reader, store, driver, talker = backend
    assert instance.observe_events()[1] == []
    add_message(reader.caches["message/message_0.db"], talker, 1)
    first_status, first = instance.observe_events()
    second_status, second = instance.observe_events()
    assert len(first) == 1 and first[0].event_id == second[0].event_id
    assert first_status["source_batch"] is second_status["source_batch"]
    assert driver.client.ui_calls == []


def test_reply_and_progress_hooks_do_not_initialize_uia_or_affect_database_receipt(backend, monkeypatch):
    _, reader, store, driver, talker = backend
    instance = WechatDatabaseBackend({}, db_reader=reader, store=store)

    def reject_uia():
        pytest.fail("数据库接收的回复和进度钩子不得初始化 UIA")

    monkeypatch.setattr(instance, "_actions", reject_uia)
    instance.observe_events()
    instance.begin_reply_cycle("Synthetic", reader.conversation_id(talker))
    instance.register_interim_text(reader.conversation_id(talker), "synthetic progress")
    add_message(reader.caches["message/message_0.db"], talker, 1)
    observation, events = instance.observe_events()
    assert len(events) == 1
    assert store.receive_source_batch(observation["source_batch"])[0].accepted
    instance.acknowledge_events([observation["source_batch"].batch_id])
    instance.forget_interim_text(reader.conversation_id(talker), "synthetic progress")
    instance.end_reply_cycle()
    assert not instance.uia_initialized
    assert driver.client.ui_calls == []
    assert instance.observe_events()[1] == []


def test_first_startup_baseline_skips_existing_history_and_new_events_are_live(backend):
    instance, reader, store, driver, talker = backend
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    assert instance.observe_events()[1] == []
    add_message(cache, talker, 2)
    status, events = instance.observe_events()
    assert [event.source_local_id for event in events] == [2]
    assert events[0].receipt_phase == "live"
    assert status["source_batch"].checkpoints[0].baseline_high_water == 1


def test_lost_ack_duplicate_receipt_then_ack_does_not_return_old_batch(backend):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    add_message(reader.caches["message/message_0.db"], talker, 1)
    observation, events = instance.observe_events()
    batch = observation["source_batch"]
    assert store.receive_source_batch(batch)[0].accepted
    second, again = instance.observe_events()
    assert again[0].event_id == events[0].event_id
    assert not store.receive_source_batch(second["source_batch"])[0].accepted
    instance.acknowledge_events([batch.batch_id])
    instance.acknowledge_events([batch.batch_id])
    empty, events = instance.observe_events()
    assert events == [] and "source_batch" not in empty


def test_restart_backfill_keeps_fixed_highwater_and_remains_cache_only(backend):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    restarted = WechatDatabaseBackend({}, db_reader=reader, store=store, uia_gateway=driver)
    observation, events = restarted.observe_events()
    assert events[0].receipt_phase == "offline_backfill"
    store.receive_source_batch(observation["source_batch"])
    restarted.acknowledge_events([observation["source_batch"].batch_id])
    add_message(cache, talker, 2)
    next_observation, next_events = restarted.observe_events()
    assert next_events[0].receipt_phase == "live"
    assert driver.client.ui_calls == []


def test_refresh_failure_stops_delivery_without_cursor_or_uia_fallback(backend):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    previous = store.get_source_checkpoints(reader.account_id)
    cache = reader.caches["message/message_0.db"]
    cache.status = replace(cache.status, healthy=False, stale=True, error_code="page_hmac_failed")
    status, events = instance.observe_events()
    assert not events and status["db_read_stale"] and not status["db_read_healthy"]
    assert status["db_read_error_code"] == "page_hmac_failed"
    assert store.get_source_checkpoints(reader.account_id) == previous
    assert driver.client.ui_calls == []


def test_stable_database_target_delegates_runtime_id_and_records_display_verification(backend):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    cid = reader.conversation_id(talker)
    result = instance.send_text(cid, "synthetic outgoing")
    assert result.status == SendStatus.SENT
    assert driver.client.sent == [(cid, "synthetic outgoing", "row-1")]
    assert result.observation["target_identity_verification"] == "display_name"


def test_same_display_name_in_database_or_ui_is_ambiguous(backend):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    cid = reader.conversation_id(talker)
    driver.client.rows.append(ConversationInfo("Synthetic", runtime_id="row-2", row_index=1))
    assert instance.resolve_target(cid).status == TargetStatus.AMBIGUOUS
    driver.client.rows.pop()
    cache = reader.caches["contact/contact.db"]
    with sqlite3.connect(cache.path) as connection:
        connection.execute("INSERT INTO contact VALUES ('duplicate','Synthetic','','')")
    cache.changed = True
    assert instance.send_text(cid, "must not send").status == SendStatus.NOT_SENT
    assert driver.client.sent == []


def test_runtime_identity_change_is_stale_and_no_send_occurs(backend):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    cid = reader.conversation_id(talker)
    assert instance.resolve_target(cid).status == TargetStatus.RESOLVED
    driver.client.rows[0] = replace(driver.client.rows[0], runtime_id="recycled-runtime")
    assert instance.send_text(cid, "must not send").status == SendStatus.NOT_SENT
    assert driver.client.sent == []


@pytest.mark.parametrize("mismatch", ["pid", "wxid"])
def test_account_mismatch_blocks_target_before_send(backend, mismatch):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    if mismatch == "pid":
        driver.client.pid = 100
    else:
        driver.client.wxid = "another-account"
    assert instance.send_text(reader.conversation_id(talker), "must not send").status == SendStatus.NOT_SENT
    assert driver.client.sent == []


def test_database_event_validation_does_not_require_visible_message_bubble(backend):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    add_message(reader.caches["message/message_0.db"], talker, 1)
    event = instance.observe_events()[1][0]
    assert instance.validate_reply_target(event).valid
    event.account_id = "another-account"
    assert not instance.validate_reply_target(event).valid


def test_history_and_search_query_do_not_initialize_or_modify_cursors(backend):
    instance, reader, store, driver, talker = backend
    add_message(reader.caches["message/message_0.db"], talker, 1)
    cid = reader.conversation_id(talker)
    assert instance.search_contacts("alias")["contacts"][0]["conversation_id"] == cid
    assert instance.read_chat_history(cid).returned_count == 1
    assert instance.read_current_chat_history().conversation_id == cid
    assert store.get_source_checkpoints(reader.account_id) == {}
    assert driver.client.ui_calls == ["pid", "owner", "header"]


def test_factory_registers_database_backend_and_retains_uia_default(backend):
    _, reader, store, driver, _ = backend
    instance = create_wechat_desktop_backend({"desktop_backend": "db_uia"}, db_reader=reader,
                                            store=store, uia_gateway=driver)
    assert isinstance(instance, WechatDatabaseBackend)
    assert instance.capabilities["background_observation"]
    with pytest.raises(ValueError):
        create_wechat_desktop_backend({"desktop_backend": "invalid"})


def test_first_startup_only_replies_to_proven_unread_native_boundary(backend, tmp_path):
    instance, reader, store, driver, talker = backend
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, server_id=122, sort_seq=99)
    add_message(cache, talker, 2, server_id=123, sort_seq=100)
    add_session(reader, tmp_path, talker)
    observation, events = instance.observe_events()
    assert [event.source_local_id for event in events] == [2]
    assert events[0].receipt_phase == "startup_unread"
    checkpoint = observation["source_batch"].checkpoints[0]
    assert checkpoint.expected_cursor == 1 and checkpoint.baseline_high_water == 2
    store.receive_source_batch(observation["source_batch"])
    instance.acknowledge_events([observation["source_batch"].batch_id])
    add_message(cache, talker, 3, server_id=124, sort_seq=101)
    assert instance.observe_events()[1][0].receipt_phase == "live"


def test_startup_unread_disabled_keeps_all_existing_messages_in_baseline(backend, tmp_path):
    instance, reader, store, driver, talker = backend
    instance.config["process_startup_unread_messages"] = False
    add_message(reader.caches["message/message_0.db"], talker, 1, server_id=123)
    add_session(reader, tmp_path, talker)
    assert instance.observe_events()[1] == []


def test_runtime_new_shard_first_message_is_live_and_not_baselined(backend, tmp_path):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    cache = PlainCache(tmp_path / "message_1.db")
    with sqlite3.connect(cache.path) as connection:
        connection.execute(f'CREATE TABLE "{table(talker)}" (local_id INTEGER PRIMARY KEY,local_type INTEGER,'
                           'real_sender_id INTEGER,create_time INTEGER,message_content BLOB,source BLOB,'
                           'packed_info_data BLOB,compress_content BLOB,server_id INTEGER,sort_seq INTEGER)')
    add_message(cache, talker, 1)
    reader.caches["message/message_1.db"] = cache
    observation, events = instance.observe_events()
    assert len(events) == 1 and events[0].receipt_phase == "live"
    assert observation["source_batch"].checkpoints[0].expected_cursor == 0
    assert observation["source_batch"].checkpoints[0].baseline_high_water == 0


@pytest.mark.parametrize("change", ["recalled", "deleted", "outgoing", "changed"])
def test_source_row_revalidation_rejects_recalled_deleted_or_changed_message(backend, change):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    event = instance.observe_events()[1][0]
    with sqlite3.connect(cache.path) as connection:
        if change == "deleted":
            connection.execute(f'DELETE FROM "{table(talker)}" WHERE local_id=1')
        else:
            field, value = {"recalled": ("local_type", 10000), "outgoing": ("real_sender_id", 2),
                            "changed": ("message_content", "synthetic edited text")}[change]
            connection.execute(f'UPDATE "{table(talker)}" SET {field}=? WHERE local_id=1', (value,))
    cache.changed = True
    assert not instance.validate_reply_target(event).valid
    assert driver.client.ui_calls == []


def test_owner_interface_alias_is_verified_via_database_self_contact(backend):
    instance, reader, store, driver, talker = backend
    cache = reader.caches["contact/contact.db"]
    with sqlite3.connect(cache.path) as connection:
        connection.execute("UPDATE contact SET alias='synthetic-owner-alias' WHERE username='synthetic-owner'")
    cache.changed = True
    driver.client.wxid = "synthetic-owner-alias"
    assert instance.send_text(reader.conversation_id(talker), "synthetic outgoing").status == SendStatus.SENT


def test_unavailable_standalone_attachment_does_not_become_a_quote(backend):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    add_message(reader.caches["message/message_0.db"], talker, 1, message_type=3, content="")
    event = instance.observe_events()[1][0]
    event, count = instance.materialize_event(event)
    assert event.attachment_status == "uia_identity_unavailable" and event.reference == {}
    assert count == 0 and driver.client.ui_calls == []


def test_pending_batch_is_not_redelivered_when_account_health_fails(backend, monkeypatch):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    add_message(reader.caches["message/message_0.db"], talker, 1)
    observation, events = instance.observe_events()
    def failed_refresh():
        raise DatabaseReadError("login_process_changed", "synthetic process changed")
    monkeypatch.setattr(reader, "refresh", failed_refresh)
    status, events = instance.observe_events()
    assert events == [] and status["db_read_stale"]
    assert instance.source.pending_batch is None


@pytest.mark.parametrize("committed", [False, True])
def test_relogin_with_pending_or_lost_ack_rescans_committed_cursor(backend, monkeypatch, committed):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    observation, events = instance.observe_events()
    original_batch = observation["source_batch"]
    if committed:
        assert store.receive_source_batch(original_batch)[0].accepted
    replacement = WechatDatabaseReader({}, binding=replace(reader.binding, pid=43), caches=reader.caches)
    def failed_refresh():
        raise DatabaseReadError("login_process_changed", "synthetic process changed")
    monkeypatch.setattr(reader, "refresh", failed_refresh)
    assert instance.observe_events()[1] == []
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", lambda config: replacement)
    observation, events = instance.observe_events()
    if committed:
        assert events == [] and "source_batch" not in observation
        assert original_batch.records[0].receipt_phase == "live"
        assert not store.receive_source_batch(original_batch)[0].accepted
    else:
        assert events[0].receipt_phase == "offline_backfill"
        assert observation["source_batch"].batch_id != original_batch.batch_id
        assert store.receive_source_batch(observation["source_batch"])[0].accepted
        instance.acknowledge_events([observation["source_batch"].batch_id])
    add_message(cache, talker, 2)
    observation, events = instance.observe_events()
    assert events[0].source_local_id == 2 and events[0].receipt_phase == "live"
    assert store.receive_source_batch(observation["source_batch"])[0].accepted
    assert store.get_source_checkpoint(reader.account_id, events[0].source_stream_id)["cursor"] == 2
    assert driver.client.ui_calls == []


def test_same_account_process_restart_rebinds_and_backfills_before_live(backend, monkeypatch):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    add_message(reader.caches["message/message_0.db"], talker, 1)
    replacement = WechatDatabaseReader({}, binding=replace(reader.binding, pid=43), caches=reader.caches)
    def failed_refresh():
        raise DatabaseReadError("login_process_changed", "synthetic process changed")
    monkeypatch.setattr(reader, "refresh", failed_refresh)
    assert instance.observe_events()[1] == []
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", lambda config: replacement)
    observation, events = instance.observe_events()
    assert events[0].receipt_phase == "offline_backfill"
    store.receive_source_batch(observation["source_batch"])
    instance.acknowledge_events([observation["source_batch"].batch_id])
    add_message(reader.caches["message/message_0.db"], talker, 2)
    assert instance.observe_events()[1][0].receipt_phase == "live"
    assert driver.client.ui_calls == []


def test_account_change_stays_paused_and_does_not_automatically_switch(backend, monkeypatch):
    instance, reader, store, driver, talker = backend
    instance.observe_events()
    replacement = WechatDatabaseReader({}, binding=replace(reader.binding, account_id="another-account", pid=43), caches=reader.caches)
    def failed_refresh():
        raise DatabaseReadError("login_process_changed", "synthetic process changed")
    monkeypatch.setattr(reader, "refresh", failed_refresh)
    instance.observe_events()
    constructed = []
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", lambda config: constructed.append(1) or replacement)
    status, events = instance.observe_events()
    assert events == [] and status["db_read_error_code"] == "account_changed"
    assert instance.observe_events()[0]["db_read_error_code"] == "account_changed"
    assert constructed == [1] and driver.client.ui_calls == []


@pytest.mark.parametrize("change", ["pid", "wxid", "runtime", "duplicate"])
def test_target_change_between_resolution_and_ui_submission_is_rejected(backend, change):
    instance, reader, _, gateway, talker = backend

    def switch_target():
        if change == "pid":
            gateway.client.pid = 43
        elif change == "wxid":
            gateway.client.wxid = "another-account"
        elif change == "runtime":
            gateway.client.rows[0] = replace(gateway.client.rows[0], runtime_id="other-row")
        else:
            gateway.client.rows.append(ConversationInfo("Synthetic", runtime_id="other-row"))

    gateway.before_send = switch_target
    assert instance.send_text(reader.conversation_id(talker), "must not send").status == SendStatus.NOT_SENT
    assert gateway.client.sent == []


def test_database_queries_and_receipts_work_when_uia_imports_are_forbidden(backend, monkeypatch):
    _, reader, store, _, talker = backend
    instance = WechatDatabaseBackend({}, db_reader=reader, store=store)
    original_import = builtins.__import__

    def database_only_import(name, *args, **kwargs):
        if name.startswith("channel.wechat_desktop.uia"):
            pytest.fail("数据库读取不应导入 UIA")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", database_only_import)
    status, events = instance.observe_events()
    assert events == []
    before = store.get_source_checkpoints(reader.account_id)
    add_message(reader.caches["message/message_0.db"], talker, 1)
    cid = reader.conversation_id(talker)
    assert instance.search_contacts("alias")["contacts"][0]["conversation_id"] == cid
    assert instance.read_chat_history(cid).returned_count == 1
    assert before == store.get_source_checkpoints(reader.account_id)
    status, events = instance.observe_events()
    assert len(events) == 1 and not instance.uia_initialized
    assert "uia_available" not in instance.source.status()
    assert "desktop_backend" not in instance.source.status()


def test_relogin_discards_old_target_binding_before_binding_new_runtime(backend, monkeypatch):
    instance, reader, _, gateway, talker = backend
    cid = reader.conversation_id(talker)
    assert instance.resolve_target(cid).status == TargetStatus.RESOLVED
    replacement = WechatDatabaseReader({}, binding=replace(reader.binding, pid=43), caches=reader.caches)

    def process_changed():
        raise DatabaseReadError("login_process_changed", "synthetic restart")

    monkeypatch.setattr(reader, "refresh", process_changed)
    assert instance.observe_events()[1] == []
    assert instance._status()["target_identity_verification"] == ""
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", lambda config: replacement)
    gateway.client.pid = 43
    gateway.client.rows[0] = replace(gateway.client.rows[0], runtime_id="new-process-row")
    assert instance.send_text(cid, "synthetic outgoing").status == SendStatus.SENT
    assert gateway.client.sent == [(cid, "synthetic outgoing", "new-process-row")]


def test_current_history_rejects_account_epoch_changed_after_title_mapping(backend, monkeypatch):
    instance, reader, _, _, _ = backend
    original = instance._binder.current_conversation
    replacement = WechatDatabaseReader({}, binding=reader.binding, caches=reader.caches)
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", lambda config: replacement)

    def mapped_then_restarted():
        mapped = original()
        instance.source.close()
        instance.source.resume()
        return mapped

    monkeypatch.setattr(instance._binder, "current_conversation", mapped_then_restarted)
    with pytest.raises(WechatHistoryReadError) as error:
        instance.read_current_chat_history()
    assert error.value.code == "account_binding_changed"


def test_close_is_idempotent_and_queries_do_not_recreate_reader_or_uia(backend, monkeypatch):
    _, reader, store, _, talker = backend
    instance = WechatDatabaseBackend({}, db_reader=reader, store=store)
    closed = []
    monkeypatch.setattr(reader, "close", lambda: closed.append(True))

    def unexpected_reader(config):
        pytest.fail("已关闭的来源不能重建读取器")

    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", unexpected_reader)
    instance.close()
    instance.close()
    assert closed == [True]
    status, events = instance.observe_events()
    assert events == [] and status["db_read_error_code"] == "source_closed"
    with pytest.raises(DatabaseReadError, match="已关闭"):
        instance.search_contacts()
    with pytest.raises(WechatHistoryReadError) as error:
        instance.read_chat_history(reader.conversation_id(talker))
    assert error.value.code == "source_closed"
    assert instance.send_text(reader.conversation_id(talker), "must not send").status == SendStatus.NOT_SENT
    assert not instance.uia_initialized


def test_reader_session_keeps_query_alive_until_close_then_allows_explicit_resume(backend, monkeypatch):
    instance, reader, _, _, _ = backend
    entered, release, reader_closed, resumed = (threading.Event() for _ in range(4))
    results, errors = [], []
    original_search = reader.search_contacts

    def slow_search(query, limit):
        entered.set()
        assert release.wait(2)
        assert not reader_closed.is_set()
        return original_search(query, limit)

    monkeypatch.setattr(reader, "search_contacts", slow_search)
    monkeypatch.setattr(reader, "close", reader_closed.set)

    def query():
        try:
            results.append(instance.source.search_contacts("alias"))
        except Exception as exc:
            errors.append(exc)

    query_thread = threading.Thread(target=query)
    close_thread = threading.Thread(target=instance.source.close)
    resume_thread = threading.Thread(target=lambda: (instance.source.resume(), resumed.set()))
    query_thread.start()
    assert entered.wait(2)
    close_thread.start()
    assert instance.source._closed.wait(2)
    resume_thread.start()
    try:
        assert not reader_closed.is_set()
        assert not resumed.wait(0.05)
    finally:
        release.set()
        for thread in (query_thread, close_thread, resume_thread):
            thread.join(2)
    assert not any(thread.is_alive() for thread in (query_thread, close_thread, resume_thread))
    assert not errors and len(results[0]) == 1
    assert reader_closed.is_set() and resumed.is_set()
    assert instance.source.status()["db_read_error_code"] == ""


def test_hybrid_resume_waits_for_source_close_and_restores_backfill(backend, monkeypatch):
    instance, reader, store, gateway, talker = backend
    instance.observe_events()
    add_message(reader.caches["message/message_0.db"], talker, 1)
    entered, release, resumed = (threading.Event() for _ in range(3))
    original_close = instance.source.close
    replacement = WechatDatabaseReader({}, binding=reader.binding, caches=reader.caches)
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", lambda config: replacement)

    def slow_close():
        entered.set()
        assert release.wait(2)
        original_close()

    monkeypatch.setattr(instance.source, "close", slow_close)
    closing = threading.Thread(target=instance.close)
    resuming = threading.Thread(target=lambda: (instance.resume(), resumed.set()))
    closing.start()
    assert entered.wait(2)
    resuming.start()
    try:
        assert not resumed.wait(0.05)
        assert gateway.closed
    finally:
        release.set()
        closing.join(2)
        resuming.join(2)
    assert not closing.is_alive() and not resuming.is_alive()
    assert resumed.is_set() and not gateway.closed
    status, events = instance.observe_events()
    assert events[0].receipt_phase == "offline_backfill"
    assert store.receive_source_batch(status["source_batch"])[0].accepted


def test_legacy_uia_driver_injection_uses_public_gateway_binding(backend):
    from channel.wechat_desktop.uia.driver import WechatUiaDriver
    from .helpers import FakeClient, FakeHook

    _, reader, store, _, talker = backend
    client = FakeClient()
    client.rows = [ConversationInfo("Synthetic", runtime_id="synthetic-ui-row", row_index=0)]
    driver = WechatUiaDriver({}, client=client, shell_hook=FakeHook())
    sent = []
    client.send_message = lambda who, text, **kwargs: sent.append((who, text, kwargs)) or {"success": True, "verified": True}
    instance = WechatDatabaseBackend({}, db_reader=reader, store=store, uia_driver=driver)
    cid = reader.conversation_id(talker)
    assert instance.send_text(cid, "synthetic outgoing").status == SendStatus.SENT
    assert sent == [("Synthetic", "synthetic outgoing", {"runtime_id": "synthetic-ui-row", "row_index": 0})]
    assert client.history_calls == [] and not driver._hook_started


@pytest.mark.parametrize("restart_at", ["after_resolution", "before_submission"])
def test_close_and_resume_cannot_revive_old_hybrid_send(backend, monkeypatch, restart_at):
    instance, reader, _, gateway, talker = backend
    replacement = WechatDatabaseReader({}, binding=reader.binding, caches=reader.caches)
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader", lambda config: replacement)

    def restart():
        instance.close()
        instance.resume()

    if restart_at == "after_resolution":
        original_resolve = instance.resolve_target

        def resolve_then_restart(conversation):
            result = original_resolve(conversation)
            restart()
            return result

        monkeypatch.setattr(instance, "resolve_target", resolve_then_restart)
    else:
        gateway.before_send = restart
    cid = reader.conversation_id(talker)
    result = instance.send_text(cid, "old invocation must not send")
    assert result.status == SendStatus.NOT_SENT and gateway.client.sent == []
