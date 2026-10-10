"""会话类型与人数后缀的安全回归；全部使用替身，不操作真实微信。"""

import sqlite3
import threading
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.binding import DatabaseUiaTargetBinder
from channel.wechat_desktop.contracts import ConversationTarget, SendStatus, TargetStatus
from channel.wechat_desktop.conversation import conversation_titles_match
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.models import HeaderInfo, OwnerInfo
from channel.wechat_desktop.send_control import current_send_target, mark_send_submitted, send_target_scope
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from .helpers import FakeClient, row
from .test_db_reader import add_message, make_reader


def make_binder(name="Alice", *, group=False, ui_title=None, extra_contacts=()):
    contact = {"conversation_id": "db-session:alice", "display_name": name,
               "username": "alice", "is_group": group}
    contacts = [contact, *extra_contacts]
    reader = SimpleNamespace(
        binding=SimpleNamespace(pid=42, wxid="owner"), account_id="account",
        refresh=lambda: None,
        get_contact_by_username=lambda username: {},
        get_contact_by_conversation_id=lambda identity: next(
            (item for item in contacts if identity == item["conversation_id"]), None),
        match_contacts=lambda title: [item for item in contacts if title == item["username"] or
                                      conversation_titles_match(title, item["display_name"])],
    )
    source = SimpleNamespace(session_epoch="epoch", reader_session=lambda: nullcontext(reader))
    bound = []
    client = FakeClient()
    client.get_owner_info_passive = lambda: OwnerInfo("小牛", "owner")
    client.get_owner_window_handle = lambda: 123
    title = ui_title if ui_title is not None else name
    client.rows = [row(title, runtime_id="alice-row")]
    client.headers["alice-row"] = HeaderInfo(title, "group" if group else "private")
    gateway = WechatUiaGateway({}, client=client)
    original_bind = gateway.bind_target

    def bind(identity, selector):
        original_bind(identity, selector)
        bound.append((identity, selector))

    gateway.bind_target = bind
    return DatabaseUiaTargetBinder(source, lambda: gateway), gateway, bound


@pytest.mark.parametrize("name,ui_title", [("Alice", "Alice(1)"), ("Alice(1)", "Alice"),
                                         ("Alice", "Alice（1）")])
def test_database_private_target_never_strips_numeric_suffix(name, ui_title):
    binder, _, bound = make_binder(name, ui_title=ui_title)
    assert binder.resolve_target("db-session:alice").status == TargetStatus.NOT_FOUND
    assert bound == []


def test_database_private_name_lookup_never_strips_numeric_suffix():
    binder, _, bound = make_binder("Alice(1)")
    assert binder.resolve_target("Alice").status == TargetStatus.NOT_FOUND
    assert bound == []


@pytest.mark.parametrize("group", [False, True])
def test_database_exact_target_still_binds(group):
    binder, _, bound = make_binder("Alice(1)", group=group)
    assert binder.resolve_target("db-session:alice").status == TargetStatus.RESOLVED
    assert len(bound) == 1


def test_database_confirmed_group_allows_member_count_suffix():
    binder, _, bound = make_binder("项目群", group=True, ui_title="项目群（9）")
    assert binder.resolve_target("db-session:alice").status == TargetStatus.RESOLVED
    assert len(bound) == 1


@pytest.mark.parametrize("kind", ["private", "unknown"])
def test_database_group_never_binds_suffix_match_without_matching_ui_group(kind):
    binder, gateway, bound = make_binder("Alice", group=True, ui_title="Alice(9)")
    gateway.client.headers["alice-row"] = HeaderInfo("Alice(9)", kind)
    assert binder.resolve_target("db-session:alice").status == TargetStatus.STALE
    assert bound == []


def test_database_binding_never_degrades_without_type_verification_api():
    binder, gateway, bound = make_binder()
    gateway.verify_target = None
    assert binder.resolve_target("db-session:alice").status == TargetStatus.STALE
    assert bound == []


def test_database_current_private_chat_never_strips_numeric_suffix():
    binder, _, _ = make_binder("Alice", ui_title="Alice(1)")
    with pytest.raises(DatabaseReadError):
        binder.current_conversation()


def test_database_current_chat_requires_known_type():
    binder, gateway, _ = make_binder()
    gateway.read_current_title = lambda: HeaderInfo("Alice", "unknown")
    with pytest.raises(DatabaseReadError):
        binder.current_conversation()


@pytest.fixture
def database_receiver(tmp_path, monkeypatch):
    instances = []

    def make(*, name="Alice", group=False, ui_kind="private", ui_title=None):
        directory = tmp_path / str(len(instances))
        directory.mkdir()
        reader, talker = make_reader(directory, group=group)
        contact_cache = reader.caches["contact/contact.db"]
        with sqlite3.connect(contact_cache.path) as connection:
            connection.execute("UPDATE contact SET nick_name=?,remark=? WHERE username=?",
                               (name, name, talker))
        contact_cache.changed = True
        reader.refresh()
        store = WechatDesktopStore(str(directory / "ledger.sqlite3"))
        client = FakeClient()
        client.rows = [row(name, runtime_id="alice-row")]
        client.headers["alice-row"] = HeaderInfo(ui_title or name, ui_kind)
        instance = WechatDatabaseBackend({}, db_reader=reader, store=store, client=client)
        monkeypatch.setattr(instance, "_actions", lambda: pytest.fail("数据库接收和来源验证不得初始化 UIA"))
        instances.append((instance, store))
        assert instance.observe_events()[1] == []
        return instance, reader, store, client, talker

    yield make
    for instance, store in instances:
        instance.close()
        store._get_connection().close()


def receive_message(receiver, local_id=1, content="请回答"):
    instance, reader, store, _, talker = receiver
    add_message(reader.caches["message/message_0.db"], talker, local_id, content=content)
    observation, events = instance.observe_events()
    assert len(events) == 1
    assert store.receive_source_batch(observation["source_batch"])[0].accepted
    instance.acknowledge_events([observation["source_batch"].batch_id])
    return events[0]


@pytest.mark.parametrize("group", [False, True])
def test_database_receipt_uses_native_type_without_known_ui_header(database_receiver, group):
    receiver = database_receiver(group=group, ui_kind="unknown")
    event = receive_message(receiver)
    instance, reader, _, client, talker = receiver
    assert event.source_type == ("group" if group else "private")
    assert event.is_group is group
    assert event.conversation_id == reader.conversation_id(talker)
    assert instance.validate_reply_target(event).valid
    assert not instance.uia_initialized
    assert client.history_calls == [] and client.focus_calls == 0 and client.owner_calls == 0


def test_unknown_ui_header_during_reply_does_not_suppress_native_new_message(database_receiver):
    receiver = database_receiver()
    first = receive_message(receiver)
    instance, _, _, client, _ = receiver
    client.headers["alice-row"] = HeaderInfo("Alice", "unknown")
    second = receive_message(receiver, 2, "后续消息")
    assert second.source_message_id != first.source_message_id
    assert second.source_type == "private"
    assert instance.validate_reply_target(first).valid and instance.validate_reply_target(second).valid
    assert not instance.uia_initialized and client.history_calls == []


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("changed_type", ["unknown", "opposite"])
def test_native_reply_source_revalidation_rejects_unconfirmed_or_changed_type(
        database_receiver, group, changed_type):
    receiver = database_receiver(group=group)
    event = receive_message(receiver)
    instance = receiver[0]
    assert instance.validate_reply_target(event).valid
    kind = "unknown" if changed_type == "unknown" else "private" if group else "group"
    assert not instance.validate_reply_target(replace(event, source_type=kind)).valid
    assert not instance.uia_initialized and receiver[3].history_calls == []


@pytest.mark.parametrize("group", [False, True])
def test_native_reply_source_revalidation_rejects_changed_group_flag(database_receiver, group):
    receiver = database_receiver(group=group)
    event = receive_message(receiver)
    assert not receiver[0].validate_reply_target(replace(event, is_group=not group)).valid
    assert not receiver[0].uia_initialized


def test_private_native_reply_source_rejects_different_suffix_named_conversation(database_receiver):
    receiver = database_receiver()
    event = receive_message(receiver)
    instance, reader, _, client, _ = receiver
    cache = reader.caches["contact/contact.db"]
    with sqlite3.connect(cache.path) as connection:
        connection.execute("INSERT INTO contact VALUES ('alice-count-user','Alice(1)','Alice(1)','')")
    cache.changed = True
    collision = replace(event, conversation_id=reader.conversation_id("alice-count-user"),
                        conversation_name="Alice(1)")
    assert not instance.validate_reply_target(collision).valid
    assert instance.validate_reply_target(event).valid
    assert not instance.uia_initialized and client.history_calls == []


def test_native_reply_source_revalidation_rejects_event_without_source_identity(database_receiver):
    receiver = database_receiver()
    event = receive_message(receiver)
    assert not receiver[0].validate_reply_target(replace(event, source_message_id="")).valid
    assert not receiver[0].validate_reply_target(replace(event, source_stream_id="")).valid
    assert not receiver[0].uia_initialized


def test_private_database_receipt_keeps_native_identity_despite_ui_suffix_collision(database_receiver):
    receiver = database_receiver(ui_title="Alice(1)")
    event = receive_message(receiver)
    instance, reader, _, client, talker = receiver
    assert event.conversation_id == reader.conversation_id(talker)
    assert event.conversation_name == "Alice" and not event.is_group
    assert instance.validate_reply_target(event).valid
    assert not instance.uia_initialized and client.history_calls == []


def test_private_send_binding_rejects_suffix_header_collision():
    binder, gateway, bound = make_binder()
    gateway.client.headers["alice-row"] = HeaderInfo("Alice(1)", "private")
    assert binder.resolve_target("db-session:alice").status == TargetStatus.STALE
    assert bound == []


@pytest.mark.parametrize("observed,requested", [("Alice", "Alice(1)"),
                                              ("Alice(1)", "Alice"),
                                              ("Alice", "Alice（1）")])
@pytest.mark.parametrize("group_blocked", [False, True])
def test_private_database_send_resolution_never_strips_requested_suffix(observed, requested, group_blocked):
    binder, gateway, bound = make_binder(observed)
    gateway.config["auto_reply_group_blacklist"] = [observed] if group_blocked else []
    resolution = binder.resolve_send_target(requested)
    assert resolution.status == TargetStatus.NOT_FOUND
    assert resolution.target is None
    assert bound == [] and gateway.client.focus_calls == 0


@pytest.mark.parametrize("requested,identity", [("Alice", "db-session:alice"),
                                               ("Alice(1)", "db-session:alice-count")])
def test_database_send_resolution_prefers_exact_private_display_name_over_suffix_alias(requested, identity):
    counted = {"conversation_id": "db-session:alice-count", "display_name": "Alice(1)",
               "username": "alice-count", "is_group": False}
    binder, gateway, bound = make_binder(extra_contacts=[counted])
    client = gateway.client
    client.rows.append(row("Alice(1)", unread=0, runtime_id="alice-count-row"))
    client.headers["alice-count-row"] = HeaderInfo("Alice(1)", "private")
    resolution = binder.resolve_send_target(requested)
    assert resolution.status == TargetStatus.RESOLVED
    assert resolution.target == ConversationTarget(identity, requested, False)
    assert bound == [] and client.focus_calls == 0
    candidate = binder.resolve_target(requested)
    assert candidate.status == TargetStatus.RESOLVED
    assert candidate.target == resolution.target
    assert bound[0][0] == identity
    assert bound[0][1].runtime_id == ("alice-row" if requested == "Alice" else "alice-count-row")


def test_private_database_send_resolution_preserves_internal_identity():
    binder, gateway, bound = make_binder("Alice(1)")
    resolution = binder.resolve_send_target("db-session:alice")
    assert resolution.status == TargetStatus.RESOLVED
    assert resolution.target == ConversationTarget("db-session:alice", "Alice(1)", False)
    assert bound == [] and gateway.client.focus_calls == 0


@pytest.mark.parametrize("observed,requested", [("Alice", "Alice（9）"), ("Alice(9)", "Alice")])
def test_database_send_resolution_allows_suffix_alias_only_for_native_group(observed, requested):
    binder, gateway, bound = make_binder(observed, group=True)
    resolution = binder.resolve_send_target(requested)
    assert resolution.status == TargetStatus.RESOLVED
    assert resolution.target == ConversationTarget("db-session:alice", observed, True)
    assert bound == [] and gateway.client.focus_calls == 0
    assert binder.resolve_target(requested).target == resolution.target
    assert len(bound) == 1


@pytest.mark.parametrize("current_kind", ["private", "unknown"])
def test_database_group_suffix_alias_requires_current_ui_group_evidence_before_binding(current_kind):
    binder, gateway, bound = make_binder(group=True)
    assert binder.resolve_send_target("Alice(9)").status == TargetStatus.RESOLVED
    gateway.client.headers["alice-row"] = HeaderInfo("Alice", current_kind)
    resolution = binder.resolve_target("Alice(9)")
    assert resolution.status == TargetStatus.STALE
    assert resolution.target is None and bound == []


def test_group_database_receipt_and_source_validation_ignore_ui_member_count_suffix(database_receiver):
    receiver = database_receiver(group=True, ui_kind="group", ui_title="Alice（9）")
    event = receive_message(receiver)
    assert event.is_group and event.source_type == "group"
    assert receiver[0].validate_reply_target(event).valid
    assert not receiver[0].uia_initialized and receiver[3].history_calls == []


@pytest.fixture
def typed_sender(monkeypatch):
    client = WechatUiaClient({"uia_text_chunk_chars": 100})
    submitted = []
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "get_title", lambda: HeaderInfo("Alice", "group"))
    monkeypatch.setattr(client, "get_chat_history", lambda **kwargs: pytest.fail("发送验证不得调用旧接收历史接口"))
    monkeypatch.setattr(client, "get_send_bubble_snapshot", lambda **kwargs: [])
    monkeypatch.setattr(client, "_clipboard", lambda **kwargs: nullcontext())
    monkeypatch.setattr(client, "_wait_for_send_slot", lambda who: None)
    monkeypatch.setattr(client, "_verify_send", lambda *args, **kwargs: {"success": True, "verified": True})

    def submit(expected_text=""):
        mark_send_submitted()
        submitted.append(expected_text)

    monkeypatch.setattr(client, "_paste_and_send", submit)
    gateway = WechatUiaGateway(client.config, client=client)
    gateway.bind_target("db-session:alice", row("Alice", runtime_id="alice-row"))
    return gateway, client, submitted


@pytest.mark.parametrize("kind", ["text", "image"])
@pytest.mark.parametrize("header_kind", ["private", "unknown"])
def test_sender_rechecks_group_type_after_locating_before_paste(typed_sender, monkeypatch, tmp_path, kind, header_kind):
    gateway, client, submitted = typed_sender
    target = ConversationTarget("db-session:alice", "Alice", True)

    def locate(*args):
        monkeypatch.setattr(client, "get_title", lambda: HeaderInfo("Alice", header_kind))
        return True

    monkeypatch.setattr(client, "locate_conversation", locate)
    if kind == "text":
        result = gateway.send_text(target.conversation_id, "不能发送", validate=lambda: target)
    else:
        path = tmp_path / "synthetic.png"
        path.write_bytes(b"synthetic image")
        result = gateway.send_image(target.conversation_id, str(path), validate=lambda: target)
    assert result.status == SendStatus.NOT_SENT
    assert submitted == []


def test_later_chunk_type_change_keeps_partial_without_resending(typed_sender, monkeypatch):
    gateway, client, submitted = typed_sender
    target = ConversationTarget("db-session:alice", "Alice", True)
    titles = iter((HeaderInfo("Alice", "group"), HeaderInfo("Alice", "private")))
    monkeypatch.setattr(client, "get_title", lambda: next(titles))
    result = gateway.send_text(target.conversation_id, "x" * 201, validate=lambda: target)
    assert result.status == SendStatus.PARTIAL
    assert result.submitted_chunks == 1
    assert submitted == ["x" * 100]


def test_send_target_scope_restores_outer_target_after_exception():
    outer = ConversationTarget("outer", "Alice", True)
    inner = ConversationTarget("inner", "Bob", False)
    assert current_send_target() is None
    with send_target_scope(outer):
        assert current_send_target() is outer
        with pytest.raises(RuntimeError):
            with send_target_scope(inner):
                assert current_send_target() is inner
                raise RuntimeError("synthetic failure")
        assert current_send_target() is outer
    assert current_send_target() is None


def test_concurrent_send_target_scopes_are_isolated():
    barrier = threading.Barrier(2)
    observed = []

    def run(target):
        with send_target_scope(target):
            barrier.wait(timeout=2)
            observed.append(current_send_target())
        assert current_send_target() is None

    targets = [ConversationTarget("one", "Alice", True), ConversationTarget("two", "Bob", False)]
    threads = [threading.Thread(target=run, args=(target,)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert set(observed) == set(targets)
    assert current_send_target() is None
