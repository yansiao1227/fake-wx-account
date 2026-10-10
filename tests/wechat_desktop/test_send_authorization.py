"""主动发送使用后端可信身份授权；合成数据库与 UIA 替身不访问真实微信。"""

import sqlite3
import threading
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.config import load_wechat_desktop_config
from channel.wechat_desktop.contracts import ConversationTarget, SendStatus, TargetResolution, TargetStatus
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.models import HeaderInfo, OwnerInfo
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.pipeline.send import WechatDesktopSendMixin
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import FakeClient, row
from .test_db_backend import Gateway
from .test_db_reader import make_reader


def channel_for(backend, store, **overrides):
    channel = WechatDesktopSendMixin()
    channel.config = load_wechat_desktop_config({"shadow_mode": False, **overrides})
    channel._driver = backend
    channel._store = store
    channel._service = SimpleNamespace(status=lambda: {"paused": False})
    channel._policy = WechatDesktopPolicy(channel.config, store)
    channel._reply_queue = WechatReplyQueue()
    channel._stop_event = threading.Event()
    return channel


@pytest.fixture
def store(tmp_path):
    value = WechatDesktopStore(str(tmp_path / "send-ledger.sqlite3"))
    yield value
    value._get_connection().close()


def database_backend(tmp_path, store, *, group=False):
    reader, talker = make_reader(tmp_path, group=group)
    gateway = Gateway()
    gateway.client.header_type = "group" if group else "private"
    backend = WechatDatabaseBackend({}, db_reader=reader, store=store, uia_gateway=gateway)
    return backend, reader, gateway, reader.conversation_id(talker)


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("identifier", ["stable_id", "display_name", "username"])
def test_database_identifiers_cannot_bypass_display_name_blacklist(tmp_path, store, group, identifier):
    backend, reader, gateway, cid = database_backend(tmp_path, store, group=group)
    contact = reader.get_contact_by_conversation_id(cid)
    conversation = {"stable_id": cid, "display_name": "Synthetic", "username": contact["username"]}[identifier]
    key = "auto_reply_group_blacklist" if group else "auto_reply_private_blacklist"
    channel = channel_for(backend, store, **{key: ["Synthetic"]})

    result = channel._execute_agent_action("send_text", conversation=conversation,
                                           text="合成消息", is_group=not group)

    assert result["status"] == "blocked"
    assert gateway.client.sent == []
    assert gateway.client.ui_calls == []


def test_database_group_cannot_bypass_group_blacklist_by_claiming_private_kind(tmp_path, store):
    backend, _, gateway, cid = database_backend(tmp_path, store, group=True)
    channel = channel_for(backend, store, auto_reply_group_blacklist=["Synthetic"])

    result = channel._execute_agent_action("send_text", conversation=cid, text="合成消息",
                                           is_group=False)

    assert result["status"] == "blocked"
    assert gateway.client.sent == []
    assert gateway.client.ui_calls == []


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("identifier", ["stable_id", "display_name", "username"])
def test_database_opposite_kind_blacklist_allows_same_name_with_trusted_kind(tmp_path, store, group, identifier):
    backend, reader, gateway, cid = database_backend(tmp_path, store, group=group)
    contact = reader.get_contact_by_conversation_id(cid)
    conversation = {"stable_id": cid, "display_name": "Synthetic", "username": contact["username"]}[identifier]
    channel = channel_for(backend, store,
                          auto_reply_private_blacklist=["Synthetic"] if group else [],
                          auto_reply_group_blacklist=[] if group else ["Synthetic"])

    result = channel._execute_agent_action("send_text", conversation=conversation,
                                           text="合成消息", is_group=not group)

    assert result["status"] == "sent", result
    assert gateway.client.sent == [(cid, "合成消息", "row-1")]
    history = store.list_conversation_history(cid, limit=1)
    assert history[0]["conversation_name"] == "Synthetic"
    assert history[0]["source_type"] == ("group" if group else "private")


@pytest.mark.parametrize("failure", ["stale", "ambiguous", "missing"])
def test_database_unverified_identity_never_submits(tmp_path, store, failure):
    backend, reader, gateway, cid = database_backend(tmp_path, store)
    if failure == "stale":
        conversation = "db-session:other-account:unknown"
    elif failure == "missing":
        conversation = "Missing"
    else:
        cache = reader.caches["contact/contact.db"]
        with sqlite3.connect(cache.path) as connection:
            connection.execute("INSERT INTO contact VALUES (?,?,?,?)", ("other-user", "Synthetic", "", ""))
        cache.changed = True
        conversation = cid
    channel = channel_for(backend, store)

    result = channel._execute_agent_action("send_text", conversation=conversation, text="合成消息")

    assert result["status"] == "blocked"
    assert gateway.client.sent == []
    assert gateway.client.ui_calls == []


def test_database_rechecks_authorized_name_immediately_before_submission(tmp_path, store):
    backend, reader, gateway, cid = database_backend(tmp_path, store)
    channel = channel_for(backend, store, auto_reply_private_blacklist=["Blocked"])

    def rename_before_submission():
        cache = reader.caches["contact/contact.db"]
        with sqlite3.connect(cache.path) as connection:
            connection.execute("UPDATE contact SET remark='Blocked' WHERE username='synthetic-user'")
        cache.changed = True
        gateway.client.rows = [replace(gateway.client.rows[0], conversation_title="Blocked")]

    gateway.before_send = rename_before_submission
    result = channel._execute_agent_action("send_text", conversation=cid, text="合成消息")

    assert result["status"] == "failed"
    assert result["delivery"]["status"] == "not_sent"
    assert gateway.client.sent == []


class SendingClient(FakeClient):
    """模拟真实客户端在每个实际发送段进入 UIA 租约。"""

    def __init__(self):
        super().__init__()
        self.uia_section = nullcontext
        self.before_submit = None
        self.sent = []

    def send_message(self, who, text, **kwargs):
        if self.before_submit:
            self.before_submit()
        with self.uia_section():
            self.sent.append((who, text, kwargs))
        return {"success": True, "verified": True}


def database_backend_with_real_gateway(tmp_path, store, *, group=False):
    reader, talker = make_reader(tmp_path, group=group)
    client = SendingClient()
    client.rows = [row("Synthetic", runtime_id="synthetic-row")]
    client.headers["synthetic-row"] = HeaderInfo("Synthetic", "group" if group else "private")
    client.get_owner_window_process_id = lambda: 42
    client.get_owner_window_handle = lambda: 4242
    client.get_owner_info_passive = lambda: OwnerInfo("Owner", "synthetic-owner")
    gateway = WechatUiaGateway({}, client=client)
    backend = WechatDatabaseBackend({}, db_reader=reader, store=store, uia_gateway=gateway)
    return backend, reader, client, reader.conversation_id(talker)


@pytest.mark.parametrize("kind", ["private", "group"])
@pytest.mark.parametrize("identifier", ["Synthetic", "stable_id"])
def test_database_authorized_identity_reaches_real_uia_gateway(tmp_path, store, kind, identifier):
    group = kind == "group"
    backend, _, client, cid = database_backend_with_real_gateway(tmp_path, store, group=group)
    conversation = "Synthetic" if identifier == "Synthetic" else cid
    channel = channel_for(backend, store)
    result = channel._execute_agent_action("send_text", conversation=conversation,
                                           text="合成消息", is_group=not group)
    assert result["status"] == "sent", result
    assert len(client.sent) == 1
    assert client.sent[0][0] == "Synthetic"
    assert client.sent[0][2]["runtime_id"] == "synthetic-row"


@pytest.mark.parametrize("indicator", ["count_label", "title_suffix", "invalid_count_label", "private_title"])
def test_real_uia_header_parser_keeps_group_type_evidence(monkeypatch, indicator):
    controls = [SimpleNamespace(AutomationId="current_chat_name_label",
                                Name="Synthetic(1)" if indicator == "title_suffix" else "Synthetic")]
    if indicator in {"count_label", "invalid_count_label"}:
        controls.append(SimpleNamespace(AutomationId="current_chat_count_label",
                                        Name="unavailable" if indicator == "invalid_count_label" else "1"))
    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_uia_root", lambda: nullcontext(controls))
    monkeypatch.setattr(client, "_walk", lambda root: iter(root))
    header = client.get_title()
    expected = {"count_label": "group", "title_suffix": "unknown",
                "invalid_count_label": "unknown", "private_title": "private"}[indicator]
    assert header.header_type == expected


@pytest.mark.parametrize("title", ["Synthetic(1)", "Synthetic（1）", "Synthetic(25)"])
def test_user_controlled_numeric_title_suffix_cannot_prove_group(monkeypatch, title):
    controls = [SimpleNamespace(AutomationId="current_chat_name_label", Name=title)]
    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_uia_root", lambda: nullcontext(controls))
    monkeypatch.setattr(client, "_walk", lambda root: iter(root))
    header = client.get_title()
    assert header.header_type == "unknown"
    assert header.title == title


def test_one_member_group_requires_independent_count_control(monkeypatch):
    controls = [SimpleNamespace(AutomationId="current_chat_name_label", Name="Synthetic(1)"),
                SimpleNamespace(AutomationId="current_chat_count_label", Name="1")]
    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_uia_root", lambda: nullcontext(controls))
    monkeypatch.setattr(client, "_walk", lambda root: iter(root))

    assert client.get_title() == HeaderInfo("Synthetic", "group", 1)


@pytest.mark.parametrize("change", ["deleted", "name", "identity"])
def test_database_rechecks_identity_inside_real_uia_submission(tmp_path, store, change):
    backend, reader, client, cid = database_backend_with_real_gateway(tmp_path, store)
    channel = channel_for(backend, store, auto_reply_private_blacklist=["Blocked"])
    def change_before_submit():
        if change == "deleted":
            cache = reader.caches["contact/contact.db"]
            with sqlite3.connect(cache.path) as connection:
                connection.execute("DELETE FROM contact WHERE username='synthetic-user'")
            cache.changed = True
        elif change == "name":
            cache = reader.caches["contact/contact.db"]
            with sqlite3.connect(cache.path) as connection:
                connection.execute("UPDATE contact SET remark='Blocked' WHERE username='synthetic-user'")
            cache.changed = True
            client.rows = [row("Blocked", runtime_id="synthetic-row")]
        else:
            client.rows = [row("Synthetic", runtime_id="changed-row")]
    client.before_submit = change_before_submit
    result = channel._execute_agent_action("send_text", conversation=cid, text="合成消息")
    assert result["status"] == "failed"
    assert result["delivery"]["status"] == SendStatus.NOT_SENT
    assert client.sent == []


def test_backend_without_trusted_kind_cannot_authorize_send(store):
    backend = SimpleNamespace(resolve_send_target=lambda name: TargetResolution(
        TargetStatus.RESOLVED, ConversationTarget("opaque-id", "Synthetic")))
    channel = channel_for(backend, store)

    result = channel._execute_agent_action("send_text", conversation="Synthetic", text="合成消息")

    assert result["status"] == "blocked"
    assert result["code"] == "target_unverified"
