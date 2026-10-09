"""数据库自动回复上下文与引用解析；仅使用合成快照。"""

import builtins
import copy
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.models import ReplyTargetValidation
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from channel.wechat_desktop.pipeline.prompts import _render_event_context_lines
from .helpers import _bare_wechat_channel
from .test_db_reader import add_message, checkpoints, make_reader, table


def test_reply_context_is_before_current_message_and_uses_native_identity(tmp_path):
    reader, talker = make_reader(tmp_path, shards=2)
    reader.config["reply_context_max_messages"] = 3
    first, second = (reader.caches[f"message/message_{index}.db"] for index in range(2))
    add_message(first, talker, 1, content="same body", created=100, sort_seq=1)
    add_message(second, talker, 1, content="same body", created=100, sort_seq=1)
    add_message(first, talker, 2, content="self reply", sender=2, created=101, sort_seq=2)
    add_message(first, talker, 3, content="current", created=102, sort_seq=3)
    add_message(second, talker, 2, content="future", created=103, sort_seq=0)
    reader.refresh()
    event = next(record.event for record in reader.poll_batch(checkpoints(reader), {}).records
                 if record.event and record.event.content == "current")
    assert [item["content"] for item in event.history] == ["same body", "same body", "self reply"]
    assert len({item["source_message_id"] for item in event.history}) == 3
    assert all(item["source_message_id"] != event.source_message_id for item in event.history)
    assert event.history[-1]["direction"] == "outgoing"
    assert all(item["account_id"] == reader.account_id for item in event.history)
    assert all(item["source"] == "wechat_database" for item in event.history)
    _, lines = _render_event_context_lines(event)
    assert lines == ["历史消息: same body", "历史消息: same body", "历史消息: self reply"]


def test_context_never_adds_later_local_id_when_native_clock_rolls_back(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="previous", created=10)
    add_message(cache, talker, 2, content="current", created=30)
    add_message(cache, talker, 3, content="later with earlier clock", created=20)
    reader.refresh()
    event = reader.poll_batch(checkpoints(reader), {}).records[1].event
    assert [item["content"] for item in event.history] == ["previous"]


def test_default_reply_context_is_three_messages_independent_of_history_query(tmp_path):
    reader, talker = make_reader(tmp_path)
    # 按需查询配置不能再作为自动回复的默认上下文配置。
    reader.config["wechat_history_max_messages"] = 1
    cache = reader.caches["message/message_0.db"]
    for local_id in range(1, 9):
        add_message(cache, talker, local_id, content=f"synthetic {local_id}", created=local_id)
    reader.refresh()
    cursors = checkpoints(reader)
    previous = copy.deepcopy(cursors)
    batch = reader.poll_batch(cursors, {})
    current = next(record.event for record in batch.records if record.local_id == 8)

    assert [item["content"] for item in current.history] == ["synthetic 5", "synthetic 6", "synthetic 7"]
    assert len(batch.records) == 8
    assert reader.read_history(talker, limit=6).returned_count == 6
    assert cursors == previous


def test_pagination_same_timestamp_and_shard_ids_keep_context_order(tmp_path):
    reader, talker = make_reader(tmp_path, shards=2, batch_size=1)
    for index in range(2):
        for local_id in range(1, 4):
            add_message(reader.caches[f"message/message_{index}.db"], talker, local_id,
                        content="repeated", created=10, sort_seq=10)
    reader.refresh()
    cursors = checkpoints(reader)
    events = []
    for _ in range(6):
        batch = reader.poll_batch(cursors, {})
        events.append(batch.records[0].event)
        for advance in batch.checkpoints:
            cursors[advance.stream_id]["cursor"] = advance.cursor
    history_ids = [message.source_message_id for message in reader.read_history(talker).messages]
    for event in events:
        position = history_ids.index(event.source_message_id)
        assert [item["source_message_id"] for item in event.history] == history_ids[:position][-3:]
    assert len({event.source_message_id for event in events}) == 6


def test_unreferenced_attachments_are_type_placeholders_and_system_rows_are_absent(tmp_path):
    reader, talker = make_reader(tmp_path, group=True)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="D:/private/synthetic.png", message_type=3, created=1)
    add_message(cache, talker, 2, content="system text", message_type=10000, created=2)
    add_message(cache, talker, 3, content="question", created=3)
    reader.refresh()
    event = reader.poll_batch(checkpoints(reader), {}).records[-1].event
    assert len(event.history) == 1
    assert event.history[0]["content"] == "[image]"
    assert event.history[0]["sender_name"] == "Sender"
    assert "file_path" not in event.history[0]
    assert event.reference == {} and event.content_type == "text"


def test_context_does_not_cross_native_conversation(tmp_path):
    reader, talker = make_reader(tmp_path)
    other = "other-user"
    contact = reader.caches["contact/contact.db"]
    with sqlite3.connect(contact.path) as conn:
        conn.execute("INSERT INTO contact VALUES (?,?,?,?)", (other, "Other", "", ""))
    contact.changed = True
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as conn:
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table(talker),)).fetchone()[0]
        conn.execute(schema.replace(table(talker), table(other)))
    add_message(cache, other, 1, content="unrelated", created=1)
    add_message(cache, talker, 1, content="related", created=2)
    add_message(cache, talker, 2, content="question", created=3)
    reader.refresh()
    event = next(record.event for record in reader.poll_batch(checkpoints(reader), {}).records
                 if record.event and record.event.content == "question")
    assert [item["content"] for item in event.history] == ["related"]


def test_batch_reuses_one_context_query_and_same_connections(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    reader.config["reply_context_max_messages"] = 2
    cache = reader.caches["message/message_0.db"]
    for local_id in range(1, 31):
        add_message(cache, talker, local_id, created=local_id)
    reader.refresh()
    cursors = checkpoints(reader)
    statements = []
    original_read = cache.read

    @contextmanager
    def traced_read():
        with original_read() as conn:
            conn.set_trace_callback(lambda sql: statements.append((id(conn), sql)))
            yield conn

    monkeypatch.setattr(cache, "read", traced_read)
    batch = reader.poll_batch(cursors, {})
    assert len(batch.records) == 30
    assert max(len(record.event.history) for record in batch.records) == 2
    context_queries = [(identity, sql) for identity, sql in statements if "/* reply_context */" in sql]
    live_queries = [(identity, sql) for identity, sql in statements if "WHERE local_id > " in sql]
    assert len(context_queries) == 1
    assert context_queries[0][0] == live_queries[0][0]


def test_backfill_and_history_queries_do_not_create_reply_context_or_consume_cursors(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    add_message(reader.caches["message/message_0.db"], talker, 1)
    reader.refresh()
    cursors = checkpoints(reader)
    previous = copy.deepcopy(cursors)
    batch = reader.poll_batch(cursors, reader.get_highwaters())
    assert batch.records[0].receipt_phase == "offline_backfill"
    assert batch.records[0].event.history == []
    scan_offset = reader._scan_offset
    monkeypatch.setattr(reader, "poll_batch", lambda *args: pytest.fail("history consumed realtime poll"))
    assert reader.read_history(talker).returned_count == 1
    assert cursors == previous and reader._scan_offset == scan_offset


def test_context_dependency_failure_cannot_return_a_batch_or_advance_cursor(tmp_path, monkeypatch):
    import zstandard
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content=zstandard.ZstdCompressor().compress(b"previous"), created=1)
    add_message(cache, talker, 2, content="current", created=2)
    reader.refresh()
    cursors = checkpoints(reader, cursor=1)
    before = copy.deepcopy(cursors)
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "zstandard":
            raise ImportError("synthetic dependency failure")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    with pytest.raises(DatabaseReadError) as error:
        reader.poll_batch(cursors, {})
    assert error.value.code == "dependency_missing"
    assert cursors == before


@pytest.mark.parametrize("native_type,content,kind,preview", [
    (1, "quote", "text", "quote"), (3, "<img />", "image", "[图片]"),
    (34, "<voice />", "voice", "[语音]"), (43, "<video />", "video", "[视频]"),
    (47, "<emoji />", "sticker", "[动画表情]"), (48, "<location />", "location", "[位置]"),
    (49, "<msg><appmsg><type>6</type><title>synthetic.pdf</title></appmsg></msg>", "file", "synthetic.pdf"),
    (99999, "unknown", "unsupported", "unknown"),
])
def test_native_reference_types_are_truthful_and_only_one_layer(tmp_path, native_type, content, kind, preview):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="unrelated old message", created=1)
    payload = (f"<msg><appmsg><type>57</type><title>question</title><refermsg><type>{native_type}</type>"
               f"<content>{escape(content)}</content><svrid>123</svrid></refermsg></appmsg></msg>")
    add_message(cache, talker, 2, content=payload, message_type=49, created=2)
    reader.refresh()
    event = reader.poll_batch(checkpoints(reader), {}).records[-1].event
    assert event.reference["content_type"] == kind
    assert event.reference["content"] == preview
    assert event.reference["resolved"] is (kind == "text")
    assert event.reference["degraded"] is (kind != "text")
    assert event.reference["depth"] == 1 and event.reference["source_message_id"] == "123"
    assert len(event.history) == 1 and event.history[0]["is_reference"]
    assert "unrelated old message" not in str(event.history)


def test_own_and_quoted_share_cards_keep_metadata_without_network_or_false_resolution(tmp_path):
    reader, talker = make_reader(tmp_path)
    share = ("<msg><appmsg><type>5</type><title>Title</title><url>https://synthetic.invalid/article</url>"
             "<des>Description</des><appname>Platform</appname></appmsg></msg>")
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content=share, message_type=49, created=1)
    quoted = ("<msg><appmsg><type>57</type><title>question</title><refermsg><type>49</type>"
              f"<content><![CDATA[{share}]]></content></refermsg></appmsg></msg>")
    add_message(cache, talker, 2, content=quoted, message_type=49, created=2)
    reader.refresh()
    first, second = [record.event for record in reader.poll_batch(checkpoints(reader), {}).records]
    assert first.content_type == "share_card" and first.reference == {}
    assert first.share_card == {"title": "Title", "url": "https://synthetic.invalid/article",
                                "description": "Description", "platform": "Platform"}
    assert second.reference["content_type"] == "share_card"
    assert second.reference["url"] == first.share_card["url"]
    assert second.reference["platform"] == "Platform"
    assert second.reference["resolved"] is False and second.reference["degraded"] is True
    assert "file_path" not in second.reference and "browser_content" not in second.reference


def test_disabling_context_limit_does_not_create_candidate_history(tmp_path):
    reader, talker = make_reader(tmp_path)
    reader.config["reply_context_max_messages"] = 0
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, created=1)
    add_message(cache, talker, 2, created=2)
    reader.refresh()
    assert all(record.event.history == [] for record in reader.poll_batch(checkpoints(reader), {}).records)


def test_native_context_reaches_agent_prompt_without_rereading_ui(tmp_path):
    reader, talker = make_reader(tmp_path)
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="previous context", created=1)
    add_message(cache, talker, 2, content="current question", created=2)
    add_message(cache, talker, 3, content="future should not leak", created=3)
    reader.refresh()
    event = reader.poll_batch(checkpoints(reader), {}).records[1].event
    channel = _bare_wechat_channel()
    channel.config = {}
    channel._driver = SimpleNamespace(validate_reply_target=lambda candidate: ReplyTargetValidation(
        *reader.validate_native_event(candidate)))
    channel._trace = lambda *_args, **_kwargs: None
    captured = []
    channel._compose_context = lambda _ctype, content, **_kwargs: captured.append(content)
    assert AgentReplyCoordinator(channel).dispatch(event) is False
    assert "previous context" in captured[0] and "current question" in captured[0]
    assert "future should not leak" not in captured[0]
    assert "[候选会话上下文，需按关联度筛选]" in captured[0]
