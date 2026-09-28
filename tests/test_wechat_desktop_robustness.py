"""微信通道故障恢复回归；只使用临时数据库和替身，不操作真实微信。"""

import queue
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.contracts import ConversationTarget, TargetResolution, TargetStatus
from channel.wechat_desktop.config import DEFAULT_CONFIG, load_wechat_desktop_config
from channel.wechat_desktop.daily_hot.scheduler import DailyHotScheduler
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.pipeline.channel import WechatDesktopChannel
from channel.wechat_desktop.pipeline.delivery import DeliveryBlocked, DeliveryService
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.storage.service import WechatDesktopService
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.text import split_message_text
from channel.wechat_desktop.pipeline.lifecycle import LifecycleRecorder


def event(text="hello"):
    return WechatDesktopEvent("message", "alice", "Alice", "alice", "Alice", "text", text)


@pytest.fixture
def channel(tmp_path):
    cls = WechatDesktopChannel.__closure__[0].cell_contents
    channel = object.__new__(cls)
    channel.config = load_wechat_desktop_config({
        "shadow_mode": False, "auto_reply_private_all": True,
        "daily_hot_broadcast_groups": ["A", "B"],
        "max_send_per_minute": 10, "max_send_per_hour": 50,
        "worker_join_timeout_seconds": 0.01,
    })
    channel._store = WechatDesktopStore(str(tmp_path / "review.sqlite3"))
    channel._service = WechatDesktopService(channel._store)
    channel._policy = WechatDesktopPolicy(channel.config, channel._store)
    channel._reply_queue = WechatReplyQueue()
    channel._stop_event = threading.Event()
    channel._runtime_lock = threading.RLock()
    channel._runtime_state = "stopped"
    channel._scan_thread = channel._materialize_thread = channel._queue_thread = None
    channel._warmup_thread = channel._daily_hot_scheduler = None
    channel._pending_private_lock = threading.RLock()
    channel._pending_private_batches = {}
    channel._materialize_submit_lock = threading.RLock()
    channel._materialize_queue = queue.Queue(maxsize=1)
    channel._materialization_active = threading.Event()
    channel._lifecycle = LifecycleRecorder()
    channel._scan_count = 0
    channel._trace = lambda *a, **kw: None
    channel.sent = []

    def send(target, content):
        channel.sent.append((target, content))
        return {"success": True, "verified": True}

    channel._driver = SimpleNamespace(
        send_text=send, send_image=send, send_interim_text=send,
        begin_reply_cycle=lambda *a: None, end_reply_cycle=lambda: None,
        close=lambda: None,
        resolve_target=lambda name: TargetResolution(TargetStatus.RESOLVED, ConversationTarget(name, name)),
    )
    yield channel
    channel._stop_event.set()
    channel._store._get_connection().close()


@pytest.mark.parametrize("failure", ["mark_event_processed", "audit", "end_reply_cycle"])
def test_reply_consumer_survives_cleanup_failures(channel, monkeypatch, failure):
    for text in ("one", "two"):
        channel._reply_queue.enqueue(event(text))
    processed = []

    def dispatch(message, token):
        processed.append(message.content)
        return False

    def broken(*args, **kwargs):
        raise RuntimeError("injected failure")

    target = channel._driver if failure == "end_reply_cycle" else channel._store
    monkeypatch.setattr(target, failure, broken)
    channel._dispatch_message = dispatch
    finish = channel._reply_queue.finish

    def finish_and_stop(item, terminal):
        finish(item, terminal)
        if len(processed) == 2:
            channel._stop_event.set()

    channel._reply_queue.finish = finish_and_stop
    channel._consume_reply_queue()
    assert processed == ["one", "two"]
    assert channel._reply_queue.status()["queue_active_event"] == ""
    assert channel._reply_queue._queue.unfinished_tasks == 0


def test_reply_consumer_survives_preparation_exception(channel):
    channel._reply_queue.enqueue(event("bad"))
    channel._reply_queue.enqueue(event("good"))
    seen = []

    def process(item):
        seen.append(item.event.content)
        if item.event.content == "bad":
            raise RuntimeError("before agent dispatch")
        channel._stop_event.set()
        return "completed"

    channel._process_reply_item = process
    channel._consume_reply_queue()
    assert seen == ["bad", "good"]
    assert channel._reply_queue.status()["queue_failed"] == 1
    assert channel._reply_queue.status()["queue_completed"] == 1


def test_queue_capacity_and_expiration(channel):
    channel._reply_queue = WechatReplyQueue(capacity=1, max_wait_seconds=1)
    first, second = event("one"), event("two")
    assert channel._reply_queue.enqueue(first)
    assert channel._reply_queue.enqueue(second).action == "full"
    assert not channel._reply_queue.contains(second.event_id)
    channel._reply_queue._queue.queue[0].enqueued_at -= 2
    item = channel._reply_queue.get()
    assert item.expired and item.terminal == "expired"
    assert not channel._reply_queue.is_active(item.token)
    channel._reply_queue.finish(item, "expired")
    assert channel._reply_queue.enqueue(second)
    assert channel._reply_queue.stop() == [second.event_id]
    assert channel._reply_queue.stop() == []
    assert channel._reply_queue.status()["queue_depth"] == 0
    assert channel._reply_queue._queue.unfinished_tasks == 0
    assert channel._reply_queue.enqueue(event()).action == "stopped"


def test_expired_item_never_reaches_agent(channel):
    channel._reply_queue = WechatReplyQueue(max_wait_seconds=1)
    channel._reply_queue.enqueue(event())
    channel._reply_queue._queue.queue[0].enqueued_at -= 2
    channel._dispatch_message = lambda *a: pytest.fail("expired task reached agent")
    finish = channel._reply_queue.finish

    def finish_and_stop(item, terminal):
        finish(item, terminal)
        channel._stop_event.set()

    channel._reply_queue.finish = finish_and_stop
    channel._consume_reply_queue()
    assert channel._reply_queue.status()["queue_expired"] == 1


def test_materialization_full_queue_does_not_block(channel):
    assert channel._submit_materialization([event("one")])
    assert not channel._submit_materialization([event("two")])
    assert channel._materialize_queue.qsize() == 1
    assert channel._service.status()["materialize_last_rejection"] == "full"


def test_image_notice_and_text_share_quota(channel):
    channel.config["max_send_per_minute"] = 3
    channel._deliver("Alice", "text", policy_target="Alice")
    channel._deliver("Alice", "image.png", policy_target="Alice", content_type="image")
    channel._deliver("Alice", "notice", policy_target="Alice", interim=True)
    with pytest.raises(DeliveryBlocked, match="rate limit"):
        channel._deliver("Alice", "extra.png", policy_target="Alice", content_type="image")
    assert len(channel.sent) == 3


def test_split_text_reserves_all_bubbles_before_sending(channel):
    channel.config.update(uia_text_chunk_chars=100, max_send_per_minute=2)
    with pytest.raises(DeliveryBlocked):
        channel._deliver("Alice", "x" * 201, policy_target="Alice")
    assert channel.sent == []
    channel._deliver("Alice", "x" * 200, policy_target="Alice")
    with pytest.raises(DeliveryBlocked):
        channel._deliver("Alice", "one more", policy_target="Alice")


def test_split_boundaries_do_not_exceed_limit():
    for text in ("a" * 99 + "\n\n" + "b" * 99, "a" * 100 + "。" + "b" * 100):
        assert all(len(chunk) <= 100 for chunk in split_message_text(text, 100))


def test_policy_rejections_do_not_consume_quota(channel):
    channel.config["max_send_per_minute"] = 1
    channel._service.set_paused(True)
    with pytest.raises(DeliveryBlocked):
        channel._deliver("Alice", "paused", policy_target="Alice")
    channel._service.set_paused(False)
    channel._deliver("Alice", "accepted", policy_target="Alice")
    assert len(channel.sent) == 1


def test_history_preserves_distinct_event_identity(channel):
    args = ("alice", "Alice", "Alice", "incoming", "text", "好的")
    store = channel._store
    assert store.append_conversation_history(*args, source_event_id="a", created_at=1000)
    assert store.append_conversation_history(*args, source_event_id="b", created_at=1010)
    assert not store.append_conversation_history(*args, source_event_id="a", created_at=1020)
    assert not store.append_conversation_history(*args, created_at=1020)
    store.deduplicate_conversation_history()
    assert len(store.list_conversation_history("alice")) == 2


def test_baseline_history_does_not_match_arbitrarily_later_records(channel):
    args = ("alice", "Alice", "Alice", "incoming", "text", "same")
    assert channel._store.append_conversation_history(*args, created_at=2000)
    assert channel._store.append_conversation_history(*args, created_at=1000)


def test_scheduler_retries_enqueue_with_cached_content(channel):
    channel.config["daily_hot_broadcast_enabled"] = True
    attempts, preparations = [], []

    def enqueue(message, fire_date):
        assert fire_date == "2026-09-20"
        attempts.append(message)
        if len(attempts) == 1:
            raise RuntimeError("queue unavailable")
        return 1

    scheduler = DailyHotScheduler(
        config=channel.config, store=channel._store, enqueue_callback=enqueue,
        prepare_factory=lambda **kw: preparations.append(1) or {"ok": True, "message": "ready"},
        now_factory=lambda: datetime(2026, 9, 20, 19),
    )
    scheduler._tick()
    assert scheduler.wait_prepare()
    assert not channel._store.get_state("daily_hot_last_date")
    scheduler._tick()
    assert scheduler.wait_prepare()
    assert attempts == ["ready", "ready"]
    assert preparations == [1]
    assert channel._store.get_state("daily_hot_last_date") == "2026-09-20"


def test_broadcast_partial_enqueue_retries_only_remaining_target(channel):
    channel._reply_queue = WechatReplyQueue(capacity=1)
    with pytest.raises(RuntimeError, match="retry remaining"):
        channel.enqueue_daily_hot_broadcast("news", "2026-09-20")
    first = channel._reply_queue.get()
    assert channel._send_precomposed_reply(first) == "completed"
    channel._reply_queue.finish(first, "completed")
    assert channel.enqueue_daily_hot_broadcast("changed", "2026-09-20") == 1
    second = channel._reply_queue.get()
    assert channel._send_precomposed_reply(second) == "completed"
    channel._reply_queue.finish(second, "completed")
    assert channel.sent == [("A", "news"), ("B", "news")]
    assert channel.enqueue_daily_hot_broadcast("news", "2026-09-20") == 0


def test_broadcast_recovers_queued_jobs_but_not_uncertain_send(channel):
    channel.enqueue_daily_hot_broadcast("news", "2026-09-20")
    first = channel._reply_queue.get()
    channel._store.set_broadcast_status("2026-09-20", first.event.task.broadcast_target, "sending")
    channel._reply_queue = WechatReplyQueue()  # 模拟进程重启后内存队列丢失。
    assert channel.enqueue_daily_hot_broadcast("news", "2026-09-20") == 1
    recovered = channel._reply_queue.get()
    assert recovered.event.conversation_name != first.event.conversation_name


def test_runtime_refuses_restart_until_old_worker_exits(channel):
    release = threading.Event()
    worker = threading.Thread(target=release.wait, name="slow-materializer")
    channel._materialize_thread = worker
    worker.start()
    old_queue, old_stop = channel._reply_queue, channel._stop_event
    try:
        channel.stop()
        assert channel._runtime_state == "stopping"
        with pytest.raises(RuntimeError, match="still stopping"):
            channel.startup()
        assert channel._reply_queue is old_queue
        assert channel._stop_event is old_stop
    finally:
        release.set()
        worker.join(timeout=1)
    channel.stop()
    assert channel._runtime_state == "stopped"


def test_restart_uses_new_stop_signal(channel):
    old_stop = channel._stop_event
    channel.stop()
    channel._start_workers = lambda: channel._stop_event.set()
    channel.startup()
    assert old_stop.is_set()
    assert channel._stop_event is not old_stop


def test_config_lists_are_isolated_and_invalid_values_fail_early():
    first, second = load_wechat_desktop_config({}), load_wechat_desktop_config({})
    first["auto_reply_groups"].append("mutated")
    assert "mutated" not in second["auto_reply_groups"]
    assert "mutated" not in DEFAULT_CONFIG["auto_reply_groups"]
    for override in ({"reply_queue_capacity": 0}, {"shadow_mode": "false"},
                     {"daily_hot_broadcast_time": "25:00"}, {"reply_queue_max_wait_seconds": float("inf")}):
        with pytest.raises(ValueError):
            load_wechat_desktop_config(override)


def test_runtime_metadata_is_not_serialized_or_fingerprinted():
    message = event()
    before = message.fingerprint()
    message.task.deferred_materialization_events = [message]
    message.task.batch_id = "batch"
    assert "task" not in message.to_dict()
    assert message.fingerprint() == before


def test_partial_send_is_reported_to_queue_and_audit(channel):
    from bridge.reply import Reply, ReplyType
    from channel.wechat_desktop.models import ReplyTargetValidation, WechatDesktopMessage

    message = event()
    context = {
        "msg": WechatDesktopMessage(message), "receiver": "alice",
        "isgroup": False, "wechat_desktop_source_type": "private",
    }
    channel._driver.validate_reply_target = lambda event: ReplyTargetValidation(True)
    channel._deliver = lambda *args, **kwargs: {
        "success": False, "verified": False, "status": "partial",
        "chunks": 3, "submitted_chunks": 1, "verified_chunks": 1,
        "retryable": False, "message": "cancelled after first chunk",
    }
    channel._send_reply_impl(Reply(ReplyType.TEXT, "long reply"), context)
    assert context["wechat_desktop_queue_terminal"] == "partial"
    audit = channel._store._get_connection().execute("SELECT result, detail FROM audit").fetchone()
    assert audit["result"] == "partial"
    assert '"submitted_chunks": 1' in audit["detail"]
    channel._reply_queue.enqueue(message)
    item = channel._reply_queue.get()
    channel._reply_queue.finish(item, context["wechat_desktop_queue_terminal"])
    assert channel._reply_queue.status()["queue_partial"] == 1


@pytest.mark.parametrize("outcome", ["cancelled", "partial"])
def test_broadcast_retries_only_when_no_chunk_was_submitted(channel, outcome):
    channel.config["daily_hot_broadcast_groups"] = ["A"]
    channel.enqueue_daily_hot_broadcast("news", "2026-09-20")
    item = channel._reply_queue.get()

    def deliver(*args, before_send, **kwargs):
        before_send()
        if outcome == "cancelled":
            raise DeliveryBlocked("paused before first chunk")
        return {"success": False, "verified": False, "status": "partial", "retryable": False}

    channel._deliver = deliver
    terminal = channel._send_precomposed_reply(item)
    channel._reply_queue.finish(item, terminal)
    counts = channel._store.broadcast_status_counts("2026-09-20")
    assert counts == {"pending" if outcome == "cancelled" else "partial": 1}
    queued = channel.enqueue_daily_hot_broadcast("news", "2026-09-20")
    assert queued == (1 if outcome == "cancelled" else 0)
