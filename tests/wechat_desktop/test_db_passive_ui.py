"""数据库预检查和拒绝路径不访问 UI；发送仍严格复核物理目标。"""

import sqlite3
from types import SimpleNamespace

import pytest

from bridge.reply import Reply, ReplyType
from channel.wechat_desktop.contracts import ConversationTarget, SendResult, SendStatus, TargetStatus
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from .test_contracts import make_channel
from .test_db_backend import Client, Gateway
from .test_db_reader import add_message, make_reader


@pytest.fixture
def database_only(tmp_path, monkeypatch):
    reader, talker = make_reader(tmp_path)
    store = WechatDesktopStore(str(tmp_path / "passive-ledger.sqlite3"))
    backend = WechatDatabaseBackend({}, db_reader=reader, store=store)
    monkeypatch.setattr(backend, "_actions", lambda: pytest.fail("无发送时不得初始化 UIA 网关"))
    yield backend, reader, store, talker
    store._get_connection().close()


def test_authorization_and_native_validation_do_not_initialize_ui(database_only):
    backend, reader, store, talker = database_only
    cid = reader.conversation_id(talker)
    for identity in (cid, talker, "Synthetic"):
        result = backend.resolve_send_target(identity)
        assert result.status == TargetStatus.RESOLVED
        assert result.target == ConversationTarget(cid, "Synthetic", False)
    backend.observe_events()
    add_message(reader.caches["message/message_0.db"], talker, 1)
    observation, events = backend.observe_events()
    assert store.receive_source_batch(observation["source_batch"])[0].accepted
    backend.acknowledge_events([observation["source_batch"].batch_id])
    assert backend.validate_reply_target(events[0]).valid
    assert not backend.uia_initialized


@pytest.mark.parametrize("code", ["page_hmac_failed", "account_changed"])
def test_native_validation_preserves_database_failure_without_ui(database_only, monkeypatch, code):
    backend, reader, _, talker = database_only
    backend.observe_events()
    add_message(reader.caches["message/message_0.db"], talker, 1)
    event = backend.observe_events()[1][0]

    def failed_refresh():
        raise DatabaseReadError(code, "合成来源故障")

    monkeypatch.setattr(reader, "refresh", failed_refresh)
    validation = backend.validate_reply_target(event)
    assert not validation.valid and validation.reason == code
    assert not backend.uia_initialized


@pytest.mark.parametrize("failure", ["same_name", "foreign_id", "missing", "health", "account"])
def test_authorization_rejections_keep_reason_and_never_initialize_ui(database_only, monkeypatch, failure):
    backend, reader, _, talker = database_only
    identity = reader.conversation_id(talker)
    expected = TargetStatus.STALE
    if failure == "same_name":
        cache = reader.caches["contact/contact.db"]
        with sqlite3.connect(cache.path) as connection:
            connection.execute("INSERT INTO contact VALUES ('duplicate','Synthetic','','')")
        cache.changed = True
        expected = TargetStatus.AMBIGUOUS
    elif failure == "foreign_id":
        identity = "db-session:foreign-account"
    elif failure == "missing":
        identity, expected = "Missing", TargetStatus.NOT_FOUND
    else:
        code = "page_hmac_failed" if failure == "health" else "account_changed"

        def failed_refresh():
            raise DatabaseReadError(code, "合成读取失败")

        monkeypatch.setattr(reader, "refresh", failed_refresh)
    resolution = backend.resolve_send_target(identity)
    assert resolution.status == expected
    assert resolution.reason
    if failure in {"health", "account"}:
        assert resolution.reason == code
    assert not backend.uia_initialized


@pytest.mark.parametrize("gate", ["shadow", "blacklist", "paused", "rate_limit", "old_receipt"])
@pytest.mark.parametrize("reply_type", [ReplyType.TEXT, ReplyType.IMAGE])
def test_auto_reply_without_new_delivery_never_initializes_ui(database_only, monkeypatch, gate, reply_type):
    backend, reader, store, talker = database_only
    backend.observe_events()
    add_message(reader.caches["message/message_0.db"], talker, 1)
    observation, events = backend.observe_events()
    store.receive_source_batch(observation["source_batch"])
    backend.acknowledge_events([observation["source_batch"].batch_id])
    event = events[0]
    channel = make_channel(store, backend)
    channel.config["auto_send_images"] = True
    context = {"msg": SimpleNamespace(event=event, other_user_id=event.conversation_id,
                                      other_user_nickname=event.conversation_name),
               "receiver": event.conversation_id, "isgroup": False,
               "wechat_desktop_source_type": "private", "wechat_desktop_source_event_ids": [event.event_id]}
    if gate == "shadow":
        channel.config["shadow_mode"] = True
    elif gate == "blacklist":
        channel.config["auto_reply_private_blacklist"] = ["Synthetic"]
    elif gate == "paused":
        channel._service.set_paused(True)
    elif gate == "rate_limit":
        channel.config["max_send_per_minute"] = 1
        assert channel._policy.reserve_send(1)
    else:
        # 先持久化合成回执，然后恢复真实后端；重复回复应在获取 UIA 前复用回执。
        method = "send_image" if reply_type == ReplyType.IMAGE else "send_text"
        with monkeypatch.context() as patched:
            patched.setattr(backend, method, lambda *args, **kwargs: SendResult(
                SendStatus.SENT, chunks=1, submitted_chunks=1, verified_chunks=1))
            channel._deliver(event.conversation_id, "synthetic reply", policy_target="Synthetic",
                             source_event_ids=[event.event_id],
                             content_type="image" if reply_type == ReplyType.IMAGE else "text")
    channel._send_reply_impl(Reply(reply_type, "synthetic reply"), context)
    assert not backend.uia_initialized


@pytest.mark.parametrize("gate", ["shadow", "blacklist", "rate_limit"])
def test_active_send_denied_before_ui_initialization(database_only, gate):
    backend, reader, store, talker = database_only
    channel = make_channel(store, backend)
    if gate == "shadow":
        channel.config["shadow_mode"] = True
    elif gate == "blacklist":
        channel.config["auto_reply_private_blacklist"] = ["Synthetic"]
    else:
        channel.config["max_send_per_minute"] = 1
        assert channel._policy.reserve_send(1)
    result = channel._execute_agent_action("send_text", conversation=reader.conversation_id(talker),
                                           text="synthetic reply")
    assert result["status"] == ("error" if gate == "rate_limit" else "blocked")
    assert not backend.uia_initialized


def test_current_history_uses_passive_account_and_title_only(tmp_path):
    reader, talker = make_reader(tmp_path)
    client = Client()
    gateway = WechatUiaGateway({}, client=client)
    backend = WechatDatabaseBackend({}, db_reader=reader, uia_gateway=gateway)
    history = backend.read_current_chat_history()
    assert history.conversation_id == reader.conversation_id(talker)
    assert client.ui_calls == ["pid", "owner_passive", "pid", "header"]
    assert client.sent == []


def test_real_send_still_refuses_authorization_changed_before_ui_submission(tmp_path):
    reader, talker = make_reader(tmp_path)
    gateway = Gateway()
    backend = WechatDatabaseBackend({}, db_reader=reader, uia_gateway=gateway)
    cid = reader.conversation_id(talker)
    authorized = backend.resolve_send_target(cid).target
    assert gateway.client.ui_calls == []
    cache = reader.caches["contact/contact.db"]
    with sqlite3.connect(cache.path) as connection:
        connection.execute("UPDATE contact SET remark='Renamed' WHERE username=?", (talker,))
    cache.changed = True
    result = backend.send_text(cid, "must not send", authorized_target=authorized)
    assert result.status == SendStatus.NOT_SENT
    assert gateway.client.sent == []
