"""发送目标定位的空树恢复回归；仅使用替身，不访问真实微信。"""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.binding import DatabaseUiaTargetBinder
from channel.wechat_desktop.contracts import ConversationTarget, TargetStatus
from channel.wechat_desktop.models import HeaderInfo, OwnerInfo
from channel.wechat_desktop.uia.gateway import WechatUiaGateway
from .helpers import FakeClient, row


class RecoveryClient(FakeClient):
    def __init__(self, recovered_rows, *, visible_rows=(), on_recovery=None):
        super().__init__()
        self.rows = list(visible_rows)
        self.recovered_rows = list(recovered_rows)
        self.on_recovery = on_recovery
        self.account_pid = 42
        self.account_wxid = "owner"
        self.account_reads = 0
        self.list_calls = 0
        self.locate_calls = []
        self.headers["alice-row"] = HeaderInfo("Alice", "private")

    def get_owner_info_passive(self):
        self.account_reads += 1
        return OwnerInfo("小牛", self.account_wxid)

    def get_owner_window_handle(self):
        return 123

    def get_owner_window_process_id(self):
        return self.account_pid

    def focus_window(self):
        super().focus_window()
        if self.focus_calls == 1:
            if self.on_recovery is not None:
                self.on_recovery(self)
            self.rows = list(self.recovered_rows)

    def get_visible_conversations(self):
        self.list_calls += 1
        return super().get_visible_conversations()

    def locate_conversation(self, name, runtime_id="", row_index=-1):
        self.locate_calls.append((name, runtime_id, row_index))
        return super().locate_conversation(name, runtime_id, row_index)


def make_recovery_binder(recovered_rows, *, visible_rows=(), on_recovery=None):
    contact = {
        "conversation_id": "db-session:alice", "display_name": "Alice",
        "username": "alice", "is_group": False,
    }
    reader = SimpleNamespace(
        binding=SimpleNamespace(pid=42, wxid="owner"), account_id="account",
        refresh=lambda: None,
        get_contact_by_username=lambda username: {},
        get_contact_by_conversation_id=lambda identity: (
            contact if identity == contact["conversation_id"] else None),
        match_contacts=lambda name: [contact] if name in {"Alice", "alice"} else [],
    )
    source = SimpleNamespace(session_epoch="epoch", reader_session=lambda: nullcontext(reader))
    client = RecoveryClient(recovered_rows, visible_rows=visible_rows, on_recovery=on_recovery)
    gateway = WechatUiaGateway({}, client=client)
    binder = DatabaseUiaTargetBinder(source, lambda: gateway)
    return binder, gateway, client, source


def test_empty_session_tree_recovers_before_target_lookup_and_binds_stable_row():
    recovered = row("Alice", runtime_id="alice-row", row_index=4)
    binder, gateway, client, _ = make_recovery_binder([recovered])

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == TargetStatus.RESOLVED
    assert result.target == ConversationTarget("db-session:alice", "Alice", False)
    assert client.list_calls == 2
    assert client.account_reads >= 2
    assert client.focus_calls == 2  # 恢复一次，定位后仍按原流程复核头部。
    assert client.locate_calls == [("Alice", "alice-row", 4)]
    assert gateway._bindings["db-session:alice"] == recovered


def test_empty_recovered_tree_still_searches_unique_database_target():
    searched_row = row("Alice", runtime_id="alice-row", row_index=4)
    binder, gateway, client, _ = make_recovery_binder([])
    searches = []

    def open_contact(name, **kwargs):
        searches.append((name, kwargs))
        client.rows = [searched_row]
        return True

    gateway.ensure_conversation_visible = open_contact

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == TargetStatus.RESOLVED
    assert result.target == ConversationTarget("db-session:alice", "Alice", False)
    assert searches == [("Alice", {"is_group": False, "expected_pid": 42})]
    assert client.list_calls == 3
    assert client.focus_calls == 2
    assert client.locate_calls == [("Alice", "alice-row", 4)]
    assert gateway._bindings["db-session:alice"] == searched_row


@pytest.mark.parametrize(
    "attribute,value,reason",
    [("account_pid", 43, "account_process_mismatch"),
     ("account_wxid", "another-owner", "account_identity_mismatch")],
)
def test_empty_tree_search_rechecks_account_before_binding(attribute, value, reason):
    binder, gateway, client, _ = make_recovery_binder([])

    def open_contact(*args, **kwargs):
        client.rows = [row("Alice", runtime_id="alice-row")]
        setattr(client, attribute, value)
        return True

    gateway.ensure_conversation_visible = open_contact

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == TargetStatus.STALE
    assert result.reason == reason
    assert client.focus_calls == 1
    assert client.locate_calls == []
    assert gateway._bindings == {}


@pytest.mark.parametrize("explicit_epoch", [True, False])
def test_empty_tree_search_rejects_changed_source_epoch(explicit_epoch):
    binder, gateway, client, source = make_recovery_binder([])

    def open_contact(*args, **kwargs):
        client.rows = [row("Alice", runtime_id="alice-row")]
        source.session_epoch = "new-epoch"
        return True

    gateway.ensure_conversation_visible = open_contact

    result = binder.resolve_target("db-session:alice", **({"session_epoch": "epoch"} if explicit_epoch else {}))

    assert result.status == TargetStatus.STALE
    assert result.reason == "account_binding_changed"
    assert client.focus_calls == 1
    assert client.locate_calls == []
    assert gateway._bindings == {}


def test_empty_tree_search_cannot_replace_existing_runtime_id_binding():
    binder, gateway, client, _ = make_recovery_binder(
        [], visible_rows=[row("Alice", runtime_id="alice-row")],
    )
    assert binder.resolve_target("db-session:alice").status == TargetStatus.RESOLVED
    client.rows = []
    gateway.ensure_conversation_visible = lambda *args, **kwargs: pytest.fail("不可搜索替换既有绑定")

    result = binder.resolve_target("db-session:alice")

    assert result.status == TargetStatus.STALE
    assert result.reason == "已绑定的目标会话不再可见"
    assert len(client.locate_calls) == 1


@pytest.mark.parametrize(
    "recovered_rows,status,reason",
    [([], TargetStatus.STALE, "uia_conversation_list_unavailable"),
     ([row("Bob", runtime_id="bob-row")], TargetStatus.NOT_FOUND,
      "目标会话当前未出现在微信会话列表")],
)
def test_recovered_tree_without_target_stops_after_one_rescan_and_failed_search(recovered_rows, status, reason):
    binder, gateway, client, _ = make_recovery_binder(recovered_rows)
    searches = []

    def search_without_result(name, **kwargs):
        searches.append((name, kwargs))
        return False

    gateway.ensure_conversation_visible = search_without_result

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == status
    assert result.reason == reason
    assert client.focus_calls == 1
    assert client.list_calls == 2
    assert client.locate_calls == []
    assert gateway._bindings == {}
    assert searches == [("Alice", {"is_group": False, "expected_pid": 42})]


@pytest.mark.parametrize(
    "searched_rows,status,reason",
    [([], TargetStatus.STALE, "uia_conversation_list_unavailable"),
     ([row("Bob", runtime_id="bob-row")], TargetStatus.NOT_FOUND,
      "搜索后目标会话仍不可见")],
)
def test_empty_tree_search_requires_rescanned_target(searched_rows, status, reason):
    binder, gateway, client, _ = make_recovery_binder([])

    def search_without_target(*args, **kwargs):
        client.rows = searched_rows
        return True

    gateway.ensure_conversation_visible = search_without_target

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == status
    assert result.reason == reason
    assert client.focus_calls == 1
    assert client.list_calls == 3
    assert client.locate_calls == []
    assert gateway._bindings == {}


def test_recovery_failure_has_explicit_uia_reason_without_binding():
    def fail_recovery(client):
        raise RuntimeError("WeChat UI Automation tree remained empty after 3 recovery attempts")

    binder, gateway, client, _ = make_recovery_binder([], on_recovery=fail_recovery)

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == TargetStatus.STALE
    assert result.reason not in {"目标会话当前未出现在微信会话列表", "target_resolution_failed", ""}
    assert "uia" in result.reason.lower() or "UIA" in result.reason
    assert client.focus_calls == client.list_calls == 1
    assert client.locate_calls == []
    assert gateway._bindings == {}


def test_nonempty_list_with_no_match_remains_passive():
    binder, gateway, client, _ = make_recovery_binder(
        [row("Alice", runtime_id="alice-row")],
        visible_rows=[row("Bob", runtime_id="bob-row")],
    )

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == TargetStatus.NOT_FOUND
    assert client.focus_calls == 0
    assert client.list_calls == 1
    assert client.locate_calls == []
    assert gateway._bindings == {}


@pytest.mark.parametrize(
    "attribute,value,reason",
    [("account_pid", 43, "account_process_mismatch"),
     ("account_wxid", "another-owner", "account_identity_mismatch")],
)
def test_recovery_rechecks_account_before_binding(attribute, value, reason):
    binder, gateway, client, _ = make_recovery_binder(
        [row("Alice", runtime_id="alice-row")],
        on_recovery=lambda client: setattr(client, attribute, value),
    )

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == TargetStatus.STALE
    assert result.reason == reason
    assert client.focus_calls == 1
    assert client.locate_calls == []
    assert gateway._bindings == {}


@pytest.mark.parametrize("explicit_epoch", [True, False])
def test_recovery_rejects_changed_source_epoch(explicit_epoch):
    binder, gateway, client, source = make_recovery_binder([row("Alice", runtime_id="alice-row")])
    client.on_recovery = lambda client: setattr(source, "session_epoch", "new-epoch")

    result = binder.resolve_target("db-session:alice", **({"session_epoch": "epoch"} if explicit_epoch else {}))

    assert result.status == TargetStatus.STALE
    assert result.reason == "account_binding_changed"
    assert client.focus_calls == 1
    assert client.locate_calls == []
    assert gateway._bindings == {}


@pytest.mark.parametrize(
    "recovered_rows,status,reason",
    [([row("Alice", runtime_id="alice-row"), row("Alice", runtime_id="another-row")],
      TargetStatus.AMBIGUOUS, "微信界面存在多个同名会话"),
     ([row("Alice")], TargetStatus.STALE, "目标会话缺少稳定 RuntimeId")],
)
def test_recovered_rows_keep_name_and_runtime_id_guards(recovered_rows, status, reason):
    binder, gateway, client, _ = make_recovery_binder(recovered_rows)

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == status
    assert result.reason == reason
    assert client.focus_calls == 1
    assert client.locate_calls == []
    assert gateway._bindings == {}


def test_recovered_target_still_verifies_conversation_type():
    binder, gateway, client, _ = make_recovery_binder([row("Alice", runtime_id="alice-row")])
    client.headers["alice-row"] = HeaderInfo("Alice", "group")

    result = binder.resolve_target("db-session:alice", session_epoch="epoch")

    assert result.status == TargetStatus.STALE
    assert client.focus_calls == 2
    assert client.locate_calls == [("Alice", "alice-row", 0)]
    assert gateway._bindings == {}


def test_authorization_does_not_create_gateway_or_focus_empty_tree():
    binder, _, client, _ = make_recovery_binder([row("Alice", runtime_id="alice-row")])
    binder._gateway_provider = lambda: pytest.fail("数据库授权不能创建 UIA 网关")

    result = binder.resolve_send_target("db-session:alice")

    assert result.status == TargetStatus.RESOLVED
    assert result.target == ConversationTarget("db-session:alice", "Alice", False)
    assert client.account_reads == client.focus_calls == client.list_calls == 0
