"""每日更新通知回归，只使用合成群身份和发送替身。"""

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.config import load_wechat_desktop_config
from channel.wechat_desktop.contracts import ConversationTarget, SendResult, SendStatus, TargetResolution, TargetStatus
from channel.wechat_desktop.pipeline.changelog import BEIJING_TIME, DailyChangelogScheduler
from channel.wechat_desktop.pipeline.delivery import DeliveryService
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.storage.store import WechatDesktopStore


DAY = "2026-10-13"


@pytest.fixture
def harness(tmp_path):
    config = load_wechat_desktop_config({"shadow_mode": False, "auto_reply_group_blacklist": [],
                                         "auto_reply_private_blacklist": []})
    ledger = WechatDesktopStore(str(tmp_path / "ledger.sqlite3"))
    policy = WechatDesktopPolicy(config, ledger)
    groups = [ConversationTarget(f"db:account:group-{index}", f"群{index}", True) for index in range(3)]
    runtime = SimpleNamespace(now=datetime(2026, 10, 13, 18, 0, tzinfo=BEIJING_TIME), paused=False)
    sent = []
    backend = SimpleNamespace(
        list_group_conversations=lambda: list(groups),
        resolve_send_target=lambda cid: TargetResolution(TargetStatus.RESOLVED, next(g for g in groups if g.conversation_id == cid)),
    )

    def send(target, message, **kwargs):
        sent.append((target, message, kwargs))
        return SendResult(SendStatus.SENT)

    scheduler = DailyChangelogScheduler(config=config, store=ledger, backend=backend, policy=policy,
                                        send=send, is_paused=lambda: runtime.paused,
                                        stop_event=threading.Event(), clock=lambda: runtime.now,
                                        repository_root=tmp_path)
    directory = tmp_path / "changelogs"
    directory.mkdir()
    path = directory / f"{DAY}.md"
    path.write_text(f"# {DAY}\n\n- 新增每日群通知。\n", encoding="utf-8")
    yield SimpleNamespace(scheduler=scheduler, config=config, store=ledger, backend=backend, policy=policy,
                          runtime=runtime, groups=groups, sent=sent, path=path)
    ledger._get_connection().close()


@pytest.mark.parametrize("hour,minute,expected", [(17, 59, 0), (18, 0, 3), (23, 59, 3)])
def test_beijing_daily_boundary(harness, hour, minute, expected):
    h = harness
    h.runtime.now = datetime(2026, 10, 13, hour, minute, tzinfo=BEIJING_TIME)
    h.scheduler.tick()
    assert len(h.sent) == expected
    assert all("今日更新（2026-10-13）\n- 新增每日群通知。" == row[1] for row in h.sent)


def test_utc_clock_is_converted_to_beijing_date(harness):
    h = harness
    h.runtime.now = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)
    h.scheduler.tick()
    assert len(h.sent) == 3


@pytest.mark.parametrize("body", [None, "", " \n", "# 当天更新\n\n", "# 更新\n<!-- 暂无更新 -->\n"])
def test_missing_or_empty_changelog_never_queries_groups(harness, body):
    h = harness
    if body is None:
        h.path.unlink()
    else:
        h.path.write_text(body, encoding="utf-8")
    h.backend.list_group_conversations = lambda: pytest.fail("empty update should not query groups")
    h.scheduler.tick()
    assert h.sent == []


def test_blacklists_use_only_group_name_and_recheck_current_name(harness):
    h = harness
    h.config["auto_reply_group_blacklist"] = [" 群0 ", " blocked "]
    h.config["auto_reply_private_blacklist"] = ["群2"]
    h.backend.resolve_send_target = lambda cid: TargetResolution(
        TargetStatus.RESOLVED, ConversationTarget(cid, "BlOcKeD" if cid == h.groups[1].conversation_id else "群2", True))
    h.scheduler.tick()
    assert [row[0] for row in h.sent] == [h.groups[2].conversation_id]
    assert h.store.get_changelog_delivery(DAY, h.groups[0].conversation_id) == {}


@pytest.mark.parametrize("gate", ["disabled", "shadow", "paused", "stopped"])
def test_gates_do_not_consume_daily_claim_and_resume_same_evening(harness, gate):
    h = harness
    if gate == "disabled":
        h.config["changelog_enabled"] = False
    elif gate == "shadow":
        h.config["shadow_mode"] = True
    elif gate == "paused":
        h.runtime.paused = True
    else:
        h.scheduler.stop_event.set()
    h.scheduler.tick()
    assert h.sent == []
    assert h.store.get_changelog_delivery(DAY, h.groups[0].conversation_id) == {}
    h.config["changelog_enabled"] = True
    h.config["shadow_mode"] = False
    h.runtime.paused = False
    h.scheduler.stop_event.clear()
    h.scheduler.tick()
    assert len(h.sent) == 3


@pytest.mark.parametrize("status", list(SendStatus))
def test_results_are_not_replayed_after_content_change_and_restart(harness, status):
    h = harness

    def send(*args, **kwargs):
        h.sent.append(args)
        return SendResult(status)

    h.scheduler.send = send
    h.scheduler.tick()
    h.path.write_text("- 后续追加。", encoding="utf-8")
    reopened = WechatDesktopStore(h.store.path)
    try:
        h.scheduler.store = reopened
        h.scheduler.tick()
        assert len(h.sent) == 3
        assert reopened.get_changelog_delivery(DAY, h.groups[0].conversation_id)["status"] == status.value
    finally:
        reopened._get_connection().close()


def test_new_day_sends_only_new_day_and_abandons_unprocessed_old_groups(harness):
    h = harness

    def crossing_midnight(*args, **kwargs):
        h.sent.append(args)
        h.runtime.now = datetime(2026, 10, 14, 0, 0, tzinfo=BEIJING_TIME)
        return SendResult(SendStatus.SENT)

    h.scheduler.send = crossing_midnight
    h.scheduler.tick()
    assert len(h.sent) == 1
    h.scheduler.tick()
    assert len(h.sent) == 1
    h.runtime.now = datetime(2026, 10, 14, 18, 0, tzinfo=BEIJING_TIME)
    h.scheduler.send = lambda *args, **kwargs: h.sent.append(args) or SendResult(SendStatus.SENT)
    h.scheduler.tick()
    assert len(h.sent) == 1  # 昨日文件不追补。
    (h.path.parent / "2026-10-14.md").write_text("- 新一天更新。", encoding="utf-8")
    h.scheduler.tick()
    assert len(h.sent) == 4


def test_rate_limit_defers_without_reusing_blocked_delivery_receipt(harness):
    h = harness
    h.config["max_send_per_minute"] = 1
    h.config["max_send_per_hour"] = 100
    h.backend.send_text = lambda target, text, **kwargs: h.sent.append((target, text, kwargs)) or SendResult(SendStatus.SENT)
    service = DeliveryService(config=h.config, policy=h.policy, backend=h.backend, store=h.store,
                              is_paused=lambda: h.runtime.paused, is_stopped=lambda: h.scheduler.stop_event.is_set(),
                              is_active=lambda token: True)
    h.scheduler.send = service.send
    h.scheduler.tick()
    assert len(h.sent) == 1
    assert h.store.get_changelog_delivery(DAY, h.groups[1].conversation_id) == {}
    with h.store._connect() as db:
        db.execute("UPDATE rate_events SET created_at=created_at-61")
    h.scheduler.tick()
    assert len(h.sent) == 2
    assert h.store.get_changelog_delivery(DAY, h.groups[1].conversation_id)["status"] == "sent"


def test_unknown_exception_retains_claim_but_other_groups_continue_next_tick(harness):
    h = harness

    def broken(*args, **kwargs):
        raise RuntimeError("may have submitted")

    h.scheduler.send = broken
    with pytest.raises(RuntimeError):
        h.scheduler.tick()
    assert h.store.get_changelog_delivery(DAY, h.groups[0].conversation_id)["status"] == "sending"
    h.scheduler.send = lambda *args, **kwargs: h.sent.append(args) or SendResult(SendStatus.SENT)
    h.scheduler.tick()
    assert [row[0] for row in h.sent] == [group.conversation_id for group in h.groups[1:]]


@pytest.mark.parametrize("resolution", [TargetResolution(TargetStatus.NOT_FOUND),
                                      TargetResolution(TargetStatus.RESOLVED, ConversationTarget("other", "私聊", False)),
                                      TargetResolution(TargetStatus.RESOLVED, ConversationTarget("other", "另一群", True))])
def test_untrusted_or_changed_target_is_never_claimed(harness, resolution):
    h = harness
    h.backend.resolve_send_target = lambda cid: resolution
    h.scheduler.tick()
    assert h.sent == []
    assert h.store.get_changelog_delivery(DAY, h.groups[0].conversation_id) == {}


def test_absolute_directory_and_utf8_bom(harness, tmp_path):
    h = harness
    directory = tmp_path / "custom"
    directory.mkdir()
    (directory / f"{DAY}.md").write_text("# 标题\n- 自定义目录更新。", encoding="utf-8-sig")
    h.config["changelog_directory"] = str(directory)
    h.scheduler.tick()
    assert len(h.sent) == 3
    assert "- 自定义目录更新。" in h.sent[0][1]


def test_scheduler_loop_recovers_and_stop_interrupts_wait(harness):
    h = harness
    calls = []

    def tick():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("injected transient failure")
        h.scheduler.stop_event.set()

    h.config["changelog_check_interval_seconds"] = 0.01
    h.scheduler.tick = tick
    worker = threading.Thread(target=h.scheduler.run)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert len(calls) == 2


@pytest.mark.parametrize("overrides", [{"changelog_send_hour": 24}, {"changelog_send_minute": 60},
                                       {"changelog_check_interval_seconds": 0}, {"changelog_directory": ""},
                                       {"changelog_directory": None}, {"changelog_message_template": "没有内容"},
                                       {"changelog_message_template": "{unknown}"}, {"changelog_message_template": "{"}])
def test_invalid_changelog_configuration_rejected(overrides):
    with pytest.raises(ValueError, match="changelog"):
        load_wechat_desktop_config(overrides)


@pytest.mark.parametrize("enabled", [False, True])
def test_channel_owns_scheduler_thread_only_when_enabled(harness, monkeypatch, enabled):
    from channel.wechat_desktop.pipeline import channel as channel_module
    from channel.wechat_desktop.storage.service import WechatDesktopService

    h = harness
    h.config["changelog_enabled"] = enabled
    cls = channel_module.WechatDesktopChannel.__closure__[0].cell_contents
    channel = object.__new__(cls)
    channel.config = h.config
    channel._store = h.store
    channel._service = WechatDesktopService(h.store)
    channel._driver = h.backend
    channel._policy = h.policy
    channel._stop_event = threading.Event()
    channel._materialization_active = threading.Event()
    channel.report_startup_success = lambda: None
    channel._trace = lambda *args: None

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target, self.name, self.started = target, name, False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    monkeypatch.setattr(channel_module.threading, "Thread", FakeThread)
    channel._start_workers()
    assert ("cow-wechat-changelog" in channel._live_workers()) is enabled
    assert len(channel._live_workers()) == (4 if enabled else 3)
    if enabled:
        scheduler = channel._changelog_thread.target.__self__
        assert scheduler.stop_event is channel._stop_event
        assert scheduler.store is channel._store
        assert scheduler.send.__self__ is channel


@pytest.mark.parametrize("stage", ["resolution", "ui_wait"])
def test_midnight_during_resolution_or_send_wait_cancels_before_submission(harness, stage):
    from channel.wechat_desktop.send_control import check_send_allowed

    h = harness
    h.runtime.now = datetime(2026, 10, 13, 23, 59, tzinfo=BEIJING_TIME)
    resolve = h.backend.resolve_send_target

    def resolve_at_midnight(cid):
        target = resolve(cid)
        h.runtime.now = datetime(2026, 10, 14, 0, 0, tzinfo=BEIJING_TIME)
        return target

    def submit(target, message, **kwargs):
        if stage == "ui_wait":
            h.runtime.now = datetime(2026, 10, 14, 0, 0, tzinfo=BEIJING_TIME)
        check_send_allowed()
        h.sent.append((target, message))
        return SendResult(SendStatus.SENT)

    if stage == "resolution":
        h.backend.resolve_send_target = resolve_at_midnight
    h.backend.send_text = submit
    service = DeliveryService(config=h.config, policy=h.policy, backend=h.backend, store=h.store,
                              is_paused=lambda: False, is_stopped=lambda: False, is_active=lambda token: True)
    h.scheduler.send = service.send
    h.scheduler.tick()
    assert h.sent == []
    assert h.store.get_changelog_delivery(DAY, h.groups[0].conversation_id) == {}
