"""轻量 UIA 网关的租约、逐段复核与组合兼容测试；全部使用替身。"""

import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.contracts import SendStatus
from channel.wechat_desktop.models import HeaderInfo, OwnerInfo
from channel.wechat_desktop.send_control import (
    SendCancelled, SendNotSubmitted, check_send_allowed, extend_send_scope,
    mark_send_submitted, send_scope,
)
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.gateway import WechatUiaGateway, _UiaPriorityCoordinator
from channel.wechat_desktop.uia.operations import resolve_conversation_selector
from .helpers import FakeClient, row


@pytest.fixture
def gateway_sender(monkeypatch):
    client = WechatUiaClient({"uia_text_chunk_chars": 100})
    submitted = []
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "get_title", lambda: HeaderInfo("合成联系人", "private"))
    monkeypatch.setattr(client, "get_send_bubble_snapshot", lambda **kwargs: [])
    monkeypatch.setattr(client, "_clipboard", lambda **kwargs: nullcontext())
    monkeypatch.setattr(client, "_wait_for_send_slot", lambda who: None)
    monkeypatch.setattr(client, "_verify_send", lambda *args, **kwargs: {"success": True, "verified": True})

    def submit(expected_text=""):
        mark_send_submitted()
        submitted.append(expected_text)

    monkeypatch.setattr(client, "_paste_and_send", submit)
    gateway = WechatUiaGateway(client.config, client=client)
    gateway.bind_target("synthetic:contact", row("合成联系人", runtime_id="synthetic-row"))
    return gateway, client, submitted


def test_gateway_has_no_receiver_driver_state_or_shell_hook():
    gateway = WechatUiaGateway({}, client=FakeClient())
    for field in ("_hook", "_rows", "_message_snapshots", "_unacknowledged_events", "_recent_emitted_identities"):
        assert not hasattr(gateway, field)
    assert gateway.priority._owner is None


def test_account_inspection_uses_passive_client_api_and_reentrant_lease():
    client = SimpleNamespace(
        get_owner_window_process_id=lambda: 42,
        get_owner_info_passive=lambda: OwnerInfo("合成账号", wx_id="wxid_synthetic", source="uia"),
        get_owner_info=lambda: pytest.fail("网关不能打开账号资料"),
    )
    gateway = WechatUiaGateway({}, client=client)
    with gateway.operation():
        assert gateway.inspect_account() == (42, client.get_owner_info_passive())
        assert gateway.reply_pending.is_set()
    assert not gateway.reply_pending.is_set()
    assert gateway.priority._owner is None


def test_account_inspection_refuses_process_change():
    ids = iter((42, 43))
    gateway = WechatUiaGateway({}, client=SimpleNamespace(
        get_owner_window_process_id=lambda: next(ids),
        get_owner_info_passive=lambda: OwnerInfo("合成账号"),
    ))
    with pytest.raises(RuntimeError, match="window changed"):
        gateway.inspect_account()
    assert gateway.priority._owner is None
    assert not gateway.reply_pending.is_set()


@pytest.mark.parametrize("kind", ["text", "image"])
def test_real_client_pacing_is_outside_gateway_lease(gateway_sender, monkeypatch, tmp_path, kind):
    gateway, client, submitted = gateway_sender
    sections = []

    def pacing(who):
        assert gateway.priority._owner is None
        assert not gateway.reply_pending.is_set()
        assert gateway.scan(lambda: "scanner-can-enter") == "scanner-can-enter"

    def validate():
        sections.append(threading.get_ident())
        assert gateway.priority._owner == threading.get_ident()
        assert gateway.reply_pending.is_set()
        # 查询账号/绑定采用同一可重入租约，不再次执行发送验证。
        with gateway.operation():
            assert gateway.read_current_title().title == "合成联系人"

    monkeypatch.setattr(client, "_wait_for_send_slot", pacing)
    if kind == "text":
        result = gateway.send_text("synthetic:contact", "x" * 201, validate=validate)
        assert submitted == ["x" * 100, "x" * 100, "x"]
        assert len(sections) == 3
    else:
        path = tmp_path / "synthetic.png"
        path.write_bytes(b"synthetic image")
        result = gateway.send_image("synthetic:contact", str(path), validate=validate)
        assert submitted == [""]
        assert len(sections) == 1
    assert result.status == SendStatus.SENT
    assert gateway.priority._owner is None
    assert not gateway.reply_pending.is_set()


def test_guard_before_first_submission_is_not_sent(gateway_sender):
    gateway, _, submitted = gateway_sender

    def refuse():
        raise SendNotSubmitted("synthetic binding expired")

    result = gateway.send_text("synthetic:contact", "合成消息", validate=refuse)
    assert result.status == SendStatus.NOT_SENT
    assert submitted == []
    assert not gateway.reply_pending.is_set()
    # 验证回调结束后不会污染下一次独立发送。
    assert gateway.send_interim_text("synthetic:contact", "下一条").status == SendStatus.SENT
    assert submitted == ["下一条"]


def test_guard_after_first_chunk_preserves_partial_and_does_not_resend(gateway_sender):
    gateway, _, submitted = gateway_sender
    checks = []

    def refuse_second():
        checks.append(True)
        if len(checks) == 2:
            raise SendNotSubmitted("synthetic account changed")

    result = gateway.send_text("synthetic:contact", "x" * 201, validate=refuse_second)
    assert result.status == SendStatus.PARTIAL
    assert result.submitted_chunks == result.verified_chunks == 1
    assert result.get("retryable") is False
    assert submitted == ["x" * 100]


def test_missing_bound_identity_never_reaches_client(gateway_sender):
    gateway, _, submitted = gateway_sender
    assert gateway.send_text("synthetic:missing", "不能发送").status == SendStatus.NOT_SENT
    assert submitted == []
    assert gateway.priority._owner is None


def test_simultaneous_senders_keep_their_own_validators():
    gate = threading.Barrier(2)
    calls = []
    results = []

    def send(who, text, **kwargs):
        gate.wait(timeout=2)
        with client.uia_section():
            calls.append(("submitted", text))
        return {"success": True, "verified": True}

    client = SimpleNamespace(uia_section=nullcontext, send_message=send)
    gateway = WechatUiaGateway({}, client=client)
    gateway.bind_target("synthetic:contact", row("合成联系人"))

    def run(text):
        result = gateway.send_text(
            "synthetic:contact", text,
            validate=lambda: calls.append(("validated", text)),
        )
        results.append(result.status)

    threads = [threading.Thread(target=run, args=(text,)) for text in ("甲", "乙")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert results == [SendStatus.SENT, SendStatus.SENT]
    assert calls in [
        [("validated", "甲"), ("submitted", "甲"), ("validated", "乙"), ("submitted", "乙")],
        [("validated", "乙"), ("submitted", "乙"), ("validated", "甲"), ("submitted", "甲")],
    ]


def test_reply_pending_stays_set_while_another_reply_waits():
    gateway = WechatUiaGateway({}, client=FakeClient())
    first_entered, first_release = threading.Event(), threading.Event()
    second_entered, second_release = threading.Event(), threading.Event()

    def first():
        with gateway.operation():
            first_entered.set()
            assert first_release.wait(2)

    def second():
        with gateway.operation():
            second_entered.set()
            assert second_release.wait(2)

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    threads[0].start()
    assert first_entered.wait(1)
    threads[1].start()
    try:
        with gateway.priority._condition:
            assert gateway.priority._condition.wait_for(lambda: gateway.priority._reply_waiters == 1, timeout=1)
        first_release.set()
        assert second_entered.wait(1)
        assert gateway.reply_pending.is_set()
    finally:
        first_release.set()
        second_release.set()
        for thread in threads:
            thread.join(timeout=3)
    assert not gateway.reply_pending.is_set()


def test_waiting_gateway_send_can_cancel_without_releasing_scan(gateway_sender):
    gateway, _, submitted = gateway_sender
    paused = threading.Event()
    errors = []

    def check():
        if paused.is_set():
            raise SendCancelled("synthetic paused")

    def run():
        try:
            with send_scope(check):
                gateway.send_text("synthetic:contact", "合成消息")
        except Exception as exc:
            errors.append(exc)

    with gateway.operation(reply=False):
        thread = threading.Thread(target=run)
        thread.start()
        with gateway.priority._condition:
            assert gateway.priority._condition.wait_for(lambda: gateway.priority._reply_waiters == 1, timeout=1)
        paused.set()
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], SendCancelled)
    assert submitted == []
    assert not gateway.reply_pending.is_set()


def test_gateway_uses_explicit_binding_and_refuses_missing_target():
    client = FakeClient()
    calls = []
    client.send_message = lambda who, text, **kwargs: calls.append((who, kwargs)) or {"success": True, "verified": True}
    gateway = WechatUiaGateway({}, client=client)
    assert isinstance(gateway.priority, _UiaPriorityCoordinator)
    gateway.bind_target("db-session:test", row("原名", runtime_id="one"))
    assert gateway.send_text("db-session:test", "第一条").status == SendStatus.SENT
    gateway.bind_target("db-session:test", row("新名", runtime_id="two"))
    assert gateway.send_text("db-session:test", "第二条").status == SendStatus.SENT
    assert gateway.send_text("db-session:missing", "失效").status == SendStatus.NOT_SENT
    assert calls == [
        ("原名", {"runtime_id": "one", "row_index": 0}),
        ("新名", {"runtime_id": "two", "row_index": 0}),
    ]


def test_explicit_database_binding_takes_priority_over_custom_dynamic_resolver():
    client = FakeClient()
    sends = []
    resolved = []
    dynamic_rows = {"uia-session:synthetic": row("UIA联系人", runtime_id="uia-one")}
    client.send_message = lambda who, text, **kwargs: sends.append((who, kwargs)) or {"success": True, "verified": True}

    def fallback(identity):
        resolved.append(identity)
        assert identity != "db-session:synthetic", "数据库显式绑定不能交给 UIA 缓存猜测"
        return resolve_conversation_selector(dynamic_rows, identity)

    gateway = WechatUiaGateway({}, client=client, selector_resolver=fallback)
    gateway.bind_target("db-session:synthetic", row("数据库联系人", runtime_id="db-one"))
    assert gateway.send_text("db-session:synthetic", "数据库发送").status == SendStatus.SENT
    assert resolved == []
    assert gateway.send_text("uia-session:synthetic", "UIA发送").status == SendStatus.SENT
    dynamic_rows = {"uia-session:synthetic": row("UIA改名", runtime_id="uia-two")}
    assert gateway.send_text("uia-session:synthetic", "UIA更新").status == SendStatus.SENT
    gateway.bind_target("db-session:synthetic", row("数据库改名", runtime_id="db-two"))
    assert gateway.send_text("db-session:synthetic", "数据库更新").status == SendStatus.SENT
    assert set(resolved) == {"uia-session:synthetic"}
    assert sends == [
        ("数据库联系人", {"runtime_id": "db-one", "row_index": 0}),
        ("UIA联系人", {"runtime_id": "uia-one", "row_index": 0}),
        ("UIA改名", {"runtime_id": "uia-two", "row_index": 0}),
        ("数据库改名", {"runtime_id": "db-two", "row_index": 0}),
    ]


@pytest.mark.parametrize("action", [
    "operation", "scan", "send_section", "inspect_account", "list_conversations",
    "read_current_title", "ensure_foreground", "send_text", "send_interim_text", "send_image",
])
def test_closed_gateway_refuses_every_new_ui_path(action):
    calls = []
    client = SimpleNamespace(
        uia_section=nullcontext,
        cancel_waits=lambda: calls.append("cancel"),
        get_owner_window_process_id=lambda: pytest.fail("closed account inspection"),
        get_owner_info_passive=lambda: pytest.fail("closed owner read"),
        get_visible_conversations=lambda: pytest.fail("closed session read"),
        get_title=lambda: pytest.fail("closed title read"),
        ensure_foreground_window=lambda: pytest.fail("closed foreground operation"),
        send_message=lambda *args, **kwargs: pytest.fail("closed message send"),
        send_file=lambda *args, **kwargs: pytest.fail("closed image send"),
    )
    gateway = WechatUiaGateway({}, client=client)
    gateway.bind_target("synthetic:contact", row("合成联系人"))
    gateway.close()
    gateway.close()
    assert gateway.closed
    assert calls == ["cancel"]
    with pytest.raises(SendCancelled, match="gateway is closed"):
        if action in {"operation", "send_section"}:
            with getattr(gateway, action)():
                pytest.fail("closed section entered")
        elif action == "scan":
            gateway.scan(lambda: pytest.fail("closed scan entered"))
        elif action in {"send_text", "send_interim_text", "send_image"}:
            getattr(gateway, action)("synthetic:contact", "合成消息或路径")
        else:
            getattr(gateway, action)()
    assert not gateway.reply_pending.is_set()
    assert gateway.priority._owner is None


def test_explicit_resume_restores_waits_and_ui_access(gateway_sender, monkeypatch):
    gateway, client, submitted = gateway_sender
    calls = []
    original_resume = client.resume_waits

    def resume():
        calls.append("resume")
        original_resume()

    monkeypatch.setattr(client, "resume_waits", resume)
    gateway.close()
    assert client._stop_event.is_set()
    with pytest.raises(SendCancelled):
        gateway.send_interim_text("synthetic:contact", "已关闭")
    gateway.resume()
    assert not gateway.closed
    assert not client._stop_event.is_set()
    assert calls == ["resume"]
    assert gateway.read_current_title().title == "合成联系人"
    # close 清理绑定，显式恢复后重新验证和绑定。
    gateway.bind_target("synthetic:contact", row("合成联系人", runtime_id="restored-row"))
    assert gateway.send_interim_text("synthetic:contact", "显式恢复").status == SendStatus.SENT
    assert submitted == ["显式恢复"]


def test_close_cancels_waiting_expedited_sender_without_waiting_for_active_lease(gateway_sender):
    gateway, _, submitted = gateway_sender
    errors = []

    def run():
        try:
            gateway.send_interim_text("synthetic:contact", "不能绕过关闭")
        except Exception as exc:
            errors.append(exc)

    with gateway.operation(reply=False):
        thread = threading.Thread(target=run)
        thread.start()
        with gateway.priority._condition:
            assert gateway.priority._condition.wait_for(lambda: gateway.priority._reply_waiters == 1, timeout=1)
        gateway.close()
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], SendCancelled)
    assert submitted == []
    assert not gateway.reply_pending.is_set()


def test_close_during_pacing_never_starts_later_ui_section(gateway_sender, monkeypatch):
    gateway, client, submitted = gateway_sender
    pacing, release = threading.Event(), threading.Event()
    errors = []

    def wait(who):
        assert gateway.priority._owner is None
        pacing.set()
        assert release.wait(2)

    def run():
        try:
            gateway.send_text("synthetic:contact", "合成消息")
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(client, "_wait_for_send_slot", wait)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert pacing.wait(1)
        gateway.close()
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], SendCancelled)
    assert submitted == []


def test_gateway_close_requires_explicit_resume_before_ui_access():
    calls = []
    client = FakeClient()
    client.cancel_waits = lambda: calls.append("cancel")
    client.resume_waits = lambda: calls.append("resume")
    gateway = WechatUiaGateway({}, client=client)
    gateway.close()
    assert calls == ["cancel"]
    with pytest.raises(SendCancelled):
        gateway.scan(lambda: pytest.fail("closed gateway accessed UI"))
    assert calls == ["cancel"]
    gateway.resume()
    assert gateway.scan(lambda: "ready") == "ready"
    assert calls == ["cancel", "resume"]


def test_closed_gateway_cannot_rebind_target():
    gateway = WechatUiaGateway({}, client=FakeClient())
    gateway.bind_target("synthetic:contact", row("合成联系人"))
    gateway.close()
    with pytest.raises(SendCancelled, match="gateway is closed"):
        gateway.bind_target("synthetic:contact", row("关闭后重新绑定"))
    assert gateway._bindings == {}


def test_close_and_resume_during_pacing_never_revives_old_sender(gateway_sender, monkeypatch):
    gateway, client, submitted = gateway_sender
    pacing, release = threading.Event(), threading.Event()
    errors = []

    def wait(who):
        pacing.set()
        assert release.wait(2)

    def run():
        try:
            gateway.send_text("synthetic:contact", "旧调用不能复活")
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(client, "_wait_for_send_slot", wait)
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert pacing.wait(1)
        gateway.close()
        gateway.resume()
        gateway.bind_target("synthetic:contact", row("合成联系人", runtime_id="new-generation"))
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], SendCancelled)
    assert "lifecycle changed" in str(errors[0])
    assert submitted == []
    assert gateway.send_interim_text("synthetic:contact", "新的调用").status == SendStatus.SENT
    assert submitted == ["新的调用"]


def test_close_and_resume_never_revives_operation_waiting_for_ui_lease():
    gateway = WechatUiaGateway({}, client=FakeClient())
    entered = []
    errors = []

    def run():
        try:
            with gateway.operation():
                entered.append(True)
        except Exception as exc:
            errors.append(exc)

    with gateway.operation(reply=False):
        thread = threading.Thread(target=run)
        thread.start()
        with gateway.priority._condition:
            assert gateway.priority._condition.wait_for(lambda: gateway.priority._reply_waiters == 1, timeout=1)
        gateway.close()
        gateway.resume()
        thread.join(timeout=1)
        assert not thread.is_alive()
    assert entered == []
    assert len(errors) == 1 and isinstance(errors[0], SendCancelled)
    assert "lifecycle changed" in str(errors[0])
    assert not gateway.reply_pending.is_set()
    assert gateway.scan(lambda: "new-operation") == "new-operation"


def test_resume_without_close_preserves_active_operation_generation(gateway_sender):
    gateway, _, submitted = gateway_sender
    generation = gateway._generation
    with gateway.operation():
        gateway.resume()
        assert gateway._generation == generation
        assert gateway.read_current_title().title == "合成联系人"
        assert gateway.send_interim_text("synthetic:contact", "仍有效").status == SendStatus.SENT
    assert submitted == ["仍有效"]


def test_nested_work_in_old_operation_cannot_capture_resumed_generation(gateway_sender):
    gateway, _, submitted = gateway_sender
    with gateway.operation():
        gateway.close()
        gateway.resume()
        with pytest.raises(SendCancelled, match="lifecycle changed"):
            gateway.read_current_title()
        with pytest.raises(SendCancelled, match="lifecycle changed"):
            gateway.bind_target("synthetic:contact", row("合成联系人"))
        with pytest.raises(SendCancelled, match="lifecycle changed"):
            gateway.send_interim_text("synthetic:contact", "旧嵌套调用")
    assert submitted == []
    gateway.bind_target("synthetic:contact", row("合成联系人"))
    assert gateway.send_interim_text("synthetic:contact", "新代次").status == SendStatus.SENT


def test_close_and_resume_inside_ui_section_refuses_actual_submission(gateway_sender, monkeypatch):
    gateway, client, submitted = gateway_sender

    def submit(expected_text=""):
        assert gateway.priority._owner == threading.get_ident()
        gateway.close()
        gateway.resume()
        mark_send_submitted()
        submitted.append(expected_text)

    monkeypatch.setattr(client, "_paste_and_send", submit)
    with pytest.raises(SendCancelled, match="lifecycle changed"):
        gateway.send_interim_text("synthetic:contact", "同一 UI 段也不能复活")
    assert submitted == []
    assert gateway.priority._owner is None
    assert not gateway.reply_pending.is_set()


def test_gateway_generation_check_keeps_outer_task_cancellation(gateway_sender, monkeypatch):
    gateway, client, submitted = gateway_sender
    cancelled = False
    outer_checks = []

    def outer():
        outer_checks.append(True)
        if cancelled:
            raise SendCancelled("synthetic outer task expired")

    def submit(expected_text=""):
        nonlocal cancelled
        cancelled = True
        mark_send_submitted()
        submitted.append(expected_text)

    monkeypatch.setattr(client, "_paste_and_send", submit)
    with send_scope(outer):
        with pytest.raises(SendCancelled, match="outer task expired"):
            gateway.send_interim_text("synthetic:contact", "外层任务也须校验")
        assert outer_checks
        # Gateway 附加作用域退出后恢复原任务门禁，而不是清空或覆盖它。
        with pytest.raises(SendCancelled, match="outer task expired"):
            check_send_allowed()
    check_send_allowed()
    assert submitted == []


def test_nested_extended_send_scopes_compose_once_and_restore_original():
    calls = []
    with send_scope(lambda: calls.append("outer")):
        with extend_send_scope(lambda: calls.append("first")):
            with extend_send_scope(lambda: calls.append("second")):
                calls.clear()
                check_send_allowed()
                assert calls == ["outer", "first", "second"]
            calls.clear()
            check_send_allowed()
            assert calls == ["outer", "first"]
        calls.clear()
        check_send_allowed()
        assert calls == ["outer"]
    calls.clear()
    check_send_allowed()
    assert calls == []
