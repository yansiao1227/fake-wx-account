"""暂停恢复与发送阻断诊断回归；只使用临时数据库和发送替身。"""

from types import SimpleNamespace

import pytest

from bridge.reply import Reply, ReplyType
from channel.wechat_desktop.contracts import ConversationTarget, TargetResolution, TargetStatus
from channel.wechat_desktop.models import ReplyTargetValidation, WechatDesktopEvent
from channel.wechat_desktop.storage.service import WechatDesktopService
from channel.wechat_desktop.storage.store import WechatDesktopStore
from common.log import logger
from .test_contracts import make_channel


@pytest.fixture
def store(tmp_path):
    ledger = WechatDesktopStore(str(tmp_path / "pause.sqlite3"))
    yield ledger
    ledger._get_connection().close()


def reply_channel(store):
    sent = []

    def send(target, content, **kwargs):
        sent.append((target, content, kwargs))
        return {"success": True, "verified": True}

    driver = SimpleNamespace(
        resolve_send_target=lambda identity: TargetResolution(
            TargetStatus.RESOLVED, ConversationTarget("alice", "Alice", False)),
        validate_reply_target=lambda event: ReplyTargetValidation(True),
        send_text=send,
        send_image=send,
    )
    return make_channel(store, driver), sent


def reply_context(content="first message"):
    event = WechatDesktopEvent(
        "message", "alice", "Alice", "alice", "Alice", "text", content,
        source_type="private",
    )
    return {
        "msg": SimpleNamespace(event=event),
        "receiver": event.conversation_id,
        "wechat_desktop_source_type": "private",
        "wechat_desktop_source_event_ids": [event.event_id],
    }


@pytest.mark.parametrize("kind", [ReplyType.TEXT, ReplyType.IMAGE, ReplyType.IMAGE_URL])
def test_saved_pause_is_visible_and_resume_restores_delivery(store, monkeypatch, kind):
    warnings, transitions = [], []
    monkeypatch.setattr(logger, "warning", lambda message, *args: warnings.append(message % args))
    monkeypatch.setattr(logger, "info", lambda message, *args: transitions.append(message % args))
    store.set_state("paused", True)
    channel, sent = reply_channel(store)
    context = reply_context()
    reply = Reply(kind, "synthetic reply" if kind == ReplyType.TEXT else "synthetic.png")

    channel._send_reply_impl(reply, context)

    assert sent == []
    assert context["wechat_desktop_queue_terminal"] == "skipped"
    assert any("上次保存的暂停状态" in line for line in warnings)
    assert any("reason=paused" in line for line in warnings)
    audit = store._get_connection().execute(
        "SELECT result,detail FROM audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(audit) == ("blocked", "paused")
    assert store._get_connection().execute("SELECT count(*) FROM rate_events").fetchone()[0] == 0

    assert channel._service.set_paused(False)["paused"] is False
    assert store.get_state("paused") is False
    assert WechatDesktopService(store).status()["paused"] is False
    assert any("接管已恢复" in line and "paused=False" in line for line in transitions)
    next_context = reply_context("message after resume")
    channel._send_reply_impl(reply, next_context)

    assert len(sent) == 1
    assert sent[0][0] == "alice"
    assert sent[0][2]["authorized_target"] == ConversationTarget("alice", "Alice", False)
    assert next_context["wechat_desktop_queue_terminal"] == "completed"


@pytest.mark.parametrize("kind", [ReplyType.TEXT, ReplyType.IMAGE])
def test_rate_blocked_final_reply_records_reason_without_submitting(store, monkeypatch, kind):
    warnings = []
    monkeypatch.setattr(logger, "warning", lambda message, *args: warnings.append(message % args))
    channel, sent = reply_channel(store)
    channel.config["max_send_per_minute"] = 1
    assert channel._policy.reserve_send()
    context = reply_context()

    channel._send_reply_impl(Reply(kind, "synthetic reply"), context)

    assert sent == []
    assert context["wechat_desktop_queue_terminal"] == "skipped"
    assert any("reason=send rate limit exceeded" in line for line in warnings)
    audit = store._get_connection().execute(
        "SELECT result,detail FROM audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(audit) == ("blocked", "send rate limit exceeded")
    assert store._get_connection().execute("SELECT status FROM deliveries").fetchone()[0] == "not_sent"


def test_startup_reports_persisted_pause_instead_of_active(store, monkeypatch):
    from channel.wechat_desktop.pipeline import channel as channel_module

    class IdleThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    messages = []
    store.set_state("paused", True)
    channel, _ = reply_channel(store)
    channel.config["changelog_enabled"] = False
    channel._materialization_active = SimpleNamespace(clear=lambda: None)
    channel.report_startup_success = lambda: None
    monkeypatch.setattr(channel_module.threading, "Thread", IdleThread)
    monkeypatch.setattr(logger, "info", lambda message, *args: messages.append(message % args))

    channel._start_workers()

    assert channel._service.status()["paused"] is True
    assert any("Channel started in paused mode (paused=True shadow_mode=False)" in line for line in messages)
    assert not any("Channel started in active mode" in line for line in messages)
