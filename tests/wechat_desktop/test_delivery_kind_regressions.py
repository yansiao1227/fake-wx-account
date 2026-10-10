"""辅助通知不改写 Agent 终态；仅使用合成账本与发送替身。"""

import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.contracts import SendResult, SendStatus
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_contracts import event, make_channel, service_with_journal, store
from .test_delivery_regressions import _reply_setup


@pytest.mark.parametrize("terminal", ["timeout", "failed"])
@pytest.mark.parametrize("status", [SendStatus.UNVERIFIED, SendStatus.PARTIAL, SendStatus.UNCERTAIN])
def test_finished_progress_preserves_failure_notice(store, monkeypatch, terminal, status):
    message = event()
    store.receive_event(message)
    channel = make_channel(store, SimpleNamespace(end_reply_cycle=lambda: None))
    channel._reply_queue.enqueue(message)
    item = channel._reply_queue.get()
    service = service_with_journal(store, lambda *args: SendResult(status))
    service.send("Alice", "合成工具进度", policy_target="Alice", interim=True,
                 source_event_ids=item.source_event_ids)
    channel._dispatch_message = lambda *args: True
    monkeypatch.setattr(AgentReplyCoordinator, "wait_for_reply", lambda self, item: terminal)
    notices = []
    channel._send_agent_failure_notice = lambda **kwargs: notices.append(kwargs)

    outcome = channel._process_reply_item(item)
    channel._on_reply_worker_finish(item, outcome)

    assert outcome == terminal
    assert store.event_state(message.event_id)["state"] == terminal
    assert [notice["reason"] for notice in notices] == ["reply_timeout" if terminal == "timeout" else "reply_failed"]


def test_tool_progress_and_timeout_notice_are_persisted_as_interim(tmp_path, store, monkeypatch):
    _, _, _, channel, context = _reply_setup(tmp_path, store)
    message = context["msg"].event
    channel.config.update(auto_reply_private_all=True, agent_tool_notice_enabled=True,
                          agent_tool_notice_templates=["正在调用 {tool_name}"],
                          agent_failure_notice_templates=["合成失败提示"])
    channel._reply_queue.enqueue(message)
    item = channel._reply_queue.get()
    context["wechat_desktop_queue_token"] = item.token
    sent = []
    channel._driver.send_interim_text = lambda *args, **kwargs: sent.append((args, kwargs)) or SendResult(SendStatus.UNVERIFIED)
    channel._dispatch_message = lambda *args: channel._send_agent_tool_notice(context, {"tool_name": "web_search"})

    def timeout(self, current):
        channel._reply_queue.expire(current.token)
        return "timeout"

    monkeypatch.setattr(AgentReplyCoordinator, "wait_for_reply", timeout)

    outcome = channel._process_reply_item(item)
    channel._on_reply_worker_finish(item, outcome)

    assert outcome == "timeout"
    assert message.task.failure_notice_sent
    assert len(sent) == 2
    assert all(kwargs["authorized_target"].conversation_id == message.conversation_id for _, kwargs in sent)
    rows = store._get_connection().execute("SELECT kind,status FROM deliveries ORDER BY updated_at").fetchall()
    assert [tuple(row) for row in rows] == [("interim", "unverified"), ("interim", "unverified")]
    assert store.event_state(message.event_id)["state"] == "timeout"
    assert store.delivery_outcome([message.event_id], "timeout") == "timeout"


@pytest.mark.parametrize("status,expected", [(SendStatus.UNVERIFIED, "uncertain"),
                                             (SendStatus.UNCERTAIN, "uncertain"),
                                             (SendStatus.PARTIAL, "partial")])
def test_final_delivery_keeps_terminal_protection_and_no_replay(store, monkeypatch, status, expected):
    message = event()
    store.receive_event(message)
    channel = make_channel(store, SimpleNamespace(end_reply_cycle=lambda: None))
    channel._reply_queue.enqueue(message)
    item = channel._reply_queue.get()
    sent = []
    service = service_with_journal(store, lambda *args: sent.append(args) or SendResult(status))
    service.send("Alice", "合成最终回复", policy_target="Alice", source_event_ids=item.source_event_ids)
    channel._dispatch_message = lambda *args: True
    monkeypatch.setattr(AgentReplyCoordinator, "wait_for_reply", lambda self, item: "timeout")
    channel._send_agent_failure_notice = lambda **kwargs: pytest.fail("已提交最终回复后重复发送失败通知")

    assert channel._process_reply_item(item) == expected
    assert store._get_connection().execute("SELECT kind FROM deliveries").fetchone()[0] == "final"
    reopened = WechatDesktopStore(store.path)
    try:
        previous = service_with_journal(reopened, lambda *args: pytest.fail("重复发送最终回复"))
        assert previous.send("Alice", "合成最终回复", policy_target="Alice",
                             source_event_ids=item.source_event_ids).status == status
    finally:
        reopened._get_connection().close()
    assert len(sent) == 1


@pytest.mark.parametrize("interim", [False, True])
def test_in_flight_delivery_protects_terminal_until_backend_finishes(store, monkeypatch, interim):
    message = event()
    store.receive_event(message)
    channel = make_channel(store, SimpleNamespace(end_reply_cycle=lambda: None))
    channel._reply_queue.enqueue(message)
    item = channel._reply_queue.get()
    entered, release = threading.Event(), threading.Event()
    results = []

    def send(*args):
        entered.set()
        assert release.wait(5), "发送替身未获准退出"
        return SendResult(SendStatus.UNVERIFIED)

    service = service_with_journal(store, send)

    def deliver():
        try:
            results.append(service.send("Alice", "合成发送", policy_target="Alice", interim=interim,
                                        source_event_ids=item.source_event_ids))
        finally:
            store._get_connection().close()

    thread = threading.Thread(target=deliver)
    thread.start()
    try:
        assert entered.wait(5), "发送替身未启动"
        assert store.delivery_outcome(["unrelated-event"], "timeout") == "timeout"
        channel._dispatch_message = lambda *args: True
        monkeypatch.setattr(AgentReplyCoordinator, "wait_for_reply", lambda self, item: "timeout")
        channel._send_agent_failure_notice = lambda **kwargs: pytest.fail("发送尚未结束时发出失败通知")

        outcome = channel._process_reply_item(item)
        channel._on_reply_worker_finish(item, outcome)

        assert outcome == "uncertain"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(results) == 1 and results[0].status == SendStatus.UNVERIFIED
    assert store.event_state(message.event_id)["state"] == "uncertain"
    assert store.delivery_outcome(item.source_event_ids, "timeout") == ("timeout" if interim else "uncertain")


@pytest.mark.parametrize("kind,status,expected", [
    ("interim", "sending", "uncertain"),
    ("interim", "unverified", "interrupted"),
    ("interim", "uncertain", "interrupted"),
    ("interim", "partial", "interrupted"),
    ("final", "sending", "uncertain"),
    ("final", "unverified", "uncertain"),
])
def test_restart_distinguishes_finished_notice_and_unfinished_submission(store, kind, status, expected):
    message = event()
    store.receive_event(message)
    store.set_event_state([message.event_id], "running")
    delivery_id, _ = store.claim_delivery([message.event_id], "Alice", "合成摘要", kind=kind)
    if status != "sending":
        store.finish_delivery(delivery_id, SendResult(SendStatus(status)))
    reopened = WechatDesktopStore(store.path)
    try:
        counts = reopened.recover_interrupted_events()
        assert counts[expected] == 1
        assert reopened.event_state(message.event_id)["state"] == expected
        assert not reopened.receive_event(message).accepted
        delivery_status = reopened._get_connection().execute("SELECT status FROM deliveries").fetchone()[0]
        assert delivery_status == ("uncertain" if status == "sending" else status)
        reopened.recover_interrupted_events()
        assert reopened.event_state(message.event_id)["state"] == expected
    finally:
        reopened._get_connection().close()


def test_interrupted_recovery_keeps_unfinished_interim_protection(store, monkeypatch):
    first, second = event("first source"), event("second source")
    for message in (first, second):
        store.receive_event(message)
        store.set_event_state([message.event_id], "running")
        store.claim_delivery([message.event_id], "Alice", message.event_id, kind="interim")
    original = store.set_event_state

    def fail_second(ids, state, reason=""):
        if ids == [second.event_id]:
            raise RuntimeError("合成恢复中断")
        original(ids, state, reason)

    monkeypatch.setattr(store, "set_event_state", fail_second)
    with pytest.raises(RuntimeError, match="合成恢复中断"):
        store.recover_interrupted_events()
    assert store.event_state(first.event_id)["state"] == "uncertain"
    assert store.event_state(second.event_id)["state"] == "running"
    assert [row[0] for row in store._get_connection().execute("SELECT status FROM deliveries")] == ["sending", "sending"]
    reopened = WechatDesktopStore(store.path)
    try:
        assert reopened.recover_interrupted_events()["uncertain"] == 1
        assert reopened.event_state(first.event_id)["state"] == "uncertain"
        assert reopened.event_state(second.event_id)["state"] == "uncertain"
        assert [row[0] for row in reopened._get_connection().execute("SELECT status FROM deliveries")] == ["uncertain", "uncertain"]
    finally:
        reopened._get_connection().close()


def test_legacy_schema_defaults_to_final_without_replaying_existing_receipt(tmp_path):
    path = tmp_path / "legacy-delivery.sqlite3"
    ids = json.dumps(["legacy-event"])
    digest = hashlib.sha256("text:False:合成最终回复".encode("utf-8")).hexdigest()
    key = hashlib.sha256(json.dumps([ids, "Alice", digest]).encode()).hexdigest()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE deliveries (delivery_id TEXT PRIMARY KEY, event_ids TEXT NOT NULL, "
                   "target TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL, "
                   "result TEXT NOT NULL DEFAULT '{}', updated_at REAL NOT NULL, dedupe_key TEXT NOT NULL DEFAULT '')")
        db.execute("INSERT INTO deliveries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   ("legacy-delivery", ids, "Alice", digest, "unverified",
                    json.dumps(SendResult(SendStatus.UNVERIFIED).to_dict()), 0, key))
    upgraded = WechatDesktopStore(str(path))
    try:
        assert upgraded._get_connection().execute("SELECT kind FROM deliveries").fetchone()[0] == "final"
        assert upgraded.delivery_outcome(["legacy-event"], "timeout") == "uncertain"
        service = service_with_journal(upgraded, lambda *args: pytest.fail("旧账本重复发送"))
        assert service.send("Alice", "合成最终回复", policy_target="Alice",
                            source_event_ids=["legacy-event"]).status == SendStatus.UNVERIFIED
        upgraded.claim_delivery(["new-event"], "Alice", "new-digest", kind="interim")
    finally:
        upgraded._get_connection().close()
    reopened = WechatDesktopStore(str(path))
    try:
        rows = reopened._get_connection().execute("SELECT kind FROM deliveries ORDER BY updated_at").fetchall()
        assert [row[0] for row in rows] == ["final", "interim"]
    finally:
        reopened._get_connection().close()
