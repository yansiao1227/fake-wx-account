"""会话类型与人数后缀的安全回归；全部使用替身，不操作真实微信。"""

import threading
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.binding import DatabaseUiaTargetBinder
from channel.wechat_desktop.contracts import ConversationTarget, SendStatus, TargetStatus
from channel.wechat_desktop.conversation import conversation_titles_match
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.models import HeaderInfo, OwnerInfo
from channel.wechat_desktop.send_control import current_send_target, mark_send_submitted, send_target_scope
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.driver import WechatUiaDriver
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from .helpers import FakeClient, FakeHook, incoming, row


def make_binder(name="Alice", *, group=False, ui_title=None):
    contact = {"conversation_id": "db-session:alice", "display_name": name,
               "username": "alice", "is_group": group}
    reader = SimpleNamespace(
        binding=SimpleNamespace(pid=42, wxid="owner"), account_id="account",
        refresh=lambda: None,
        get_contact_by_username=lambda username: {},
        get_contact_by_conversation_id=lambda identity: contact if identity == contact["conversation_id"] else None,
        match_contacts=lambda title: [contact] if title == contact["username"] or
        conversation_titles_match(title, contact["display_name"]) else [],
    )
    source = SimpleNamespace(session_epoch="epoch", reader_session=lambda: nullcontext(reader))
    bound = []
    client = FakeClient()
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


def make_driver(*, name="Alice", kind="private", known_group=False):
    client = FakeClient()
    client.rows = [row(name, runtime_id="alice-row", mention=known_group)]
    client.headers["alice-row"] = HeaderInfo(name, kind)
    client.histories["alice-row"] = [incoming("请回答", "first")]
    driver = WechatUiaDriver(
        {"bootstrap_existing_messages": True, "group_reply_mode": "all",
         "auto_reply_groups": [name] if known_group else []},
        client=client, shell_hook=FakeHook(),
    )
    return driver, client


@pytest.mark.parametrize("known_group", [False, True])
def test_unknown_scan_header_never_emits_replyable_events_and_retries(known_group):
    driver, client = make_driver(kind="unknown", known_group=known_group)
    _, events = driver.observe_events()
    assert events == []
    assert client.history_calls == []
    assert driver._retry_conversations
    client.headers["alice-row"] = HeaderInfo("Alice", "private")
    client.rows = [replace(client.rows[0], not_read_number=0, mentions_self=False)]
    _, recovered = driver.observe_events()
    assert len(recovered) == 1
    assert recovered[0].source_type == "private"


def test_unknown_reply_monitor_header_never_emits_new_events():
    driver, client = make_driver()
    _, initial = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in initial])
    driver.begin_reply_cycle("Alice", initial[0].conversation_id)
    client.headers["alice-row"] = HeaderInfo("Alice", "unknown")
    client.histories["alice-row"].append(incoming("后续消息", "second"))
    history_calls = len(client.history_calls)
    _, events = driver.observe_events()
    assert events == []
    assert len(client.history_calls) == history_calls


@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("changed_type", ["unknown", "opposite"])
def test_reply_source_revalidation_rejects_unconfirmed_or_changed_type(group, changed_type):
    driver, client = make_driver(kind="group" if group else "private")
    _, events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in events])
    assert len(events) == 1
    kind = "unknown" if changed_type == "unknown" else "private" if group else "group"
    client.headers["alice-row"] = HeaderInfo("Alice", kind)
    assert not driver.validate_reply_target(events[0]).valid


def test_private_reply_source_revalidation_rejects_suffix_title_collision():
    driver, client = make_driver()
    _, events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in events])
    client.headers["alice-row"] = HeaderInfo("Alice(1)", "private")
    assert not driver.validate_reply_target(events[0]).valid


def test_reply_source_revalidation_rejects_event_with_unknown_type():
    driver, _ = make_driver()
    _, events = driver.observe_events()
    driver.acknowledge_events([event.event_id for event in events])
    assert not driver.validate_reply_target(replace(events[0], source_type="unknown")).valid


def test_private_scan_rejects_suffix_title_collision():
    driver, client = make_driver()
    client.headers["alice-row"] = HeaderInfo("Alice(1)", "private")
    assert driver.observe_events()[1] == []
    assert client.history_calls == []


def test_private_send_resolution_rejects_suffix_title_collision():
    driver, client = make_driver()
    driver.observe_events()
    client.headers["alice-row"] = HeaderInfo("Alice(1)", "private")
    assert driver.resolve_send_target("uia-session:alice-row").status == TargetStatus.STALE


@pytest.mark.parametrize("observed,requested", [("Alice", "Alice(1)"),
                                              ("Alice(1)", "Alice"),
                                              ("Alice", "Alice（1）")])
@pytest.mark.parametrize("known_group", [False, True])
def test_private_active_send_resolution_never_strips_requested_name_suffix(observed, requested, known_group):
    driver, _ = make_driver(name=observed, known_group=known_group)
    driver.observe_events()
    resolution = driver.resolve_send_target(requested)
    assert resolution.status != TargetStatus.RESOLVED
    assert resolution.target is None


@pytest.mark.parametrize("requested,identity", [("Alice", "uia-session:alice-row"),
                                               ("Alice(1)", "uia-session:alice-count-row")])
def test_active_send_resolution_prefers_exact_display_name_over_suffix_alias(requested, identity):
    driver, client = make_driver()
    client.rows = [row("Alice", unread=0, runtime_id="alice-row"),
                   row("Alice(1)", unread=0, runtime_id="alice-count-row")]
    client.headers["alice-count-row"] = HeaderInfo("Alice(1)", "private")
    driver.observe_events()
    candidate = driver.resolve_target(requested)
    assert candidate.status == TargetStatus.RESOLVED
    assert candidate.target.conversation_id == identity
    resolution = driver.resolve_send_target(requested)
    assert resolution.status == TargetStatus.RESOLVED
    assert resolution.target == ConversationTarget(identity, requested, False)


def test_private_active_send_resolution_preserves_internal_identity():
    driver, _ = make_driver(name="Alice(1)")
    driver.observe_events()
    resolution = driver.resolve_send_target("uia-session:alice-row")
    assert resolution.status == TargetStatus.RESOLVED
    assert resolution.target == ConversationTarget("uia-session:alice-row", "Alice(1)", False)


@pytest.mark.parametrize("observed,requested", [("Alice", "Alice（9）"), ("Alice(9)", "Alice")])
def test_active_send_resolution_allows_suffix_alias_after_current_group_verification(observed, requested):
    driver, client = make_driver(name=observed, kind="group")
    client.rows = [replace(client.rows[0], not_read_number=0)]
    driver.observe_events()
    assert driver._known_group_keys == set()
    resolution = driver.resolve_send_target(requested)
    assert resolution.status == TargetStatus.RESOLVED
    assert resolution.target == ConversationTarget("uia-session:alice-row", observed, True)


@pytest.mark.parametrize("current_kind", ["private", "unknown"])
def test_active_send_group_suffix_alias_requires_current_group_evidence(current_kind):
    driver, client = make_driver(kind="group")
    driver.observe_events()
    assert driver._known_group_keys == {"uia-session:alice-row"}
    client.headers["alice-row"] = HeaderInfo("Alice", current_kind)
    resolution = driver.resolve_send_target("Alice(9)")
    assert resolution.status != TargetStatus.RESOLVED
    assert resolution.target is None


def test_group_scan_and_source_revalidation_allow_member_count_suffix():
    driver, client = make_driver(kind="group")
    client.headers["alice-row"] = HeaderInfo("Alice（9）", "group", 9)
    _, events = driver.observe_events()
    assert len(events) == 1
    assert events[0].is_group
    assert driver.validate_reply_target(events[0]).valid


@pytest.fixture
def typed_sender(monkeypatch):
    client = WechatUiaClient({"uia_text_chunk_chars": 100})
    submitted = []
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "get_title", lambda: HeaderInfo("Alice", "group"))
    monkeypatch.setattr(client, "get_chat_history", lambda **kwargs: [])
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
