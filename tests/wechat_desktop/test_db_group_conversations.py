"""全量群聊枚举只读取合成数据库，不发送消息或访问微信界面。"""

import sqlite3
from dataclasses import replace

import pytest

from channel.wechat_desktop.contracts import ConversationTarget
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_db_reader import add_message, add_session, make_reader


def add_groups(reader, rows):
    cache = reader.caches["contact/contact.db"]
    with sqlite3.connect(cache.path) as connection:
        connection.executemany("INSERT INTO contact VALUES (?,?,?,?)", rows)
    cache.changed = True


def reject_ui():
    pytest.fail("群聊枚举不能初始化或访问 UIA")


def test_lists_all_known_groups_without_message_streams_or_search_limit(tmp_path, monkeypatch):
    reader, _ = make_reader(tmp_path)
    rows = [(f"group-{index:03}@chatroom", f"群聊 {index:03}", "", "") for index in range(75)]
    add_groups(reader, rows + [rows[0]])
    add_session(reader, tmp_path, "session-only@chatroom")
    messages = reader.caches["message/message_0.db"]
    with sqlite3.connect(messages.path) as connection:
        connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
        connection.executemany("INSERT INTO Name2Id(user_name) VALUES (?)", [
            ("mapping-only@chatroom",), ("mapping-only-private",),
        ])
    messages.changed = True
    backend = WechatDatabaseBackend({}, db_reader=reader)
    monkeypatch.setattr(backend, "_actions", reject_ui)
    monkeypatch.setattr(backend, "_ledger", lambda: pytest.fail("枚举不能初始化接收账本"))

    groups = backend.list_group_conversations()

    assert len(groups) == 77
    assert all(isinstance(target, ConversationTarget) and target.is_group is True for target in groups)
    assert len({target.conversation_id for target in groups}) == len(groups)
    assert {target.conversation_id for target in groups} == {
        reader.conversation_id(row[0]) for row in rows
    } | {reader.conversation_id("session-only@chatroom"), reader.conversation_id("mapping-only@chatroom")}
    assert groups == sorted(groups, key=lambda target: (target.display_name, target.conversation_id))
    assert len(reader.search_contacts("", 1000)) == 50
    assert not any(stream.talker.endswith("@chatroom") for stream in reader._streams.values())
    assert not backend.uia_initialized and backend._store is None


def test_refreshes_group_names_and_indexes_without_changing_stable_id(tmp_path):
    reader, talker = make_reader(tmp_path, group=True)
    backend = WechatDatabaseBackend({}, db_reader=reader)
    first = backend.list_group_conversations()
    assert first == [ConversationTarget(reader.conversation_id(talker), "Synthetic", True)]
    cache = reader.caches["contact/contact.db"]
    with sqlite3.connect(cache.path) as connection:
        connection.execute("UPDATE contact SET nick_name=?,remark='' WHERE username=?", ("已改名的群", talker))
        connection.execute("INSERT INTO contact VALUES (?,?,?,?)", ("new@chatroom", "新增群", "", ""))
    cache.changed = True

    updated = {target.conversation_id: target for target in backend.list_group_conversations()}

    assert updated[first[0].conversation_id].display_name == "已改名的群"
    assert updated[reader.conversation_id("new@chatroom")].display_name == "新增群"
    with sqlite3.connect(cache.path) as connection:
        connection.execute("DELETE FROM contact WHERE username=?", ("new@chatroom",))
    cache.changed = True
    assert backend.list_group_conversations() == [
        ConversationTarget(first[0].conversation_id, "已改名的群", True)
    ]


def test_preserves_distinct_groups_with_same_display_name(tmp_path):
    reader, _ = make_reader(tmp_path)
    add_groups(reader, [("first@chatroom", "同名群", "", ""), ("second@chatroom", "同名群", "", "")])

    groups = reader.list_group_conversations()

    assert len(groups) == 2
    assert {target.display_name for target in groups} == {"同名群"}
    assert len({target.conversation_id for target in groups}) == 2


def test_enumeration_preserves_pending_batch_and_persistent_receive_cursors(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    store = WechatDesktopStore(str(tmp_path / "ledger.sqlite3"))
    backend = WechatDatabaseBackend({}, db_reader=reader, store=store)
    monkeypatch.setattr(backend, "_actions", reject_ui)
    try:
        backend.observe_events()
        messages = reader.caches["message/message_0.db"]
        add_message(messages, talker, 1)
        observation, first_events = backend.observe_events()
        pending = observation["source_batch"]
        checkpoints = store.get_source_checkpoints(reader.account_id)
        epoch = backend.source.session_epoch
        add_message(messages, talker, 2)
        add_groups(reader, [("no-messages@chatroom", "未收过消息的群", "", "")])

        groups = backend.list_group_conversations()

        assert len(groups) == 1 and groups[0].display_name == "未收过消息的群"
        assert backend.source.pending_batch is pending
        assert backend.source.session_epoch == epoch
        assert store.get_source_checkpoints(reader.account_id) == checkpoints
        repeated, repeated_events = backend.observe_events()
        assert repeated["source_batch"] is pending
        assert [event.event_id for event in repeated_events] == [event.event_id for event in first_events]
        store.receive_source_batch(pending)
        backend.acknowledge_events([pending.batch_id])
        _, next_events = backend.observe_events()
        assert [event.source_local_id for event in next_events] == [2]
        assert not backend.uia_initialized
    finally:
        store._get_connection().close()


def test_refresh_failure_does_not_return_stale_groups(tmp_path):
    reader, _ = make_reader(tmp_path, group=True)
    backend = WechatDatabaseBackend({}, db_reader=reader)
    assert len(backend.list_group_conversations()) == 1
    cache = reader.caches["contact/contact.db"]
    cache.status = replace(cache.status, healthy=False, stale=True, error_code="page_hmac_failed")

    with pytest.raises(DatabaseReadError) as failure:
        backend.list_group_conversations()

    assert failure.value.code == "page_hmac_failed"
    assert not backend.uia_initialized


def test_closed_source_rejects_group_enumeration(tmp_path):
    reader, _ = make_reader(tmp_path, group=True)
    backend = WechatDatabaseBackend({}, db_reader=reader)
    backend.close()

    with pytest.raises(DatabaseReadError) as failure:
        backend.list_group_conversations()

    assert failure.value.code == "source_closed"
