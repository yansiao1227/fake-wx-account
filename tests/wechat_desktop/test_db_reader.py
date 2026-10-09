"""合成明文快照验证读取契约；不使用真实账号或聊天内容。"""

import hashlib
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from channel.wechat_desktop.db.cache import SnapshotStatus
from channel.wechat_desktop.db.discovery import AccountBinding
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.reader import WechatDatabaseReader


class PlainCache:
    def __init__(self, path):
        self.path = Path(path)
        self.status = SnapshotStatus(healthy=True, stale=False, generation="synthetic-generation", changed=True)
        self.changed = True

    def refresh(self):
        self.status = replace(self.status, changed=self.changed)
        self.changed = False
        return self.status

    @contextmanager
    def read(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            yield connection
        finally:
            connection.close()


def table(talker):
    return "Msg_" + hashlib.md5(talker.encode()).hexdigest()


def add_message(cache, talker, local_id, *, content="synthetic text", sender=3, message_type=1,
                sort_seq=100, created=1000, source="", compressed=None, server_id=999):
    with sqlite3.connect(cache.path) as connection:
        connection.execute(f'INSERT INTO "{table(talker)}" VALUES (?,?,?,?,?,?,?,?,?,?)',
                           (local_id, message_type, sender, created, content, source, None, compressed, server_id, sort_seq))
    cache.changed = True


def make_reader(tmp_path, *, shards=1, batch_size=200, group=False):
    talker = "synthetic@chatroom" if group else "synthetic-user"
    contacts = PlainCache(tmp_path / "contact.db")
    with sqlite3.connect(contacts.path) as connection:
        connection.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT, alias TEXT)")
        connection.executemany("INSERT INTO contact VALUES (?,?,?,?)", [
            (talker, "Synthetic", "Synthetic", "alias"), ("synthetic-owner", "Owner", "", ""),
            ("synthetic-sender", "Sender", "", "")])
    resource = PlainCache(tmp_path / "message_resource.db")
    with sqlite3.connect(resource.path) as connection:
        connection.execute("CREATE TABLE SenderName2Id(user_name TEXT)")
        connection.executemany("INSERT INTO SenderName2Id(rowid,user_name) VALUES (?,?)",
                               [(2, "synthetic-owner"), (3, "synthetic-sender" if group else talker)])
    caches = {"contact/contact.db": contacts, "message/message_resource.db": resource}
    for index in range(shards):
        cache = PlainCache(tmp_path / f"message_{index}.db")
        with sqlite3.connect(cache.path) as connection:
            connection.execute(f'CREATE TABLE "{table(talker)}" (local_id INTEGER PRIMARY KEY,local_type INTEGER,'
                               'real_sender_id INTEGER,create_time INTEGER,message_content BLOB,source BLOB,'
                               'packed_info_data BLOB,compress_content BLOB,server_id INTEGER,sort_seq INTEGER)')
        caches[f"message/message_{index}.db"] = cache
    binding = AccountBinding("synthetic-account", tmp_path, tmp_path, 42, "4.1.9.30", "synthetic-owner")
    reader = WechatDatabaseReader({"db_batch_size": batch_size}, binding=binding, caches=caches)
    reader.refresh()
    return reader, talker


def checkpoints(reader, cursor=0):
    return {stream_id: {"cursor": cursor, "baseline_high_water": cursor, "generation": high["generation"]}
            for stream_id, high in reader.get_highwaters().items()}


def add_session(reader, tmp_path, talker, *, count=1, first_server_id=123, pat_id=0):
    cache = PlainCache(tmp_path / "session.db")
    with sqlite3.connect(cache.path) as connection:
        connection.execute("CREATE TABLE SessionTable(username TEXT,unread_count INTEGER,"
                           "unread_first_msg_srv_id INTEGER,unread_first_pat_msg_local_id INTEGER)")
        connection.execute("INSERT INTO SessionTable VALUES (?,?,?,?)", (talker, count, first_server_id, pat_id))
    reader.caches["session/session.db"] = cache


def test_same_text_and_sort_sequence_remain_distinct_and_paginate(tmp_path):
    reader, talker = make_reader(tmp_path, batch_size=1)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    add_message(cache, talker, 2)
    reader.refresh()
    cursors = checkpoints(reader)
    first = reader.poll_batch(cursors, {})
    checkpoint = first.checkpoints[0]
    cursors[checkpoint.stream_id]["cursor"] = checkpoint.cursor
    second = reader.poll_batch(cursors, {})
    assert first.records[0].event.content == second.records[0].event.content
    assert first.records[0].source_message_id != second.records[0].source_message_id
    assert first.records[0].event.fingerprint() != second.records[0].event.fingerprint()
    assert [first.records[0].local_id, second.records[0].local_id] == [1, 2]


def test_idle_reader_reuses_highwaters_but_refresh_and_backlog_still_advance(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path, batch_size=1)
    cache = reader.caches["message/message_0.db"]
    high = reader.get_highwaters()
    cursor = checkpoints(reader)
    assert reader.poll_batch(cursor, high) is None
    original_read = cache.read
    def unexpected_sql_read():
        raise AssertionError("idle polling reopened SQLite")
    monkeypatch.setattr(cache, "read", unexpected_sql_read)
    reader.refresh()
    assert reader.get_highwaters() == high
    assert reader.poll_batch(cursor, high) is None
    monkeypatch.setattr(cache, "read", original_read)
    for local_id in (1, 2):
        add_message(cache, talker, local_id)
    reader.refresh()
    assert max(item["cursor"] for item in reader.get_highwaters().values()) == 2
    first = reader.poll_batch(cursor, high)
    checkpoint = first.checkpoints[0]
    cursor[checkpoint.stream_id]["cursor"] = checkpoint.cursor
    second = reader.poll_batch(cursor, high)
    assert first.records[0].local_id == 1 and second.records[0].local_id == 2


def test_reader_reauthenticates_replaced_database_key_before_publishing(tmp_path):
    from channel.wechat_desktop.db.cache import EncryptedDatabaseCache
    from .test_db_crypto import FAKE_KEY, encrypt_page, plain_page
    reader, _ = make_reader(tmp_path)
    source = tmp_path / "encrypted_contact.db"
    source.write_bytes(encrypt_page(plain_page()))
    cipher = EncryptedDatabaseCache(source, bytes(reversed(FAKE_KEY)), tmp_path / "private" / "copy.db")
    reader.caches["contact/contact.db"] = cipher
    class AuthenticatedProvider:
        def get_key(self, relative, path):
            assert relative == "contact/contact.db" and path == source
            return FAKE_KEY
    reader._key_provider = AuthenticatedProvider()
    try:
        reader.refresh()
        assert cipher.key == FAKE_KEY and cipher.status.healthy
        assert cipher.status.full_rebuilds == 1
        assert reader.status()["db_read_healthy"]
    finally:
        reader.close()


def test_shards_with_same_local_id_have_distinct_source_identity(tmp_path):
    reader, talker = make_reader(tmp_path, shards=2)
    for index in range(2):
        add_message(reader.caches[f"message/message_{index}.db"], talker, 1)
    reader.refresh()
    batch = reader.poll_batch(checkpoints(reader), {})
    assert len(batch.records) == 2
    assert len({record.source_message_id for record in batch.records}) == 2
    assert len(batch.checkpoints) == 2
    history = reader.read_history(talker)
    assert history.returned_count == 2
    assert history.history_window_opened is False
    assert len({message.message_id for message in history.messages}) == 2


@pytest.mark.parametrize("message_type,kind,direction", [
    (10000, "system", "system"), (3, "image", "unknown"), (34, "voice", "unknown"),
    (99999, "unsupported", "unknown"),
])
def test_history_preserves_native_type_when_sender_is_unknown(tmp_path, message_type, kind, direction):
    reader, talker = make_reader(tmp_path)
    add_message(reader.caches["message/message_0.db"], talker, 1, sender=999, message_type=message_type)
    history = reader.read_history(talker)
    assert history.messages[0].content_type == kind
    assert history.messages[0].direction == direction


def test_fixed_boot_highwater_keeps_backfill_phase_across_pages(tmp_path):
    reader, talker = make_reader(tmp_path, batch_size=1)
    cache = reader.caches["message/message_0.db"]
    for index in range(1, 4):
        add_message(cache, talker, index)
    reader.refresh()
    high = reader.get_highwaters()
    cursor = checkpoints(reader)
    add_message(cache, talker, 4)
    reader.refresh()
    phases = []
    for _ in range(4):
        batch = reader.poll_batch(cursor, high)
        phases.append(batch.records[0].receipt_phase)
        advance = batch.checkpoints[0]
        cursor[advance.stream_id]["cursor"] = advance.cursor
    assert phases == ["offline_backfill"] * 3 + ["live"]


def test_group_sender_at_and_reference_are_parsed_from_native_fields(tmp_path):
    reader, talker = make_reader(tmp_path, group=True)
    source = '<msgsource><atuserlist><![CDATA[synthetic-owner]]></atuserlist></msgsource>'
    quoted = '<msg><appmsg><title>synthetic reply</title><type>57</type><refermsg><type>1</type>' \
             '<displayname>Quoted</displayname><content>synthetic quote</content><svrid>88</svrid></refermsg></appmsg></msg>'
    add_message(reader.caches["message/message_0.db"], talker, 1, content=quoted,
                message_type=(57 << 32) | 49, source=source)
    reader.refresh()
    event = reader.poll_batch(checkpoints(reader), {}).records[0].event
    assert event.sender_id == "synthetic-sender"
    assert event.sender_name == "Sender" and event.is_at and event.is_group
    assert event.content_type == "text" and event.content == "synthetic reply"
    assert event.reference["content"] == "synthetic quote"
    assert event.reference["source_message_id"] == "88"


def test_zstd_long_text_and_binary_compressed_fallback(tmp_path):
    import zstandard
    reader, talker = make_reader(tmp_path)
    compressed = zstandard.ZstdCompressor().compress(b"synthetic long text")
    add_message(reader.caches["message/message_0.db"], talker, 1, content=compressed)
    add_message(reader.caches["message/message_0.db"], talker, 2, content=b"\xff", compressed=compressed)
    reader.refresh()
    batch = reader.poll_batch(checkpoints(reader), {})
    assert [record.event.content for record in batch.records] == ["synthetic long text"] * 2


def test_group_at_xml_inside_binary_packed_info_is_recognized(tmp_path):
    reader, talker = make_reader(tmp_path, group=True)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    packed = b"\xff\x00\x01<msgsource><atuserlist>synthetic-owner</atuserlist></msgsource>\xff"
    with sqlite3.connect(cache.path) as connection:
        connection.execute(f'UPDATE "{table(talker)}" SET packed_info_data=? WHERE local_id=1', (packed,))
    reader.refresh()
    assert reader.poll_batch(checkpoints(reader), {}).records[0].event.is_at


def test_local_sender_mapping_wins_and_numeric_two_is_not_assumed_self(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as connection:
        connection.execute("CREATE TABLE Name2Id(user_name TEXT,is_session INTEGER)")
        connection.execute("INSERT INTO Name2Id(rowid,user_name,is_session) VALUES (2,?,1)", (talker,))
    cache.changed = True
    add_message(cache, talker, 1, sender=2)
    add_message(cache, talker, 2, sender=3)  # Global resource ID 3 must not fill a hole in a local mapping.
    reader.refresh()
    batch = reader.poll_batch(checkpoints(reader), {})
    assert batch.records[0].event.direction == "incoming" and batch.records[0].event.sender_id == talker
    assert batch.records[1].event is None and batch.records[1].filter_reason == "sender_direction_unresolved"


def test_unmapped_sender_is_filtered_in_private_and_group_messages(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, sender=999)
    reader.refresh()
    record = reader.poll_batch(checkpoints(reader), {}).records[0]
    assert record.event is None and record.filter_reason == "sender_direction_unresolved"


def test_outgoing_and_system_rows_are_filtered_but_advance_batch(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, sender=2)
    add_message(cache, talker, 2, message_type=10000)
    reader.refresh()
    batch = reader.poll_batch(checkpoints(reader), {})
    assert [record.filter_reason for record in batch.records] == ["outgoing_message", "system_message"]
    assert batch.checkpoints[0].cursor == 2
    history = reader.read_history(talker)
    assert history.messages[0].direction == "outgoing"
    assert history.messages[1].content_type == "system"


def test_contact_rename_keeps_conversation_and_message_identity(tmp_path):
    reader, talker = make_reader(tmp_path)
    add_message(reader.caches["message/message_0.db"], talker, 1)
    reader.refresh()
    before = reader.poll_batch(checkpoints(reader), {}).records[0].event
    cache = reader.caches["contact/contact.db"]
    with sqlite3.connect(cache.path) as connection:
        connection.execute("UPDATE contact SET remark='Renamed' WHERE username=?", (talker,))
    cache.changed = True
    reader.refresh()
    after = reader.poll_batch(checkpoints(reader), {}).records[0].event
    assert before.conversation_id == after.conversation_id and before.event_id == after.event_id
    assert after.conversation_name == "Renamed"


def test_history_search_do_not_advance_poll_cursors(tmp_path):
    reader, talker = make_reader(tmp_path)
    add_message(reader.caches["message/message_0.db"], talker, 1)
    reader.refresh()
    cursor = checkpoints(reader)
    original = {key: dict(value) for key, value in cursor.items()}
    assert reader.search_contacts("alias")[0]["username"] == talker
    assert reader.read_history(talker, 50).returned_count == 1
    assert cursor == original
    assert reader.poll_batch(cursor, {}).records[0].local_id == 1


def test_stale_snapshot_and_replaced_source_cannot_advance_cursor(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    cursor = checkpoints(reader)
    cache.status = replace(cache.status, stale=True, healthy=False, error_code="page_hmac_failed")
    with pytest.raises(DatabaseReadError, match="快照"):
        reader.refresh()
    assert reader.status()["db_read_stale"]
    cache.status = replace(cache.status, stale=False, healthy=True, generation="replacement")
    cache.changed = True
    reader.refresh()
    with pytest.raises(DatabaseReadError) as exc:
        reader.poll_batch(cursor, {})
    assert exc.value.code == "source_generation_changed"


def test_startup_unread_requires_unique_native_boundary_and_matching_incoming_count(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, server_id=122, sort_seq=99)
    add_message(cache, talker, 2, server_id=123, sort_seq=100)
    add_message(cache, talker, 3, server_id=124, sort_seq=101, sender=2)
    add_message(cache, talker, 4, server_id=125, sort_seq=102)
    add_session(reader, tmp_path, talker, count=2)
    reader.refresh()
    high = reader.get_highwaters()
    bounds = reader.startup_unread_boundaries(high)
    stream_id = next(iter(bounds))
    assert bounds[stream_id] == 2
    batch = reader.poll_batch(checkpoints(reader), high)
    assert [record.receipt_phase for record in batch.records] == ["offline_backfill", "startup_unread", "offline_backfill", "startup_unread"]


@pytest.mark.parametrize("case", ["server_duplicate", "sort_duplicate", "count_mismatch", "pat_only"])
def test_ambiguous_startup_boundary_never_enables_startup_reply(tmp_path, case):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, server_id=123, sort_seq=100)
    add_message(cache, talker, 2, server_id=123 if case == "server_duplicate" else 124,
                sort_seq=100 if case == "sort_duplicate" else 101)
    add_session(reader, tmp_path, talker, count=1 if case == "count_mismatch" else 2,
                first_server_id=0 if case == "pat_only" else 123, pat_id=1)
    reader.refresh()
    high = reader.get_highwaters()
    assert reader.startup_unread_boundaries(high) == {}
    assert all(record.receipt_phase == "offline_backfill" for record in reader.poll_batch(checkpoints(reader), high).records)
