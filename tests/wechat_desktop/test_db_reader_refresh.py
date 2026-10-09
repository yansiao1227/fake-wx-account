"""通过 SQL 查询计数核验刷新范围，避免用耗时阈值掩盖历史库扫描。"""

import sqlite3
from contextlib import contextmanager
from dataclasses import replace

import pytest

from channel.wechat_desktop.db.errors import DatabaseReadError
from .test_db_reader import PlainCache, add_message, checkpoints, make_reader, table


def trace_caches(reader, monkeypatch):
    queries = {relative: [] for relative in reader.caches}
    for relative, cache in reader.caches.items():
        original_read = cache.read
        @contextmanager
        def traced_read(original=original_read, output=queries[relative]):
            with original() as conn:
                conn.set_trace_callback(output.append)
                yield conn
        monkeypatch.setattr(cache, "read", traced_read)
    return queries


def mark_changed_tables(cache, *tables):
    """模拟认证WAL已发布的表/索引页清单，来自真实SQLite页归属。"""
    with sqlite3.connect(cache.path) as conn:
        pages = tuple(row[0] for row in conn.execute(
            "SELECT d.pageno FROM dbstat AS d LEFT JOIN sqlite_master AS m ON m.name=d.name "
            "WHERE coalesce(m.tbl_name,d.name) IN (" + ",".join("?" for _ in tables) + ")", tables))
    cache.status = replace(cache.status, changed_pages=pages)
    cache.changed = True


def test_ordinary_message_refresh_only_queries_changed_shard(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path, shards=18)
    # 在其他分片中增加许多不变消息表，再暖好元数据、高水位和空闲状态。
    for index in range(1, 18):
        cache = reader.caches[f"message/message_{index}.db"]
        with sqlite3.connect(cache.path) as conn:
            for number in range(4):
                conn.execute(f'CREATE TABLE "Msg_extra_{number}" (local_id INTEGER PRIMARY KEY,'
                             'local_type INTEGER,create_time INTEGER)')
        cache.changed = True
    reader.refresh()
    high = reader.get_highwaters()
    cursor = checkpoints(reader)
    assert reader.poll_batch(cursor, high) is None
    queries = trace_caches(reader, monkeypatch)
    changed = "message/message_0.db"
    add_message(reader.caches[changed], talker, 1)
    mark_changed_tables(reader.caches[changed], table(talker))
    reader.refresh()
    assert reader.get_highwaters()[changed + ":" + table(talker)]["cursor"] == 1
    batch = reader.poll_batch(cursor, high)
    assert batch.records[0].event.content == "synthetic text"
    assert all(not statements for relative, statements in queries.items() if relative != changed)
    assert not any("table_info" in sql.lower() or "sqlite_master" in sql.lower() for sql in queries[changed])
    # 同一快照、同一游标重复查询无需重做 max/count；分页后计数由已读取行数扣减。
    before_aggregates = sum("max(" in sql.lower() or "count(" in sql.lower() for sql in queries[changed])
    reader.poll_batch(cursor, high)
    advance = batch.checkpoints[0]
    cursor[advance.stream_id]["cursor"] = advance.cursor
    assert reader.poll_batch(cursor, high) is None
    assert sum("max(" in sql.lower() or "count(" in sql.lower() for sql in queries[changed]) == before_aggregates


def test_unchanged_snapshot_backlog_pages_do_not_rescan_aggregates(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path, batch_size=1, shards=12)
    cache = reader.caches["message/message_0.db"]
    for local_id in range(1, 5):
        add_message(cache, talker, local_id)
    reader.refresh()
    high = reader.get_highwaters()
    cursor = checkpoints(reader)
    queries = trace_caches(reader, monkeypatch)
    seen, backlog = [], []
    for _ in range(4):
        batch = reader.poll_batch(cursor, high)
        seen.append(batch.records[0].local_id)
        backlog.append(reader.status()["db_read_backlog"][batch.records[0].stream_id])
        advance = batch.checkpoints[0]
        cursor[advance.stream_id]["cursor"] = advance.cursor
    assert seen == [1, 2, 3, 4]
    assert backlog == [4, 3, 2, 1]
    assert all(not statements for relative, statements in queries.items() if relative != "message/message_0.db")
    sql = queries["message/message_0.db"]
    assert sum("count(" in statement.lower() for statement in sql) == 1
    assert not any("max(" in statement.lower() for statement in sql)


def test_one_changed_message_page_does_not_query_other_tables_in_same_shard(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path, shards=3)
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as conn:
        conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (3,?)", (talker,))
        for number in range(150):
            conn.execute(f'CREATE TABLE "Msg_history_{number}" (local_id INTEGER PRIMARY KEY,'
                         'local_type INTEGER,create_time INTEGER)')
    cache.changed = True
    reader.refresh()
    high = reader.get_highwaters()
    cursor = checkpoints(reader)
    reader.poll_batch(cursor, high)
    original_indexes = (reader._contacts, reader._streams, reader._sender_maps, reader._conversation_lookup)
    queries = trace_caches(reader, monkeypatch)
    add_message(cache, talker, 1)
    mark_changed_tables(cache, table(talker))
    reader.refresh()
    updated = reader.get_highwaters()
    batch = reader.poll_batch(cursor, high)
    assert len(batch.records) == 1
    assert updated[batch.records[0].stream_id]["cursor"] == 1
    statements = queries["message/message_0.db"]
    assert len(statements) == 4  # schema_version、目标表max/count和有界分页。
    assert all(before is after for before, after in zip(
        original_indexes, (reader._contacts, reader._streams, reader._sender_maps, reader._conversation_lookup)))
    assert not any("Msg_history_" in sql or "Name2Id" in sql or "dbstat" in sql for sql in statements)
    assert all(not sql for relative, sql in queries.items() if relative != "message/message_0.db")


def test_no_published_pages_preserve_idle_signature_and_global_indexes(tmp_path, monkeypatch):
    reader, _ = make_reader(tmp_path)
    high = reader.get_highwaters()
    cursor = checkpoints(reader)
    assert reader.poll_batch(cursor, high) is None
    original_signature, original_revision = reader._idle_poll_signature, reader._index_revision
    original_indexes = (reader._contacts, reader._streams, reader._sender_maps, reader._conversation_lookup)
    cache = reader.caches["message/message_0.db"]
    cache.status = replace(cache.status, changed_pages=())
    cache.changed = True
    queries = trace_caches(reader, monkeypatch)
    reader.refresh()
    assert reader._idle_poll_signature == original_signature
    assert reader._index_revision == original_revision
    assert all(before is after for before, after in zip(
        original_indexes, (reader._contacts, reader._streams, reader._sender_maps, reader._conversation_lookup)))
    assert reader.get_highwaters() == high
    assert reader.poll_batch(cursor, high) is None
    assert queries["message/message_0.db"] == ["PRAGMA schema_version"]


def test_mapping_page_and_its_index_refresh_independently_from_message_streams(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path, group=True)
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as conn:
        conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
        conn.execute("CREATE INDEX sender_name_index ON Name2Id(user_name)")
        conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (3,'wxid_before')")
    add_message(cache, talker, 1)
    reader.refresh()
    cursor = checkpoints(reader)
    assert reader.poll_batch(cursor, {}).records[0].event.sender_id == "wxid_before"
    queries = trace_caches(reader, monkeypatch)
    with sqlite3.connect(cache.path) as conn:
        conn.execute("UPDATE Name2Id SET user_name='wxid_after' WHERE rowid=3")
        index_page = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='sender_name_index'").fetchone()[0]
    cache.status = replace(cache.status, changed_pages=(index_page,))
    cache.changed = True
    reader.refresh()
    event = reader.poll_batch(cursor, {}).records[0].event
    assert event.sender_id == "wxid_after"
    sql = queries["message/message_0.db"]
    assert any("Name2Id" in statement for statement in sql)
    assert not any("max(" in statement.lower() or "count(" in statement.lower() for statement in sql)
    assert all(not statements for relative, statements in queries.items() if relative != "message/message_0.db")


def test_new_overflow_pages_rebuild_ownership_then_resume_targeted_queries(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    cursor = checkpoints(reader)
    queries = trace_caches(reader, monkeypatch)
    add_message(cache, talker, 1, content="x" * 14000)
    mark_changed_tables(cache, table(talker))
    reader.refresh()
    batch = reader.poll_batch(cursor, {})
    assert batch.records[0].event.content == "x" * 14000
    assert sum("dbstat" in sql.lower() for sql in queries["message/message_0.db"]) == 1
    checkpoint = batch.checkpoints[0]
    cursor[checkpoint.stream_id]["cursor"] = checkpoint.cursor
    queries["message/message_0.db"].clear()
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=1', ("y" * 14000,))
    mark_changed_tables(cache, table(talker))
    reader.refresh()
    reader.get_highwaters()
    assert reader.poll_batch(cursor, {}) is None
    assert not any("dbstat" in sql.lower() for sql in queries["message/message_0.db"])


def test_missing_dbstat_safely_falls_back_to_changed_shard(tmp_path, monkeypatch):
    monkeypatch.setattr("channel.wechat_desktop.db.reader.WechatDatabaseReader._page_tables", staticmethod(lambda conn: None))
    reader, talker = make_reader(tmp_path, shards=3)
    cursor = checkpoints(reader)
    reader.poll_batch(cursor, {})
    queries = trace_caches(reader, monkeypatch)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    mark_changed_tables(cache, table(talker))
    reader.refresh()
    assert reader.poll_batch(cursor, {}).records[0].local_id == 1
    assert all(not statements for relative, statements in queries.items() if relative != "message/message_0.db")


def test_failed_schema_refresh_remains_paused_until_schema_is_repaired(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as conn:
        conn.execute("CREATE TABLE Msg_invalid(local_id INTEGER PRIMARY KEY)")
    cache.changed = True
    for _ in range(2):
        with pytest.raises(DatabaseReadError) as error:
            reader.refresh()
        assert error.value.code == "unsupported_message_schema"
    with sqlite3.connect(cache.path) as conn:
        conn.execute("DROP TABLE Msg_invalid")
    cache.changed = True
    reader.refresh()
    assert reader.status()["db_read_healthy"]


def test_schema_mapping_and_new_shard_changes_are_discovered(tmp_path):
    reader, talker = make_reader(tmp_path, group=True)
    original = reader.caches["message/message_0.db"]
    with sqlite3.connect(original.path) as conn:
        conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (3,'wxid_first')")
    add_message(original, talker, 1)
    reader.refresh()
    cursor = checkpoints(reader)
    assert reader.poll_batch(cursor, {}).records[0].event.sender_id == "wxid_first"
    # schema_version不变的映射原位修改也必须生效。
    with sqlite3.connect(original.path) as conn:
        conn.execute("UPDATE Name2Id SET user_name='wxid_updated' WHERE rowid=3")
    original.changed = True
    reader.refresh()
    assert reader.poll_batch(cursor, {}).records[0].event.sender_id == "wxid_updated"
    new_cache = PlainCache(tmp_path / "message_1.db")
    with sqlite3.connect(new_cache.path) as conn:
        conn.execute(f'CREATE TABLE "{table(talker)}" (local_id INTEGER PRIMARY KEY,local_type INTEGER,'
                     'real_sender_id INTEGER,create_time INTEGER,message_content BLOB,source BLOB,'
                     'packed_info_data BLOB,compress_content BLOB,server_id INTEGER,sort_seq INTEGER)')
    reader.caches["message/message_1.db"] = new_cache
    add_message(new_cache, talker, 9)
    reader.refresh()
    high = reader.get_highwaters()
    assert high["message/message_1.db:" + table(talker)]["cursor"] == 9
    # 无已持久化基线的新流不会交付历史，建立基线后仍能分页。
    assert len(reader.poll_batch(cursor, high).records) == 1
    cursor = checkpoints(reader)
    assert len(reader.poll_batch(cursor, high).records) == 2


def test_new_table_and_name_mapping_in_existing_shard_are_discovered(tmp_path):
    reader, talker = make_reader(tmp_path)
    original_cursor = checkpoints(reader)
    assert reader.poll_batch(original_cursor, {}) is None
    cache = reader.caches["message/message_0.db"]
    new_talker = "synthetic-new-session"
    with sqlite3.connect(cache.path) as conn:
        conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (3,?)", (new_talker,))
        conn.execute(f'CREATE TABLE "{table(new_talker)}" (local_id INTEGER PRIMARY KEY,local_type INTEGER,'
                     'real_sender_id INTEGER,create_time INTEGER,message_content BLOB,source BLOB,'
                     'packed_info_data BLOB,compress_content BLOB,server_id INTEGER,sort_seq INTEGER)')
    add_message(cache, new_talker, 7)
    mark_changed_tables(cache, table(new_talker), "Name2Id")
    reader.refresh()
    stream_id = "message/message_0.db:" + table(new_talker)
    high = reader.get_highwaters()
    assert high[stream_id]["cursor"] == 7
    assert reader.poll_batch(original_cursor, high) is None
    batch = reader.poll_batch(checkpoints(reader), high)
    assert batch.records[0].stream_id == stream_id
    assert batch.records[0].event.conversation_name == new_talker
    assert batch.records[0].receipt_phase == "offline_backfill"


def test_cached_highwater_still_detects_cursor_regression_and_generation_change(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 5)
    reader.refresh()
    cursor = checkpoints(reader, 5)
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f'DELETE FROM "{table(talker)}"')
    cache.changed = True
    reader.refresh()
    with pytest.raises(DatabaseReadError) as error:
        reader.poll_batch(cursor, {})
    assert error.value.code == "source_cursor_regressed"
    cache.status = replace(cache.status, generation="new-generation")
    # generation即使没有changed布尔也不能沿用旧元数据。
    reader.refresh()
    with pytest.raises(DatabaseReadError) as error:
        reader.poll_batch(cursor, {})
    assert error.value.code == "source_generation_changed"
