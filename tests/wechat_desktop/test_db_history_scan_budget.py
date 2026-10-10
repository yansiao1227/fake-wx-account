"""自动候选历史的扫描预算；只读取合成 SQLite 快照。"""

import copy
import re
import sqlite3
from contextlib import contextmanager

import pytest

from channel.wechat_desktop.config import DEFAULT_CONFIG, load_wechat_desktop_config
from .test_db_reader import add_message, checkpoints, make_reader, table


def _add_unusable_rows(cache, talker, first, last, kind):
    message_type = 10000 if kind == "system" else 1
    sender = 99 if kind == "unknown_sender" else 3
    content = b"\x28\xb5\x2f\xfdinvalid" if kind == "corrupt" else "synthetic filtered"
    with sqlite3.connect(cache.path) as conn:
        conn.executemany(f'INSERT INTO "{table(talker)}" VALUES (?,?,?,?,?,?,?,?,?,?)', [
            (local_id, message_type, sender, local_id, content, "", None, None, local_id, local_id)
            for local_id in range(first, last + 1)
        ])
    cache.changed = True


def _observe_context(reader, monkeypatch):
    parsed = []
    statements = []
    original_parse = reader._parse

    def counted_parse(stream, row, *args, **kwargs):
        if kwargs.get("include_filtered"):
            parsed.append((stream.stream_id, int(row["local_id"])))
        return original_parse(stream, row, *args, **kwargs)

    monkeypatch.setattr(reader, "_parse", counted_parse)
    for database, cache in reader.caches.items():
        if not database.startswith("message/message_") or database.endswith("resource.db"):
            continue
        original_read = cache.read

        @contextmanager
        def traced_read(read=original_read):
            with read() as conn:
                conn.set_trace_callback(lambda sql: statements.append(sql)
                                        if "/* reply_context */" in sql else None)
                yield conn

        monkeypatch.setattr(cache, "read", traced_read)
    return parsed, statements


def _query_limits(statements):
    limits = []
    for sql in statements:
        match = re.search(r"\bLIMIT\s+(\d+)\s*$", sql, re.IGNORECASE)
        assert match is not None, f"自动候选历史查询必须带 SQL LIMIT: {sql}"
        limits.append(int(match.group(1)))
    return limits


def _add_time_index(cache, talker):
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f'CREATE INDEX context_time ON "{table(talker)}" (create_time)')


def _additional_parses(parsed, batch):
    batch_ids = {(record.stream_id, record.local_id) for record in batch.records}
    return [identity for identity in parsed if identity not in batch_ids]


@pytest.mark.parametrize("timestamp_index", [False, True])
@pytest.mark.parametrize("kind", ["system", "corrupt", "unknown_sender"])
def test_unusable_rows_stop_at_scan_budget_instead_of_searching_entire_history(
        tmp_path, monkeypatch, kind, timestamp_index):
    reader, talker = make_reader(tmp_path)
    reader.config["reply_context_scan_max_rows"] = 8
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="outside automatic scan", created=1)
    _add_unusable_rows(cache, talker, 2, 501, kind)
    add_message(cache, talker, 502, content="current", created=502)
    if timestamp_index:
        _add_time_index(cache, talker)
    reader.refresh()
    parsed, statements = _observe_context(reader, monkeypatch)

    event = reader.poll_batch(checkpoints(reader, cursor=501), {}).records[0].event

    assert len(parsed) <= 8
    assert _query_limits(statements) == [8]
    assert event.history == []


def test_exhausted_budget_keeps_fewer_usable_messages_without_filling_from_older_rows(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    reader.config["reply_context_scan_max_rows"] = 8
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="outside automatic scan", created=1)
    _add_unusable_rows(cache, talker, 2, 201, "system")
    add_message(cache, talker, 202, content="recent first", created=202)
    add_message(cache, talker, 203, content="recent second", created=203, sender=2)
    add_message(cache, talker, 204, content="current", created=204)
    reader.refresh()
    parsed, statements = _observe_context(reader, monkeypatch)

    event = reader.poll_batch(checkpoints(reader, cursor=203), {}).records[0].event

    assert [item["content"] for item in event.history] == ["recent first", "recent second"]
    assert len(parsed) <= 8
    assert _query_limits(statements) == [8]


@pytest.mark.parametrize("timestamp_index", [False, True])
def test_same_talker_targets_share_each_shard_scan_and_one_batch_budget(tmp_path, monkeypatch, timestamp_index):
    reader, talker = make_reader(tmp_path, shards=3)
    reader.config["reply_context_scan_max_rows"] = 18
    for index in range(3):
        cache = reader.caches[f"message/message_{index}.db"]
        _add_unusable_rows(cache, talker, 1, 150, "system")
        for local_id in range(151, 154):
            add_message(cache, talker, local_id, content=f"current {index}/{local_id}", created=local_id)
        if timestamp_index:
            _add_time_index(cache, talker)
    reader.refresh()
    parsed, statements = _observe_context(reader, monkeypatch)

    batch = reader.poll_batch(checkpoints(reader, cursor=150), {})

    assert len(batch.records) == 9
    assert len(_additional_parses(parsed, batch)) <= 18
    assert len(parsed) <= 18 + len(batch.records)
    assert len(parsed) == len(set(parsed)), "同一候选行不能按 target 重复解析"
    assert len(statements) == 3
    assert sum(_query_limits(statements)) <= 18
    assert all(len(record.event.history) <= 3 for record in batch.records)


def test_rows_outside_timestamp_boundary_still_consume_fallback_scan_budget(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path, batch_size=1)
    reader.config["reply_context_scan_max_rows"] = 5
    cache = reader.caches["message/message_0.db"]
    for local_id in range(1, 10):
        add_message(cache, talker, local_id, content=f"old {local_id}", created=local_id)
    with sqlite3.connect(cache.path) as conn:
        conn.executemany(f'INSERT INTO "{table(talker)}" VALUES (?,?,?,?,?,?,?,?,?,?)', [
            (local_id, 1, 3, 1000, "future timestamp", "", None, None, local_id, local_id)
            for local_id in range(10, 510)
        ])
    add_message(cache, talker, 510, content="current", created=500)
    reader.refresh()
    parsed, statements = _observe_context(reader, monkeypatch)

    event = reader.poll_batch(checkpoints(reader, cursor=509), {}).records[0].event

    assert event.history == []
    assert parsed == []
    assert _query_limits(statements) == [5]


def test_budget_is_shared_across_conversations_in_the_same_batch(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    reader.config["reply_context_scan_max_rows"] = 12
    other = "other-synthetic-user"
    contacts = reader.caches["contact/contact.db"]
    with sqlite3.connect(contacts.path) as conn:
        conn.execute("INSERT INTO contact VALUES (?,?,?,?)", (other, "Other", "", ""))
    contacts.changed = True
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as conn:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table(talker),)).fetchone()[0]
        conn.execute(schema.replace(table(talker), table(other)))
    for native_talker in (talker, other):
        _add_unusable_rows(cache, native_talker, 1, 150, "system")
        add_message(cache, native_talker, 151, content="current", created=151)
    reader.refresh()
    parsed, statements = _observe_context(reader, monkeypatch)

    batch = reader.poll_batch(checkpoints(reader, cursor=150), {})

    assert len(batch.records) == 2
    assert len(parsed) <= 12
    assert len(statements) == 2
    assert sum(_query_limits(statements)) <= 12


@pytest.mark.parametrize("scan_budget", [1, 2, 4, 7])
def test_small_or_uneven_budget_never_opens_a_query_without_a_row_allowance(tmp_path, monkeypatch, scan_budget):
    reader, talker = make_reader(tmp_path, shards=4)
    reader.config["reply_context_scan_max_rows"] = scan_budget
    for index in range(4):
        cache = reader.caches[f"message/message_{index}.db"]
        _add_unusable_rows(cache, talker, 1, 20, "system")
        add_message(cache, talker, 21, content="current", created=21)
    reader.refresh()
    parsed, statements = _observe_context(reader, monkeypatch)

    batch = reader.poll_batch(checkpoints(reader, cursor=20), {})

    assert len(batch.records) == 4
    limits = _query_limits(statements)
    assert all(limit > 0 for limit in limits)
    assert len(limits) == min(scan_budget, 4)
    assert sum(limits) == scan_budget
    assert len(_additional_parses(parsed, batch)) <= scan_budget
    assert len(parsed) <= scan_budget + len(batch.records)


def test_zero_budget_skips_automatic_queries_without_affecting_explicit_history(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    reader.config["reply_context_scan_max_rows"] = 0
    cache = reader.caches["message/message_0.db"]
    for local_id in range(1, 26):
        add_message(cache, talker, local_id, content=f"synthetic {local_id}", created=local_id)
    reader.refresh()
    cursors = checkpoints(reader, cursor=24)
    before = copy.deepcopy(cursors)
    _, statements = _observe_context(reader, monkeypatch)

    event = reader.poll_batch(cursors, {}).records[0].event

    assert event.history == []
    assert statements == []
    assert reader.read_history(talker, limit=20).returned_count == 20
    assert cursors == before


def test_zero_additional_scan_budget_keeps_context_from_rows_already_read_in_batch(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    reader.config["reply_context_scan_max_rows"] = 0
    cache = reader.caches["message/message_0.db"]
    for local_id in range(1, 6):
        add_message(cache, talker, local_id, content=f"body {local_id}", created=local_id)
    reader.refresh()
    parsed, statements = _observe_context(reader, monkeypatch)

    batch = reader.poll_batch(checkpoints(reader, cursor=2), {})

    assert statements == []
    assert _additional_parses(parsed, batch) == []
    assert [[item["source_local_id"] for item in record.event.history] for record in batch.records] == [
        [], [3], [3, 4]]
    assert [source.source_local_id for source in batch.records[-1].event.task.context_source_events] == [3, 4]


def test_batch_scan_budget_also_respects_existing_per_stream_cap(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path, shards=3)
    reader.config.update(reply_context_scan_max_rows=12, db_reply_context_max_rows_per_stream=3)
    for index in range(3):
        cache = reader.caches[f"message/message_{index}.db"]
        _add_unusable_rows(cache, talker, 1, 30, "system")
        add_message(cache, talker, 31, content="current", created=31)
    reader.refresh()
    parsed, statements = _observe_context(reader, monkeypatch)

    batch = reader.poll_batch(checkpoints(reader, cursor=30), {})

    assert _query_limits(statements) == [3, 3, 3]
    assert len(_additional_parses(parsed, batch)) <= 9


def test_zero_candidate_budget_still_enriches_explicit_reference(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    reader.config["reply_context_scan_max_rows"] = 0
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="quoted original", created=1, server_id=123)
    reference = ("<msg><appmsg><type>57</type><title>current</title><refermsg>"
                 "<type>1</type><content>preview</content><svrid>123</svrid>"
                 "</refermsg></appmsg></msg>")
    add_message(cache, talker, 2, content=reference, message_type=49, created=2)
    reader.refresh()
    _, statements = _observe_context(reader, monkeypatch)

    event = reader.poll_batch(checkpoints(reader, cursor=1), {}).records[0].event

    assert statements == []
    assert event.reference["content"] == "quoted original"
    assert event.history == [{**event.reference, "is_reference": True}]


def test_scan_budget_default_is_owned_and_validated_by_channel_configuration():
    assert DEFAULT_CONFIG["reply_context_scan_max_rows"] > 0
    assert load_wechat_desktop_config({"reply_context_scan_max_rows": 0})["reply_context_scan_max_rows"] == 0
    with pytest.raises(ValueError, match="reply_context_scan_max_rows"):
        load_wechat_desktop_config({"reply_context_scan_max_rows": -1})
    with pytest.raises(ValueError, match="reply_context_scan_max_rows"):
        load_wechat_desktop_config({"reply_context_scan_max_rows": 1.5})
