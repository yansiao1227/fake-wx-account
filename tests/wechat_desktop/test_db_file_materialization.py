"""SaveAs 来源许可使用合成原生行；不含真实微信正文或 packed blob。"""

from copy import deepcopy
from dataclasses import replace
import pickle
import sqlite3

import pytest

from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.file_materialization import file_path_transition_matches
from .test_db_reader import add_message, make_reader, table
from .test_db_reference_lookup import add_question, question_event, update


FILENAME = "合成扫描文件.pdf"


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def integer(number, value):
    return varint(number << 3) + varint(value)


def blob(number, value):
    return varint((number << 3) | 2) + varint(len(value)) + value


def packed(path, *, metadata=b"synthetic opaque data", extra=b"", entries=None):
    parts = (blob(1, integer(1, 0) + blob(2, metadata)) +
             blob(1, integer(1, 1) + blob(2, str(path).encode("utf-8")))) if entries is None else entries
    return integer(1, 2) + integer(2, 1) + blob(7, parts + integer(4, 2)) + integer(11, 0) + extra


def setup_file(tmp_path, *, subtype=6):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    old_path = tmp_path / f"111111111111_{FILENAME}"
    saved_path = tmp_path / f"222222222222_{FILENAME}"
    old_path.write_bytes(b"synthetic old PDF")
    saved_path.write_bytes(b"synthetic saved PDF")
    app = f"<msg><appmsg><type>{subtype}</type><title>{FILENAME}</title><opaque>synthetic</opaque></appmsg></msg>"
    add_message(cache, talker, 1, content=app, message_type=49, created=10, server_id=55)
    update(cache, talker, packed_info_data=packed(old_path))
    add_question(cache, talker, native_type=49, sender=talker)
    event = question_event(reader)
    return reader, talker, cache, event, old_path, saved_path


def test_own_save_as_changes_only_origin_signature_and_restores_strict_validation(tmp_path):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    signature = event.reference["content_signature"]
    event_signature = event.content_signature
    expected_reference = deepcopy(event.reference)
    snapshot = reader.snapshot_reference_file(event)
    update(cache, talker, packed_info_data=packed(saved_path))
    assert reader.validate_native_event(event) == (False, "reference_source_changed")
    assert reader.accept_reference_file_materialization(event, snapshot, str(saved_path)) == (True, "")
    assert event.reference["content_signature"] != signature
    assert event.content_signature == event_signature
    expected_reference["content_signature"] = event.reference["content_signature"]
    assert event.reference == expected_reference
    assert reader.validate_native_event(event) == (True, "")
    # 接受本次 SaveAs 后，后续路径再变化仍须拒绝。
    update(cache, talker, packed_info_data=packed(tmp_path / f"333333333333_{FILENAME}"))
    assert reader.validate_native_event(event) == (False, "reference_source_changed")


def test_snapshot_is_one_use_and_cannot_be_serialized_or_expose_native_rows(tmp_path):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    snapshot = reader.snapshot_reference_file(event)
    assert repr(snapshot) == "<ReferenceFileSnapshot>"
    with pytest.raises(TypeError, match="当前进程"):
        pickle.dumps(snapshot)
    update(cache, talker, packed_info_data=packed(saved_path))
    assert reader.accept_reference_file_materialization(event, snapshot, saved_path) == (True, "")
    assert reader.accept_reference_file_materialization(event, snapshot, saved_path) == (
        False, "reference_file_materialization_consumed")


def test_snapshot_cannot_be_used_by_another_event_or_reader(tmp_path):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    snapshot = reader.snapshot_reference_file(event)
    assert reader.accept_reference_file_materialization(replace(event, reference=deepcopy(event.reference)),
                                                       snapshot, saved_path) == (
        False, "reference_file_materialization_identity_mismatch")
    other = type(reader)(reader.config, binding=reader.binding, caches=reader.caches)
    other.refresh()
    assert other.accept_reference_file_materialization(event, snapshot, saved_path) == (
        False, "reference_file_materialization_identity_mismatch")


@pytest.mark.parametrize("field,value", [
    ("message_content", "<msg><appmsg><type>6</type><title>替换.pdf</title></appmsg></msg>"),
    ("compress_content", b"changed compressed field"), ("source", b"changed source"),
    ("real_sender_id", 2), ("create_time", 11), ("local_type", 3),
    ("server_id", 56), ("sort_seq", 101),
])
def test_save_as_never_accepts_other_native_origin_column_changes(tmp_path, field, value):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    signature = event.reference["content_signature"]
    snapshot = reader.snapshot_reference_file(event)
    update(cache, talker, packed_info_data=packed(saved_path), **{field: value})
    accepted, reason = reader.accept_reference_file_materialization(event, snapshot, saved_path)
    assert accepted is False and reason
    assert event.reference["content_signature"] == signature


def test_unknown_native_columns_are_compared_even_if_the_regular_signature_does_not_use_them(tmp_path):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f'ALTER TABLE "{table(talker)}" ADD COLUMN unknown_native_field BLOB')
    cache.changed = True
    snapshot = reader.snapshot_reference_file(event)
    update(cache, talker, packed_info_data=packed(saved_path), unknown_native_field=b"changed")
    assert reader.accept_reference_file_materialization(event, snapshot, saved_path) == (
        False, "reference_source_changed")


def test_save_as_never_accepts_question_native_row_change(tmp_path):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    snapshot = reader.snapshot_reference_file(event)
    update(cache, talker, packed_info_data=packed(saved_path))
    update(cache, talker, local_id=2, packed_info_data=b"question also changed")
    assert reader.accept_reference_file_materialization(event, snapshot, saved_path) == (
        False, "source_message_changed")


@pytest.mark.parametrize("target", ["event", "reference"])
def test_modified_in_memory_source_proof_cannot_consume_the_permission(tmp_path, target):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    snapshot = reader.snapshot_reference_file(event)
    if target == "event":
        event.content = "modified question"
    else:
        event.reference["source_message_id"] = "different ID"
    update(cache, talker, packed_info_data=packed(saved_path))
    assert reader.accept_reference_file_materialization(event, snapshot, saved_path) == (
        False, "reference_file_materialization_identity_mismatch")


def test_non_file_reference_cannot_create_snapshot(tmp_path):
    reader, _, _, event, _, _ = setup_file(tmp_path, subtype=5)
    with pytest.raises(DatabaseReadError) as error:
        reader.snapshot_reference_file(event)
    assert error.value.code == "reference_file_snapshot_unavailable"


def test_changed_source_cannot_create_snapshot(tmp_path):
    reader, talker, cache, event, _, _ = setup_file(tmp_path)
    update(cache, talker, source=b"changed before SaveAs")
    with pytest.raises(DatabaseReadError) as error:
        reader.snapshot_reference_file(event)
    assert error.value.code == "reference_source_changed"


def test_same_path_save_requires_identical_packed_bytes(tmp_path):
    reader, _, _, event, old_path, _ = setup_file(tmp_path)
    snapshot = reader.snapshot_reference_file(event)
    assert reader.accept_reference_file_materialization(event, snapshot, old_path) == (True, "")


@pytest.mark.parametrize("kind", ["unknown_field", "duplicate_field", "changed_metadata", "two_paths",
                                  "nonminimal_varint", "truncated", "unknown_entry", "reordered"])
def test_packed_mutations_except_the_unique_path_are_rejected(tmp_path, kind):
    reader, talker, cache, event, old_path, saved_path = setup_file(tmp_path)
    before = packed(old_path)
    after = packed(saved_path)
    if kind == "unknown_field":
        after += integer(12, 0)
    elif kind == "duplicate_field":
        after += integer(11, 0)
    elif kind == "changed_metadata":
        after = packed(saved_path, metadata=b"changed opaque data")
    elif kind == "two_paths":
        entry = blob(1, integer(1, 1) + blob(2, str(saved_path).encode()))
        after = packed(saved_path, entries=entry + entry)
    elif kind == "nonminimal_varint":
        after = b"\x88\x00" + after[1:]
    elif kind == "truncated":
        after = after[:-1]
    elif kind == "unknown_entry":
        after = packed(saved_path, entries=blob(1, integer(1, 9) + blob(2, b"unknown")) +
                       blob(1, integer(1, 1) + blob(2, str(saved_path).encode())))
    elif kind == "reordered":
        after = integer(2, 1) + integer(1, 2) + after[len(integer(1, 2) + integer(2, 1)):]
    assert file_path_transition_matches(before, after, saved_path, FILENAME) is False
    snapshot = reader.snapshot_reference_file(event)
    update(cache, talker, packed_info_data=after)
    assert reader.accept_reference_file_materialization(event, snapshot, saved_path) == (
        False, "reference_file_materialization_invalid")


@pytest.mark.parametrize("kind", ["missing", "different_path", "wrong_filename", "directory", "relative",
                                  "nul_path", "url_path", "different_case_prefix", "unknown_prefix"])
def test_saved_path_requires_existing_exact_native_destination_and_filename(tmp_path, kind):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    supplied = saved_path
    native = saved_path
    if kind == "missing":
        saved_path.unlink()
    elif kind == "different_path":
        supplied = tmp_path / FILENAME
        supplied.write_bytes(b"different file")
    elif kind == "wrong_filename":
        native = supplied = tmp_path / "different.pdf"
        supplied.write_bytes(b"wrong filename")
    elif kind == "directory":
        saved_path.unlink()
        saved_path.mkdir()
    elif kind == "relative":
        native = FILENAME
    elif kind == "nul_path":
        native = str(saved_path) + "\x00"
    elif kind == "url_path":
        native = "https://synthetic.invalid/document.pdf"
    elif kind == "different_case_prefix":
        native = supplied = tmp_path / f"ABCDEF012345_{FILENAME}"
        supplied.write_bytes(b"unrecognized prefix")
    elif kind == "unknown_prefix":
        native = supplied = tmp_path / f"unverified_{FILENAME}"
        supplied.write_bytes(b"unrecognized prefix")
    snapshot = reader.snapshot_reference_file(event)
    update(cache, talker, packed_info_data=packed(native))
    assert reader.accept_reference_file_materialization(event, snapshot, supplied) == (
        False, "reference_file_materialization_invalid")


def test_failed_acceptance_leaves_the_old_signature_and_cannot_be_retried(tmp_path):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    signature = event.reference["content_signature"]
    snapshot = reader.snapshot_reference_file(event)
    update(cache, talker, packed_info_data=packed(saved_path, metadata=b"changed"))
    assert reader.accept_reference_file_materialization(event, snapshot, saved_path)[0] is False
    assert event.reference["content_signature"] == signature
    update(cache, talker, packed_info_data=packed(saved_path))
    assert reader.accept_reference_file_materialization(event, snapshot, saved_path) == (
        False, "reference_file_materialization_consumed")


def test_reference_generation_change_is_not_an_own_save_as_change(tmp_path):
    reader, talker, cache, event, _, saved_path = setup_file(tmp_path)
    snapshot = reader.snapshot_reference_file(event)
    update(cache, talker, packed_info_data=packed(saved_path))
    cache.status = replace(cache.status, generation="different-generation")
    accepted, reason = reader.accept_reference_file_materialization(event, snapshot, saved_path)
    assert accepted is False
    assert reason in {"reference_source_generation_changed", "reference_file_materialization_identity_mismatch"}
