"""PR #3：跨批历史来源证据和真实有界的 SQLite 上下文查询。"""

import sqlite3

import pytest

from .test_db_reader import add_message, checkpoints, make_reader, table


@pytest.mark.parametrize("change", ["rewrite", "delete"])
def test_cross_batch_history_keeps_native_evidence_for_later_validation(tmp_path, change):
    reader, talker = make_reader(tmp_path, batch_size=1)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="prior batch", created=1)
    add_message(cache, talker, 2, content="current batch", created=2)
    reader.refresh()

    event = reader.poll_batch(checkpoints(reader, cursor=1), {}).records[0].event
    evidence, = event.task.context_source_events
    assert evidence.source_message_id == event.history[0]["source_message_id"]
    assert evidence.content_signature == event.history[0]["content_signature"]
    assert evidence.account_id == event.account_id
    assert evidence.conversation_id == event.conversation_id
    assert evidence.content == "" and evidence.history == [] and evidence.reference == {}
    assert evidence.task.context_source_events == []
    assert reader.validate_native_event(evidence) == (True, "")

    with sqlite3.connect(cache.path) as conn:
        if change == "rewrite":
            conn.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=1', ("new body",))
        else:
            conn.execute(f'DELETE FROM "{table(talker)}" WHERE local_id=1')
    cache.changed = True
    assert reader.validate_native_event(event) == (True, "")
    assert reader.validate_native_event(evidence) == (
        False, "source_message_changed" if change == "rewrite" else "source_message_deleted")


def test_only_selected_history_keeps_evidence_and_outgoing_sources_validate(tmp_path):
    reader, talker = make_reader(tmp_path)
    reader.config["wechat_history_max_messages"] = 2
    cache = reader.caches["message/message_0.db"]
    for local_id in range(1, 6):
        add_message(cache, talker, local_id, content=f"body {local_id}", created=local_id,
                    sender=2 if local_id == 4 else 3)
    reader.refresh()

    event = reader.poll_batch(checkpoints(reader, cursor=4), {}).records[0].event
    assert [item["source_local_id"] for item in event.history] == [3, 4]
    assert [source.source_local_id for source in event.task.context_source_events] == [3, 4]
    assert event.task.context_source_events[-1].direction == "outgoing"
    assert all(reader.validate_native_event(source) == (True, "")
               for source in event.task.context_source_events)
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f'UPDATE "{table(talker)}" SET real_sender_id=2 WHERE local_id=5')
    cache.changed = True
    assert reader.validate_native_event(event) == (False, "source_message_filtered")


@pytest.mark.parametrize("timestamp_index", [False, True])
def test_clock_rollback_does_not_hide_prior_batch_context_for_later_event(tmp_path, timestamp_index):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="prior batch", created=100)
    add_message(cache, talker, 2, content="clock rolled back", created=50)
    add_message(cache, talker, 3, content="later question", created=200)
    if timestamp_index:
        with sqlite3.connect(cache.path) as conn:
            conn.execute(f'CREATE INDEX context_time ON "{table(talker)}" (create_time)')
    reader.refresh()
    first, last = [record.event for record in reader.poll_batch(checkpoints(reader, cursor=1), {}).records]
    assert first.history == []
    assert [item["content"] for item in last.history] == ["clock rolled back", "prior batch"]


@pytest.mark.parametrize("timestamp_index", [False, True])
def test_small_scan_budget_preserves_history_for_every_event_in_same_batch(tmp_path, timestamp_index):
    reader, talker = make_reader(tmp_path)
    reader.config.update(wechat_history_max_messages=2, db_reply_context_max_rows_per_stream=3)
    cache = reader.caches["message/message_0.db"]
    for local_id in range(1, 61):
        add_message(cache, talker, local_id, content=f"body {local_id}", created=local_id)
    if timestamp_index:
        with sqlite3.connect(cache.path) as conn:
            conn.execute(f'CREATE INDEX context_time ON "{table(talker)}" (create_time)')
    reader.refresh()

    batch = reader.poll_batch(checkpoints(reader, cursor=30), {})
    assert len(batch.records) == 30
    for record in batch.records:
        assert [item["source_local_id"] for item in record.event.history] == [record.local_id - 2, record.local_id - 1]
        assert [source.source_local_id for source in record.event.task.context_source_events] == [
            record.local_id - 2, record.local_id - 1]


@pytest.mark.parametrize("timestamp_index", [False, True])
def test_sqlite_work_is_bounded_even_with_large_history_and_timestamp_ties(tmp_path, timestamp_index):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as conn:
        # 所有历史共享时间戳，若 SQLite 按完整全序排序，将扫描十万行。
        conn.executemany(f'INSERT INTO "{table(talker)}" VALUES (?,?,?,?,?,?,?,?,?,?)', (
            (local_id, 1, 3, 1000, "old body", "", None, None, local_id, local_id)
            for local_id in range(1, 100001)))
        conn.execute(f'INSERT INTO "{table(talker)}" VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (100001, 1, 3, 1001, "current", "", None, None, 100001, 100001))
        if timestamp_index:
            conn.execute(f'CREATE INDEX context_time ON "{table(talker)}" (create_time)')
    cache.changed = True
    reader.refresh()
    stream, = reader._streams.values()

    with cache.read() as conn:
        target_row = reader._rows(stream, after=100000, limit=1, connection=conn)[0]
        target = reader._parse(stream, target_row).event
        progress = []
        statements = []
        conn.set_progress_handler(lambda: progress.append(1) or 0, 100)
        conn.set_trace_callback(statements.append)
        result = reader._reply_context_rows(stream, [(reader._row_order(stream, target_row), target)], conn, 12)
        conn.set_progress_handler(None, 0)
        assert len(result) == 12
        assert len(progress) * 100 < 2000
        query, = [sql for sql in statements if "/* reply_context */" in sql]
        plan = " ".join(str(row[3]).upper() for row in conn.execute("EXPLAIN QUERY PLAN " + query))
        assert "SEARCH " in plan and "TEMP B-TREE" not in plan and "SCAN " not in plan
        assert "LIMIT 12" in query
        assert ("ORDER BY \"create_time\" DESC" in query) is timestamp_index


def test_no_index_does_not_fall_back_to_full_table_scan(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as conn:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table(talker),)).fetchone()[0]
        conn.execute(f'DROP TABLE "{table(talker)}"')
        conn.execute(schema.replace("local_id INTEGER PRIMARY KEY", "local_id INTEGER"))
    add_message(cache, talker, 1, created=1)
    add_message(cache, talker, 2, created=2)
    reader.refresh()
    stream, = reader._streams.values()

    with cache.read() as conn:
        row = reader._rows(stream, after=1, limit=1, connection=conn)[0]
        event = reader._parse(stream, row).event
        statements = []
        conn.set_trace_callback(statements.append)
        assert reader._reply_context_rows(stream, [(reader._row_order(stream, row), event)], conn, 12) == []
        assert not any("/* reply_context */" in sql for sql in statements)
