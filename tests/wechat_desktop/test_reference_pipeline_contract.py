"""引用从数据库补文到 Agent 的端到端合成回归，不调用真实 UI 或网络。"""

import sqlite3
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from channel.wechat_desktop.pipeline.prompts import _link_reading_urls
from channel.wechat_desktop.references import reference_requires_uia
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_db_reader import add_message, make_reader, table


@pytest.fixture
def native_channel(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path, shards=2)
    store = WechatDesktopStore(str(tmp_path / "ledger.sqlite3"))
    backend = WechatDatabaseBackend({}, db_reader=reader, store=store)
    backend.observe_events()
    monkeypatch.setattr(backend, "_actions", lambda: pytest.fail("文字和已取得URL不得访问微信UI"))
    captured = []
    channel = SimpleNamespace(
        config={}, _driver=backend, _trace=lambda *args, **kwargs: None,
        _compose_context=lambda _ctype, content, **kwargs: captured.append(content),
    )
    yield backend, reader, store, talker, channel, captured
    backend.close()
    store._get_connection().close()


def receive(native_channel, local_id=2):
    backend, _, store, _, _, _ = native_channel
    status, events = backend.observe_events()
    batch = status["source_batch"]
    store.receive_source_batch(batch)
    backend.acknowledge_events([batch.batch_id])
    return next(event for event in events if event.source_local_id == local_id)


def reference_xml(native_type=49, preview="", svr_id=71):
    return ("<msg><appmsg><type>57</type><title>synthetic question</title><refermsg>"
            f"<type>{native_type}</type><svrid>{svr_id}</svrid><content>{escape(preview)}</content>"
            "</refermsg></appmsg></msg>")


def test_missing_share_url_is_recovered_from_database_and_reaches_generic_tools(native_channel):
    backend, reader, store, talker, channel, captured = native_channel
    share = ("<msg><appmsg><type>5</type><title>synthetic post</title>"
             "<url>https://example.invalid/post?access=synthetic</url></appmsg></msg>")
    add_message(reader.caches["message/message_0.db"], talker, 1, content=share,
                message_type=49, server_id=71, created=100)
    add_message(reader.caches["message/message_1.db"], talker, 2, content=reference_xml(),
                message_type=49, server_id=72, created=101)
    event = receive(native_channel)
    checkpoints = store.get_source_checkpoints(reader.account_id)

    result, count = backend.materialize_event(event)
    assert AgentReplyCoordinator(channel).dispatch(result) is False

    assert count == 0 and result.reference["content_type"] == "share_card"
    assert result.reference["source_message_id"] == "71"
    assert result.reference["source_native_message_id"]
    assert result.reference["fetch_status"] == "pending_tool"
    assert not reference_requires_uia(result.reference)
    assert "https://example.invalid/post?access=synthetic" in captured[0]
    assert "[链接读取要求]" in captured[0] and "navigate" in captured[0]
    assert store.get_source_checkpoints(reader.account_id) == checkpoints
    assert not backend.uia_initialized


def test_reference_text_full_body_replaces_preview_without_using_uia(native_channel):
    backend, reader, _, talker, channel, captured = native_channel
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="synthetic full text with details", server_id=71, created=100)
    add_message(cache, talker, 2, content=reference_xml(1, "synthetic full text"),
                message_type=49, server_id=72, created=101)
    result, count = backend.materialize_event(receive(native_channel))

    AgentReplyCoordinator(channel).dispatch(result)

    assert count == 0 and result.reference["content"] == "synthetic full text with details"
    assert result.reference["preview_content"] == "synthetic full text"
    assert "synthetic full text with details" in captured[0]
    assert not reference_requires_uia(result.reference)


def test_referenced_original_changed_while_queued_stops_agent_reading(native_channel):
    backend, reader, _, talker, channel, captured = native_channel
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1, content="https://example.invalid/original", server_id=71, created=100)
    add_message(cache, talker, 2, content=reference_xml(1), message_type=49, server_id=72, created=101)
    event, _ = backend.materialize_event(receive(native_channel))
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=1',
                     ("https://example.invalid/replaced",))
    cache.changed = True

    assert AgentReplyCoordinator(channel).dispatch(event) is False
    assert captured == []


@pytest.mark.parametrize("when", ["before_materialization", "while_queued"])
def test_plain_text_link_source_is_validated_before_tools(native_channel, when):
    backend, reader, _, talker, channel, captured = native_channel
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 2, content="read https://example.invalid/current", server_id=72)
    event = receive(native_channel)
    if when == "while_queued":
        event, _ = backend.materialize_event(event)
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=2', ("changed source",))
    cache.changed = True
    if when == "before_materialization":
        event, _ = backend.materialize_event(event)
        assert event.attachment_status == "source_invalid"

    assert AgentReplyCoordinator(channel).dispatch(event) is False
    assert captured == []


@pytest.mark.parametrize("reference,requires_uia", [
    ({"content_type": "text", "content": "complete", "resolved": True}, False),
    ({"content_type": "text", "content": "", "resolved": False}, True),
    ({"content_type": "app_message", "content": "card preview", "resolved": False}, True),
    ({"content_type": "unsupported", "content": "", "resolved": False}, True),
    ({"content_type": "image", "resolved": False}, True),
    ({"content_type": "file", "resolved": False}, True),
    ({"content_type": "share_card", "url": "https://example.invalid/post"}, False),
    ({"content_type": "share_card", "browser_content": "error", "browser_status": "error"}, True),
    ({"content_type": "voice", "content": "[语音]", "resolved": False}, False),
])
def test_reference_uia_boundary_is_explicit(reference, requires_uia):
    assert reference_requires_uia(reference) is requires_uia
