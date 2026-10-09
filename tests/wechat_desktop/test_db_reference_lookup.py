"""一层引用的原生服务器 ID 查找；全部使用合成快照，不调用 UI 或网络。"""

import copy
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from xml.sax.saxutils import escape

import pytest

from channel.wechat_desktop.db.errors import DatabaseReadError
from .test_db_reader import add_message, checkpoints, make_reader, table


def quote(*, native_id="55", native_type=1, content="", sender=""):
    return ("<msg><appmsg><type>57</type><title>synthetic question</title><refermsg>"
            f"<type>{native_type}</type><svrid>{native_id}</svrid><chatusr>{sender}</chatusr>"
            f"<content>{escape(content)}</content></refermsg></appmsg></msg>")


def add_question(cache, talker, *, local_id=2, created=20, **fields):
    add_message(cache, talker, local_id, content=quote(**fields), message_type=49,
                created=created, server_id=200 + local_id)


def question_event(reader):
    reader.refresh()
    return max((record.event for record in reader.poll_batch(checkpoints(reader), {}).records
                if record.event and record.event.content == "synthetic question"),
               key=lambda event: event.native_timestamp)


def update(cache, talker, local_id=1, **fields):
    with sqlite3.connect(cache.path) as conn:
        assignments = ",".join(f'"{key}"=?' for key in fields)
        conn.execute(f'UPDATE "{table(talker)}" SET {assignments} WHERE local_id=?',
                     (*fields.values(), local_id))
    cache.changed = True


@pytest.mark.parametrize("inline", ["", "short preview"])
def test_exact_original_supplies_full_text_and_preserves_preview(tmp_path, inline):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="complete synthetic original", created=10, server_id=55)
    add_question(cache, talker, content=inline, sender=talker)
    event = question_event(reader)
    reference = event.reference
    assert reference["content"] == "complete synthetic original"
    assert reference["preview_content"] == inline
    assert reference["resolved"] is True and reference["degraded"] is False
    assert reference["source_message_id"] == "55"
    assert reference["source_native_message_id"] != "55"
    assert reference["source_local_id"] == 1 and reference["source_account_id"] == reader.account_id
    assert len(reference["content_signature"]) == 64 and reference["depth"] == 1
    assert event.history == [{**reference, "is_reference": True}]
    assert reader.validate_native_event(event) == (True, "")


@pytest.mark.parametrize("subtype,title,url,kind", [
    (5, "Synthetic card", "https://synthetic.invalid/article", "share_card"),
    (6, "synthetic.pdf", "", "file"),
])
def test_missing_app_xml_is_recovered_from_original(tmp_path, subtype, title, url, kind):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    app = f"<msg><appmsg><type>{subtype}</type><title>{title}</title><url>{url}</url></appmsg></msg>"
    add_message(cache, talker, 1, content=app, message_type=49, created=10, server_id=55)
    add_question(cache, talker, native_type=49)
    event = question_event(reader)
    assert event.reference["content_type"] == kind and event.reference["content"] == title
    assert event.reference.get("url", "") == url
    assert event.reference["resolved"] is False  # URL/文件名都不代表已读到页面正文或文件。
    assert "browser_content" not in event.reference and "file_path" not in event.reference


def test_original_outgoing_message_and_other_shard_are_valid_references(tmp_path):
    reader, talker = make_reader(tmp_path, shards=2, group=True)
    original = reader.caches["message/message_1.db"]
    add_message(original, talker, 8, content="owner original", sender=2, created=10, server_id=55)
    add_question(reader.caches["message/message_0.db"], talker, sender="synthetic-owner")
    event = question_event(reader)
    assert event.reference["content"] == "owner original"
    assert event.reference["source_stream_id"].startswith("message/message_1.db:")
    assert event.reference["source_direction"] == "outgoing"
    assert reader.validate_native_event(event) == (True, "")


@pytest.mark.parametrize("same_shard", [True, False])
def test_duplicate_server_id_is_never_guessed(tmp_path, same_shard):
    reader, talker = make_reader(tmp_path, shards=2)
    first, second = (reader.caches[f"message/message_{index}.db"] for index in range(2))
    add_message(first, talker, 1, content="same original", created=10, server_id=55)
    add_message(first if same_shard else second, talker, 2 if same_shard else 1,
                content="same original", created=10, server_id=55)
    add_question(first, talker, local_id=3, content="embedded preview")
    event = question_event(reader)
    assert event.reference["content"] == "embedded preview"
    assert "source_native_message_id" not in event.reference


@pytest.mark.parametrize("fields,quote_fields", [
    ({"created": 30}, {}),
    ({"sender": 2}, {"sender": "synthetic-user"}),
    ({"message_type": 3}, {}),
    ({"message_type": 10000}, {"native_type": 10000}),
    ({"sender": 999}, {}),
])
def test_future_sender_type_system_and_unknown_direction_do_not_enrich(tmp_path, fields, quote_fields):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    defaults = {"created": 10, "content": "untrusted original", "server_id": 55}
    add_message(cache, talker, 1, **{**defaults, **fields})
    add_question(cache, talker, content="embedded preview", **quote_fields)
    event = question_event(reader)
    assert event.reference["content"] == "embedded preview"
    assert "source_native_message_id" not in event.reference


def test_same_shard_future_local_id_is_rejected_even_with_earlier_clock(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_question(cache, talker, local_id=1, content="embedded preview")
    add_message(cache, talker, 2, content="future row", created=10, server_id=55)
    event = question_event(reader)
    assert event.reference["content"] == "embedded preview"
    assert "source_native_message_id" not in event.reference


def test_lookup_never_crosses_native_conversation(tmp_path):
    reader, talker = make_reader(tmp_path)
    other = "other-synthetic-user"
    contact = reader.caches["contact/contact.db"]
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(contact.path) as conn:
        conn.execute("INSERT INTO contact VALUES (?,?,?,?)", (other, "Synthetic", "", ""))
    contact.changed = True
    with sqlite3.connect(cache.path) as conn:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table(talker),)).fetchone()[0]
        conn.execute(schema.replace(table(talker), table(other)))
    add_message(cache, other, 1, content="other conversation secret", created=10, server_id=55)
    add_question(cache, talker, content="embedded preview")
    event = question_event(reader)
    assert event.reference["content"] == "embedded preview"
    assert "other conversation secret" not in str(event.history)


@pytest.mark.parametrize("native_id", ["", "0", "-55", "55abc", "9223372036854775808", "18446744073709551615"])
def test_unusable_server_id_keeps_inline_content_without_sql_lookup(tmp_path, native_id, monkeypatch):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="original", created=10, server_id=55)
    add_question(cache, talker, content="embedded preview", native_id=native_id)
    monkeypatch.setattr(reader, "_lookup_reference_row", lambda *_: pytest.fail("unsafe native ID queried"))
    assert question_event(reader).reference["content"] == "embedded preview"


def test_nested_reference_only_supplies_original_question_not_inner_quote(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    nested = quote(native_id="54", content="inner quote must stay out")
    add_message(cache, talker, 1, content=nested, message_type=49, created=10, server_id=55)
    add_question(cache, talker, native_type=49)
    event = question_event(reader)
    assert event.reference["content"] == "synthetic question"
    assert event.reference["depth"] == 1 and event.reference["content_type"] == "text"
    assert "inner quote must stay out" not in str(event.reference)
    assert "reference" not in event.reference


def test_batch_reuses_lookup_and_live_snapshot_connection(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="original", created=10, server_id=55)
    for local_id in range(2, 8):
        add_question(cache, talker, local_id=local_id)
    reader.refresh()
    statements = []
    original_read = cache.read

    @contextmanager
    def traced_read():
        with original_read() as conn:
            conn.set_trace_callback(lambda sql: statements.append((id(conn), sql)))
            yield conn

    monkeypatch.setattr(cache, "read", traced_read)
    batch = reader.poll_batch(checkpoints(reader), {})
    assert len(batch.records) == 7
    queries = [(identity, sql) for identity, sql in statements if "/* reference_lookup */" in sql]
    live = [(identity, sql) for identity, sql in statements if "WHERE local_id > " in sql]
    assert len(queries) == 1 and queries[0][0] == live[0][0]


@pytest.mark.parametrize("change", ["body", "deleted", "generation", "duplicate"])
def test_reference_evidence_is_revalidated_independently_from_outer_question(tmp_path, change):
    reader, talker = make_reader(tmp_path, shards=2)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="original", created=10, server_id=55)
    add_question(cache, talker)
    event = question_event(reader)
    assert reader.validate_native_event(event) == (True, "")
    if change == "body":
        update(cache, talker, message_content="changed original")
    elif change == "deleted":
        with sqlite3.connect(cache.path) as conn:
            conn.execute(f'DELETE FROM "{table(talker)}" WHERE local_id=1')
        cache.changed = True
    elif change == "generation":
        cache.status = replace(cache.status, generation="replaced-generation")
    else:
        add_message(reader.caches["message/message_1.db"], talker, 1,
                    content="original", created=10, server_id=55)
    valid, reason = reader.validate_native_event(event)
    assert valid is False and reason.startswith("reference_source_")
    with pytest.raises(DatabaseReadError):
        reader.enrich_reference(event)


def test_explicit_enrichment_rechecks_late_original_without_polling_or_moving_cursors(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path, shards=2)
    first, second = (reader.caches[f"message/message_{index}.db"] for index in range(2))
    add_question(first, talker, local_id=1, content="preview")
    event = question_event(reader)
    cursors = checkpoints(reader, cursor=1)
    before = copy.deepcopy(cursors)
    offset = reader._scan_offset
    add_message(second, talker, 1, content="late decrypted original", created=10, server_id=55)
    monkeypatch.setattr(reader, "poll_batch", lambda *_: pytest.fail("enrichment consumed realtime poll"))
    assert reader.enrich_reference(event) is event
    assert event.reference["content"] == "late decrypted original"
    assert cursors == before and reader._scan_offset == offset
    event.account_id = "other-account"
    with pytest.raises(DatabaseReadError) as error:
        reader.enrich_reference(event)
    assert error.value.code == "source_account_mismatch"


def test_compressed_original_and_display_rename_keep_native_reference_evidence(tmp_path):
    import zstandard
    reader, talker = make_reader(tmp_path, group=True)
    cache = reader.caches["message/message_0.db"]
    content = zstandard.ZstdCompressor().compress(b"complete compressed original")
    add_message(cache, talker, 1, content=content, created=10, server_id=55)
    add_question(cache, talker, sender="synthetic-sender")
    event = question_event(reader)
    assert event.reference["content"] == "complete compressed original"
    contact = reader.caches["contact/contact.db"]
    with sqlite3.connect(contact.path) as conn:
        conn.execute("UPDATE contact SET remark='Changed display name'")
    contact.changed = True
    assert reader.validate_native_event(event) == (True, "")


@pytest.mark.parametrize("content,native_type", [("", 1), ("<msg><appmsg><type>57</type></appmsg></msg>", 49)])
def test_empty_original_cannot_turn_placeholder_into_resolved_text(tmp_path, content, native_type):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content=content, message_type=native_type, created=10, server_id=55)
    add_question(cache, talker, native_type=native_type)
    event = question_event(reader)
    assert event.reference["resolved"] is False
    assert "source_native_message_id" not in event.reference
