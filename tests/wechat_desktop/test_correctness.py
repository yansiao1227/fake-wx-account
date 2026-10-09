"""正确性回归：仅使用控件替身和临时数据库，不触碰真实微信。"""

import threading
import time
import sqlite3
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.config import load_wechat_desktop_config
from channel.wechat_desktop.models import HeaderInfo, WechatDesktopEvent
from channel.wechat_desktop.pipeline.delivery import DeliveryBlocked, DeliveryService
from channel.wechat_desktop.pipeline.materialize import WechatDesktopMaterializeMixin
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.send_control import (
    SendCancelled, mark_send_submitted, send_scope,
)
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.uia.client import WechatUiaClient
from channel.wechat_desktop.uia.gateway import _UiaPriorityCoordinator
from channel.wechat_desktop.uia.controls import _runtime_id
from .helpers import (
    ClickableGeometryControl, GeometryControl,
    _selection_tree,
)


@pytest.fixture
def same_name_sessions(monkeypatch):
    import win32api

    selected = {"id": 1}
    controls = []
    for identity in (1, 2):
        control = ClickableGeometryControl(
            (0, identity * 100, 300, identity * 100 + 60), "同名", "mmui::ChatSessionCell",
            automation_id="session_item_同名",
            on_click=lambda i=identity: selected.update(id=i),
        )
        control.GetRuntimeId = lambda i=identity: (42, i)
        control.GetSelectionItemPattern = lambda i=identity: SimpleNamespace(IsSelected=selected["id"] == i)
        controls.append(control)
    message = GeometryControl((400, 200, 900, 260), "消息", "mmui::ChatTextItemView")
    root, _, _ = _selection_tree("同名", [message], controls[0])
    root._children.append(controls[1])
    client = WechatUiaClient({})
    monkeypatch.setattr(client, "_uia_root", lambda: nullcontext(root))
    monkeypatch.setattr(client, "_paced_wait", lambda *args: None)
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (0, 0))
    monkeypatch.setattr(win32api, "SetCursorPos", lambda point: None)
    return client, controls, selected


def test_same_title_cannot_bypass_missing_runtime_id(same_name_sessions):
    client, controls, _ = same_name_sessions
    assert not client.locate_conversation("同名", "missing", 0)
    assert sum(control.click_count for control in controls) == 0


def test_exact_session_switches_between_identical_titles(same_name_sessions):
    client, controls, selected = same_name_sessions
    assert client.locate_conversation("同名", _runtime_id(controls[1]), 0)
    assert selected["id"] == 2
    assert controls[1].click_count == 1
    # 再次定位精确选中的会话不重复点击，避免微信切换为空白面板。
    assert client.locate_conversation("同名", _runtime_id(controls[1]))
    assert controls[1].click_count == 1


def test_failed_same_title_switch_is_not_accepted(same_name_sessions):
    client, controls, _ = same_name_sessions
    controls[1].on_click = lambda: None
    assert not client.locate_conversation("同名", _runtime_id(controls[1]))


def test_row_index_cannot_disambiguate_same_name_sessions(same_name_sessions):
    client, _, _ = same_name_sessions
    assert not client.locate_conversation("同名", row_index=1)


@pytest.mark.parametrize("known_group", [False, True])
@pytest.mark.parametrize("mode,content,mention,expected", [
    ("all", "普通消息", False, True),
    ("prefix", "/cow 帮忙", False, True),
    ("prefix", "@小牛 帮忙", True, False),
    ("at_only", "/cow 帮忙", False, False),
    ("at_only", "@小牛 帮忙", True, True),
    ("at_or_prefix", "/cow 帮忙", False, True),
    ("at_or_prefix", "@小牛 帮忙", True, True),
    ("at_or_prefix", "普通消息", False, False),
])
def test_group_trigger_modes_use_native_event_metadata(known_group, mode, content, mention, expected):
    config = load_wechat_desktop_config({"group_reply_mode": mode, "auto_reply_groups": ["群"] if known_group else []})
    event = WechatDesktopEvent("message", "db-session:group", "群", "member", "成员", "text", content,
                               is_group=True, is_at=mention, source_type="group")
    assert WechatDesktopPolicy(config, None).group_triggered(event) is expected


@pytest.fixture
def sender(monkeypatch):
    client = WechatUiaClient({"uia_text_chunk_chars": 100})
    sent = []
    monkeypatch.setattr(client, "focus_window", lambda: None)
    monkeypatch.setattr(client, "locate_conversation", lambda *args: True)
    monkeypatch.setattr(client, "get_title", lambda: HeaderInfo("Alice", "private"))
    monkeypatch.setattr(client, "get_send_bubble_snapshot", lambda **kwargs: [])
    monkeypatch.setattr(client, "_clipboard", lambda **kwargs: nullcontext())
    monkeypatch.setattr(client, "_wait_for_send_slot", lambda who: None)

    def submit(expected_text=""):
        mark_send_submitted()
        sent.append(expected_text)

    monkeypatch.setattr(client, "_paste_and_send", submit)
    monkeypatch.setattr(client, "_verify_send", lambda *args, **kwargs: {"success": True, "verified": True})
    state = {"paused": False, "stopped": False, "active": True}
    backend = SimpleNamespace(send_text=client.send_message, send_image=lambda target, path: client.send_file(target, [path]))
    policy = SimpleNamespace(allows_send=lambda *args, **kwargs: True, reserve_send=lambda units: True)
    service = DeliveryService(client.config, policy, backend,
                              lambda: state["paused"], lambda: state["stopped"], lambda token: state["active"])
    return client, service, state, sent


@pytest.mark.parametrize("flag,value", [("paused", True), ("stopped", True), ("active", False)])
@pytest.mark.parametrize("content_type", ["text", "image"])
def test_cancel_during_pacing_never_submits(sender, monkeypatch, tmp_path, flag, value, content_type):
    client, service, state, sent = sender
    monkeypatch.setattr(client, "_wait_for_send_slot", lambda who: state.update({flag: value}))
    path = tmp_path / "image.png"
    path.write_bytes(b"fake image")
    with pytest.raises(DeliveryBlocked):
        service.send("Alice", str(path) if content_type == "image" else "x" * 201,
                     policy_target="Alice", content_type=content_type, token="task")
    assert sent == []


def test_cancel_after_first_chunk_returns_partial_and_does_not_leak_scope(sender, monkeypatch):
    client, service, state, sent = sender
    original = client._verify_send

    def verify(*args, **kwargs):
        state["active"] = False
        return original(*args, **kwargs)

    monkeypatch.setattr(client, "_verify_send", verify)
    result = service.send("Alice", "x" * 201, policy_target="Alice", token="task")
    assert sent == ["x" * 100]
    assert result["status"] == "partial"
    assert result["submitted_chunks"] == result["verified_chunks"] == 1
    assert result["retryable"] is False
    # 上一调用的已过期令牌不能污染独立的后续发送。
    client.send_message("Alice", "new", expedited=True)
    assert sent[-1] == "new"


def test_verification_error_after_submit_is_uncertain_not_retryable(sender, monkeypatch):
    client, service, _, sent = sender

    def fail_verify(*args, **kwargs):
        raise RuntimeError("bubble unreadable")

    monkeypatch.setattr(client, "_verify_send", fail_verify)
    result = service.send("Alice", "hello", policy_target="Alice")
    assert sent == ["hello"]
    assert result["status"] == "uncertain"
    assert result["submitted_chunks"] == 1
    assert result["retryable"] is False


@pytest.mark.parametrize("wait_on", ["send_lock", "uia_lease", "pacing"])
def test_waiting_sender_can_cancel_without_waiting_for_resource(sender, monkeypatch, wait_on):
    client, service, state, sent = sender
    waiting = threading.Event()
    if wait_on == "send_lock":
        resource = client._send_lock
        client._send_lock = SimpleNamespace(
            acquire=lambda **kwargs: waiting.set() or resource.acquire(**kwargs),
            release=resource.release,
        )
    elif wait_on == "uia_lease":
        coordinator = _UiaPriorityCoordinator()

        @contextmanager
        def section():
            waiting.set()
            with coordinator.lease(reply=True):
                yield

        client.uia_section = section
        resource = coordinator.lease(reply=False)
    else:
        resource = nullcontext()
        client._last_send_at = time.monotonic()
        client.config.update(uia_send_interval_ms_min=10000, uia_send_interval_ms_max=10000)
        monkeypatch.setattr(client, "_wait_for_send_slot", lambda who: waiting.set() or WechatUiaClient._wait_for_send_slot(client, who))
    errors = []

    def run():
        try:
            service.send("Alice", "hello", policy_target="Alice")
        except Exception as exc:
            errors.append(exc)

    with resource:
        thread = threading.Thread(target=run)
        thread.start()
        assert waiting.wait(1)
        state["paused"] = True
        thread.join(timeout=1)
        finished_while_locked = not thread.is_alive()
    thread.join(timeout=1)
    assert finished_while_locked
    assert len(errors) == 1 and isinstance(errors[0], DeliveryBlocked)
    assert sent == []


def test_cancellation_before_actual_click_does_not_press_send(monkeypatch):
    import win32api

    button = SimpleNamespace(Click=lambda **kwargs: pytest.fail("clicked after cancellation"))
    monkeypatch.setattr(win32api, "GetCursorPos", lambda: (0, 0))
    monkeypatch.setattr(win32api, "SetCursorPos", lambda point: None)
    cancelled = False

    def check():
        if cancelled:
            raise SendCancelled("cancelled")

    with send_scope(check):
        cancelled = True
        with pytest.raises(SendCancelled):
            WechatUiaClient({})._click_send_button(button)


def test_cancelled_shortcut_always_releases_control_key(monkeypatch):
    client = WechatUiaClient({})
    calls = []
    api = SimpleNamespace(keybd_event=lambda *args: calls.append(args))
    constants = SimpleNamespace(VK_CONTROL=17, KEYEVENTF_KEYUP=2)

    def cancelled(*args):
        raise SendCancelled("paused while Control was pressed")

    monkeypatch.setattr(client, "_paced_wait", cancelled)
    with pytest.raises(SendCancelled):
        client._press_shortcut(api, constants, ord("V"))
    assert calls == [(17, 0, 0, 0), (17, 0, 2, 0)]


@pytest.fixture
def store(tmp_path):
    instance = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    yield instance
    instance._get_connection().close()


def evidence_event(source="", observed_at=1):
    return WechatDesktopEvent("message", "Alice", "Alice", "Alice", "Alice", "file", str(source),
                              evidence_path=str(source), observed_at=observed_at)


@pytest.mark.parametrize("late_materialization", [False, True])
def test_evidence_cleanup_deletes_only_persisted_managed_copy(store, tmp_path, late_materialization):
    source = tmp_path / "original.txt"
    source.write_text("original content")
    event = evidence_event("" if late_materialization else source)
    store.record_event(event)
    event.evidence_path = str(source)
    channel = SimpleNamespace(_store=store)
    WechatDesktopMaterializeMixin._preserve_event_evidence(channel, event)
    managed = Path(event.evidence_path)
    assert managed.read_text() == "original content"
    saved = store._get_connection().execute("SELECT * FROM events").fetchone()
    assert saved["evidence_path"] == str(source)
    assert saved["managed_evidence_path"] == str(managed.resolve())
    store.cleanup()
    assert source.exists()
    assert not managed.exists()


def test_legacy_evidence_is_never_assumed_owned(store, tmp_path):
    source = tmp_path / "legacy.txt"
    source.write_text("keep")
    store.record_event(evidence_event(source))
    store.cleanup()
    assert source.read_text() == "keep"


def test_old_database_adds_managed_path_without_claiming_existing_files(tmp_path):
    source = tmp_path / "old-source.txt"
    source.write_text("keep")
    path = str(tmp_path / "old.sqlite3")
    connection = sqlite3.connect(path)
    with connection:
        connection.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, evidence_path TEXT, observed_at REAL)")
        connection.execute("INSERT INTO events VALUES ('old', ?, 1)", (str(source),))
    connection.close()
    store = WechatDesktopStore(path)
    try:
        saved = store._get_connection().execute("SELECT * FROM events").fetchone()
        assert saved["managed_evidence_path"] == ""
        store.cleanup()
        assert source.exists()
    finally:
        store._get_connection().close()


def test_shared_managed_evidence_survives_until_last_event_expires(store, tmp_path):
    source = tmp_path / "original.txt"
    source.write_text("shared")
    first, second = evidence_event(source), evidence_event(source, time.time())
    second.content = "another reference"
    assert store.record_event(first)
    assert store.record_event(second)
    WechatDesktopMaterializeMixin._preserve_event_evidence(SimpleNamespace(_store=store), first)
    store.set_event_evidence(second.event_id, str(source), first.evidence_path)
    store.cleanup()
    assert Path(first.evidence_path).exists()
    with store._connect() as db:
        db.execute("UPDATE events SET observed_at=1")
    store.cleanup()
    assert not Path(first.evidence_path).exists()
    assert source.exists()


def test_managed_evidence_cannot_escape_owned_directory(store, tmp_path):
    source = tmp_path / "outside.txt"
    source.write_text("keep")
    event = evidence_event(source)
    store.record_event(event)
    with pytest.raises(ValueError, match="inside"):
        store.set_event_evidence(event.event_id, str(source), str(source))
    # 即使旧数据里存在错误登记，清理也必须再次检查边界。
    with store._connect() as db:
        db.execute("INSERT INTO managed_evidence VALUES (?, 1)", (str(source),))
    store.cleanup()
    assert source.exists()


def test_failed_evidence_registration_does_not_leave_orphan_copy(store, tmp_path, monkeypatch):
    source = tmp_path / "original.txt"
    source.write_text("keep")
    event = evidence_event(source)
    store.record_event(event)

    def fail(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store, "set_event_evidence", fail)
    with pytest.raises(RuntimeError):
        WechatDesktopMaterializeMixin._preserve_event_evidence(SimpleNamespace(_store=store), event)
    assert event.evidence_path == str(source)
    assert source.exists()
    assert list(store.evidence_dir.iterdir()) == []


def test_failed_managed_file_deletion_is_retried(store, tmp_path, monkeypatch):
    source = tmp_path / "original.txt"
    source.write_text("keep")
    event = evidence_event(source)
    store.record_event(event)
    WechatDesktopMaterializeMixin._preserve_event_evidence(SimpleNamespace(_store=store), event)
    managed = Path(event.evidence_path)
    original_unlink = Path.unlink

    def blocked(path, *args, **kwargs):
        if path == managed:
            raise PermissionError("file is in use")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", blocked)
        store.cleanup()
    assert managed.exists()
    store.cleanup()
    assert not managed.exists()
    assert source.exists()
