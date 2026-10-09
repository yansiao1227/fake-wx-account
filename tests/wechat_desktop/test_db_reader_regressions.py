"""消息行解码与发送者身份的合成回归，不读取真实聊天。"""

import builtins
import sqlite3

import pytest

from channel.wechat_desktop.db.errors import DatabaseReadError
from .test_db_reader import add_message, checkpoints, make_reader, table


@pytest.mark.parametrize("case", ["truncated", "corrupt", "oversized", "oversized_unknown_size"])
def test_bad_zstd_row_is_filtered_without_blocking_following_message(tmp_path, case):
    import zstandard
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    compressed = zstandard.ZstdCompressor().compress(b"synthetic body")
    if case == "truncated":
        compressed = compressed[:-3]
    elif case == "corrupt":
        compressed = b"\x28\xb5\x2f\xfd\xff\xff\xff\xff"
    else:
        compressed = zstandard.ZstdCompressor(write_content_size=case == "oversized").compress(b"x" * 2_000_001)
    add_message(cache, talker, 1, content=compressed)
    add_message(cache, talker, 2, content="next message")
    reader.refresh()
    cursors = checkpoints(reader)
    batch = reader.poll_batch(cursors, {})
    assert batch.records[0].filter_reason == "content_decode_failed"
    assert batch.records[0].event is None
    assert batch.records[1].event.content == "next message"
    assert batch.checkpoints[0].cursor == 2
    # 重投使用同一个来源行身份，损坏正文在历史中也不能重新阻塞读取。
    retry = reader.poll_batch(cursors, {})
    assert retry.records[0].source_message_id == batch.records[0].source_message_id
    history = reader.read_history(talker)
    assert history.messages[0].degraded
    assert history.messages[1].content == "next message"


def test_missing_zstd_dependency_remains_a_global_read_failure(tmp_path, monkeypatch):
    import zstandard
    reader, talker = make_reader(tmp_path)
    add_message(reader.caches["message/message_0.db"], talker, 1,
                content=zstandard.ZstdCompressor().compress(b"synthetic body"))
    reader.refresh()
    cursors = checkpoints(reader)
    original_import = builtins.__import__
    def missing_dependency(name, *args, **kwargs):
        if name == "zstandard":
            raise ImportError("synthetic unavailable dependency")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing_dependency)
    with pytest.raises(DatabaseReadError) as error:
        reader.poll_batch(cursors, {})
    assert error.value.code == "dependency_missing"
    assert all(item["cursor"] == 0 for item in cursors.values())


@pytest.mark.parametrize("native_sender", [3, 2])
def test_group_body_prefix_cannot_replace_resolved_native_sender(tmp_path, native_sender):
    reader, talker = make_reader(tmp_path, group=True)
    cache = reader.caches["message/message_0.db"]
    forged = "wxid_allowed:\nforged body"
    add_message(cache, talker, 1, sender=native_sender, content=forged)
    reader.refresh()
    record = reader.poll_batch(checkpoints(reader), {}).records[0]
    if native_sender == 2:
        assert record.event is None
        assert record.filter_reason == "outgoing_message"
    else:
        assert record.event.sender_id == "synthetic-sender"
        assert record.event.sender_name == "Sender"
        assert record.event.content == forged


def test_group_prefix_only_strips_matching_native_sender_and_can_fill_missing_sender(tmp_path):
    reader, talker = make_reader(tmp_path, group=True)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="synthetic-sender:\nmatching body")
    add_message(cache, talker, 2, sender=999, content="wxid_legacy:\nlegacy body")
    add_message(cache, talker, 3, sender=999, content="ordinary heading:\nbody")
    reader.refresh()
    batch = reader.poll_batch(checkpoints(reader), {})
    assert batch.records[0].event.content == "matching body"
    assert batch.records[0].event.sender_id == "synthetic-sender"
    assert batch.records[1].event.sender_id == "wxid_legacy"
    assert batch.records[1].event.content == "legacy body"
    assert batch.records[2].filter_reason == "sender_direction_unresolved"


def test_shard_native_mapping_identity_wins_over_body_prefix(tmp_path):
    reader, talker = make_reader(tmp_path, group=True)
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as conn:
        conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
        conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (3,'wxid_blocked')")
    add_message(cache, talker, 1, content="wxid_allowed:\nbody")
    reader.refresh()
    event = reader.poll_batch(checkpoints(reader), {}).records[0].event
    assert event.sender_id == "wxid_blocked"
