"""本地联系人搜索的身份与次数约束；全部替身，不访问真实微信。"""

import sys
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.contracts import TargetStatus
from channel.wechat_desktop.models import HeaderInfo
from channel.wechat_desktop.send_control import SendNotSubmitted
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.operations import WechatConversationSearchOperations
from .helpers import GeometryControl, row
from .test_target_identity_regressions import make_binder


class SearchControl(GeometryControl):
    def __init__(self, *, name="测试群", aid="search_item_测试群", pid=42, runtime=(42, 2)):
        super().__init__((0, 0, 100, 30), name=name, automation_id=aid)
        self.ProcessId = pid
        self.runtime = runtime
        self.clicks = 0
        self.on_click = lambda: None

    def GetRuntimeId(self):
        return self.runtime

    def Click(self):
        self.clicks += 1
        self.on_click()


@pytest.fixture
def search_client(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32api", SimpleNamespace(
        GetCursorPos=lambda: (0, 0), SetCursorPos=lambda value: None))
    changes = []
    pattern = SimpleNamespace(Value="")

    def set_value(value):
        pattern.Value = value
        changes.append(value)

    pattern.SetValue = set_value
    edit = SearchControl(name="搜索", aid="", runtime=(42, 1))
    edit.ControlTypeName = "EditControl"
    edit.GetValuePattern = lambda: pattern
    result = SearchControl()
    unrelated = SearchControl(name="搜索测试群", aid="", runtime=(42, 3))
    root = GeometryControl((0, 0, 500, 500), children=[edit, result, unrelated])
    root.NativeWindowHandle = 123
    root.ProcessId = 42
    client = SimpleNamespace(
        config={"uia_selection_settle_ms": 0}, _stop_event=threading.Event(),
        operation_lock=threading.RLock(), _walk=WechatUiaClient._walk,
        _uia_root=lambda: nullcontext(root),
        get_owner_window_handle=lambda: 123, get_owner_window_process_id=lambda: 42,
        ensure_foreground_window=lambda: None, rows=[], header=HeaderInfo("旧会话", "private"),
    )
    client.get_title = lambda: client.header
    client.get_visible_conversations = lambda: list(client.rows)

    def open_result():
        client.header = HeaderInfo("测试群（2）", "group", 2)
        client.rows = [row("测试群", runtime_id="new-session-row")]

    result.on_click = open_result
    return client, root, edit, result, changes


def search(client):
    return WechatConversationSearchOperations(client).ensure_conversation_visible(
        "测试群", is_group=True, expected_pid=42)


def test_search_opens_only_exact_local_result_and_returns_real_session_row(search_client):
    client, _, _, result, changes = search_client
    assert search(client)
    assert result.clicks == 1
    assert changes == ["测试群", ""]
    assert client.rows[0].runtime_id == "new-session-row"
    assert client.rows[0].runtime_id != "42.2"


@pytest.mark.parametrize("attribute,value", [
    ("AutomationId", ""), ("AutomationId", "search_item_测试群1"),
    ("Name", "测试群（2）"), ("ProcessId", 43), ("runtime", ()),
])
def test_search_rejects_web_fuzzy_other_process_and_unstable_results(search_client, attribute, value):
    client, _, _, result, changes = search_client
    setattr(result, attribute, value)
    assert not search(client)
    assert result.clicks == 0
    assert changes == ["测试群", ""]


def test_search_rejects_multiple_exact_results_even_with_different_runtime_ids(search_client):
    client, root, _, result, _ = search_client
    duplicate = SearchControl(runtime=(42, 4))
    root._children.append(duplicate)
    assert not search(client)
    assert result.clicks == duplicate.clicks == 0


@pytest.mark.parametrize("header", [HeaderInfo("测试群", "private"),
                                  HeaderInfo("测试群", "unknown"),
                                  HeaderInfo("另一个群", "group", 2)])
def test_search_requires_opened_header_name_and_database_type(search_client, header):
    client, _, _, result, changes = search_client
    result.on_click = lambda: setattr(client, "header", header)
    with pytest.raises(SendNotSubmitted, match="header_unverified"):
        search(client)
    assert changes[-1] == ""


def test_search_requires_real_session_list_after_opening(search_client):
    client, _, _, result, _ = search_client
    result.on_click = lambda: setattr(client, "header", HeaderInfo("测试群", "group", 2))
    assert not search(client)


def test_search_prefers_existing_physical_control_click_api(search_client):
    client, _, _, result, _ = search_client
    clicked = []

    def click(control):
        clicked.append(control)
        control.on_click()
        return True

    client._click_control_point = click
    assert search(client)
    assert clicked == [result]
    assert result.clicks == 0


def test_search_refuses_control_without_clickable_bounds(search_client):
    client, _, _, result, changes = search_client
    client._click_control_point = lambda control: False
    with pytest.raises(SendNotSubmitted, match="not_clickable"):
        search(client)
    assert result.clicks == 0
    assert changes[-1] == ""


def test_search_has_three_attempt_budget_when_no_result(search_client, monkeypatch):
    client, root, edit, _, _ = search_client
    root._children = [edit]
    operation = WechatConversationSearchOperations(client)
    reads = []
    monkeypatch.setattr(operation, "_search_result", lambda *args: reads.append(True))
    assert not operation.ensure_conversation_visible("测试群", is_group=True, expected_pid=42)
    assert len(reads) == 3


def test_search_does_not_overwrite_query_changed_during_operation(search_client, monkeypatch):
    client, _, edit, result, changes = search_client
    operation = WechatConversationSearchOperations(client)
    monkeypatch.setattr(operation, "_settle", lambda: edit.GetValuePattern().SetValue("用户新查询"))
    with pytest.raises(SendNotSubmitted, match="query_changed"):
        operation.ensure_conversation_visible("测试群", is_group=True, expected_pid=42)
    assert result.clicks == 0
    assert changes[-1] == "用户新查询"


@pytest.mark.parametrize("attribute,value", [("NativeWindowHandle", 124), ("ProcessId", 43)])
def test_search_never_accepts_auxiliary_or_other_process_root(search_client, attribute, value):
    client, root, _, result, changes = search_client
    setattr(root, attribute, value)
    with pytest.raises(SendNotSubmitted, match="root_unverified"):
        search(client)
    assert result.clicks == 0 and changes == []


def test_binder_searches_missing_unique_contact_then_uses_regular_binding():
    binder, gateway, bound = make_binder("测试群", group=True)
    original_rows = gateway.client.rows
    gateway.client.rows = []
    calls = []

    def open_contact(name, **kwargs):
        calls.append((name, kwargs))
        gateway.client.rows = original_rows
        return True

    gateway.ensure_conversation_visible = open_contact
    assert binder.resolve_target("db-session:alice").status == TargetStatus.RESOLVED
    assert calls == [("测试群", {"is_group": True, "expected_pid": 42})]
    assert bound[0][1].runtime_id == "alice-row"


def test_binder_revalidates_account_after_search_before_binding():
    binder, gateway, bound = make_binder("测试群", group=True)
    original_rows = gateway.client.rows
    gateway.client.rows = []

    def open_contact(*args, **kwargs):
        gateway.client.rows = original_rows
        gateway.client.get_owner_window_process_id = lambda: 43
        return True

    gateway.ensure_conversation_visible = open_contact
    result = binder.resolve_target("db-session:alice")
    assert result.status == TargetStatus.STALE
    assert result.reason == "account_process_mismatch" and bound == []


def test_binder_never_searches_to_replace_existing_runtime_id_binding():
    binder, gateway, bound = make_binder("测试群", group=True)
    assert binder.resolve_target("db-session:alice").status == TargetStatus.RESOLVED
    gateway.client.rows = []
    gateway.ensure_conversation_visible = lambda *args, **kwargs: pytest.fail("不可替换既有绑定")
    assert binder.resolve_target("db-session:alice").status == TargetStatus.STALE
    assert len(bound) == 1


def test_binder_never_searches_ambiguous_database_contact():
    duplicate = {"conversation_id": "db-session:duplicate", "display_name": "测试群",
                 "username": "duplicate", "is_group": True}
    binder, gateway, bound = make_binder("测试群", group=True, extra_contacts=[duplicate])
    gateway.client.rows = []
    gateway.ensure_conversation_visible = lambda *args, **kwargs: pytest.fail("数据库歧义禁止搜索")
    assert binder.resolve_target("db-session:alice").status == TargetStatus.AMBIGUOUS
    assert bound == []
