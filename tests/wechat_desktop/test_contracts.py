"""跨阶段契约：真实 SQLite、受控故障、后端替身，无真实微信操作。"""

import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from bridge.reply import Reply, ReplyType
from channel.wechat_desktop.config import load_wechat_desktop_config
from channel.wechat_desktop.contracts import SendResult, SendStatus
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.pipeline.delivery import DeliveryService
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.storage.service import WechatDesktopService
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.db.source import WechatDatabaseSource
from channel.wechat_desktop.uia.operations import WechatSendOperations, WechatConversationSelector
from .helpers import _bare_wechat_channel
from .test_db_reader import make_reader, add_message
from channel.wechat_desktop.pipeline.lifecycle import LifecycleRecorder


@pytest.fixture
def store(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "contracts.sqlite3"))
    yield store
    store._get_connection().close()


def event(text="hello"):
    return WechatDesktopEvent("message", "alice", "Alice", "alice", "Alice", "text", text, source_type="private")


def database_source_with_messages(tmp_path, store, *, batch_size=200):
    reader, talker = make_reader(tmp_path, batch_size=batch_size)
    source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    assert source.observe_events()[1] == []
    add_message(reader.caches["message/message_0.db"], talker, 1, content="hello")
    return source, reader, talker


def make_channel(store, driver):
    channel = _bare_wechat_channel()
    channel.config = load_wechat_desktop_config({"shadow_mode": False})
    channel._driver = driver
    channel._store = store
    channel._service = WechatDesktopService(store)
    channel._policy = WechatDesktopPolicy(channel.config, store)
    channel._reply_queue = WechatReplyQueue(capacity=1)
    channel._stop_event = threading.Event()
    channel._lifecycle = LifecycleRecorder()
    channel._last_cleanup_at = time.time()
    channel._trace = lambda *args, **kwargs: None
    channel._materialize_submit_lock = threading.RLock()
    channel._materialize_queue = queue.Queue(maxsize=1)
    channel._mark_lifecycle = lambda *args, **kwargs: None
    return channel


@pytest.mark.parametrize("raw,status", [
    ({"success": True, "verified": True}, SendStatus.SENT),
    ({"success": True, "verified": False}, SendStatus.UNVERIFIED),
    ({"success": False}, SendStatus.UNCERTAIN),
    ({"status": "not_sent"}, SendStatus.NOT_SENT),
    ({"status": "partial", "chunks": 2, "submitted_chunks": 1, "verified_chunks": 1}, SendStatus.PARTIAL),
    ({"status": "sent", "success": False}, SendStatus.UNCERTAIN),
    ({"status": "not_sent", "submitted_chunks": 1}, SendStatus.UNCERTAIN),
    ({"chunks": "broken"}, SendStatus.UNCERTAIN),
    (None, SendStatus.UNCERTAIN),
])
def test_normalized_result_is_conservative_and_json_serializable(raw, status):
    result = SendResult.from_backend(raw)
    assert result.status == status
    assert result["retryable"] is False
    assert json.loads(json.dumps(result.to_dict()))["status"] == status.value


def test_observation_failure_cannot_turn_verified_send_into_retry():
    calls = []
    client = SimpleNamespace(send_message=lambda *args, **kwargs: calls.append(1) or {"success": True, "verified": True})

    def failed_observation():
        raise RuntimeError("status read failed after send")

    operations = WechatSendOperations(client, lambda name: WechatConversationSelector(name), None, failed_observation)
    result = operations.send_text("Alice", "hello")
    assert result.status == SendStatus.SENT
    assert result.observation["error"]
    assert calls == [1]


def test_unacknowledged_database_events_are_redelivered_with_same_identity(tmp_path, store):
    source, _, _ = database_source_with_messages(tmp_path, store)
    observation, first = source.observe_events()
    repeated, again = source.observe_events()
    assert first and [item.event_id for item in again] == [item.event_id for item in first]
    assert observation["source_batch"].batch_id == repeated["source_batch"].batch_id
    store.receive_source_batch(observation["source_batch"])
    source.acknowledge_events([observation["source_batch"].batch_id])
    source.acknowledge_events([observation["source_batch"].batch_id])
    assert source.observe_events()[1] == []


def test_database_receipt_failure_does_not_ack_or_lose_event(tmp_path, store, monkeypatch):
    source, reader, _ = database_source_with_messages(tmp_path, store)
    channel = make_channel(store, source)
    routed = []
    channel._route_reply_event = lambda message: routed.append(message.event_id)
    original = store.receive_source_batch
    checkpoints = store.get_source_checkpoints(reader.account_id)
    monkeypatch.setattr(store, "receive_source_batch", lambda batch: (_ for _ in ()).throw(RuntimeError("disk unavailable")))
    channel._poll_once()
    pending = source.pending_batch
    assert pending is not None and routed == []
    assert store.get_source_checkpoints(reader.account_id) == checkpoints
    monkeypatch.setattr(store, "receive_source_batch", original)
    channel._poll_once()
    assert routed == [record.event.event_id for record in pending.records if record.event]
    assert source.pending_batch is None


def test_lost_database_ack_does_not_reenter_agent_or_finish_active_event(tmp_path, store, monkeypatch):
    source, _, _ = database_source_with_messages(tmp_path, store)
    channel = make_channel(store, source)
    routed = []
    channel._route_reply_event = lambda message: routed.append(message.event_id)
    original_ack = source.acknowledge_events
    monkeypatch.setattr(source, "acknowledge_events", lambda ids: (_ for _ in ()).throw(RuntimeError("ack lost")))
    with pytest.raises(RuntimeError, match="ack lost"):
        channel._poll_once()
    monkeypatch.setattr(source, "acknowledge_events", original_ack)
    channel._poll_once()
    assert len(routed) == 1
    assert store.event_state(routed[0])["state"] == "received"
    assert source.pending_batch is None


def test_event_and_receipt_commit_atomically(store):
    with store._connect() as db:
        db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON event_runs BEGIN SELECT RAISE(ABORT, 'receipt failed'); END")
    with pytest.raises(Exception, match="receipt failed"):
        store.receive_event(event())
    assert store._get_connection().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_receipt_returns_canonical_identity_for_duplicate_snapshot(store):
    first, second = event(), event()
    accepted = store.receive_event(first)
    duplicate = store.receive_event(second)
    assert accepted.accepted
    assert not duplicate.accepted
    assert duplicate.canonical_event_id == first.event_id
    assert duplicate.observed_event_id == second.event_id


def test_database_backpressure_is_bounded_until_ack(tmp_path, store):
    source, reader, talker = database_source_with_messages(tmp_path, store, batch_size=1)
    add_message(reader.caches["message/message_0.db"], talker, 2, content="two")
    observation, first = source.observe_events()
    assert [item.content for item in first] == ["hello"]
    assert len(source.pending_batch.records) == 1
    assert source.observe_events()[1] == first
    store.receive_source_batch(observation["source_batch"])
    source.acknowledge_events([observation["source_batch"].batch_id])
    assert [item.content for item in source.observe_events()[1]] == ["two"]


@pytest.mark.parametrize("stage,expected", [("received", "interrupted"), ("queued", "interrupted"), ("running", "interrupted"), ("sending", "uncertain")])
def test_restart_terminalizes_old_work_without_replay(store, stage, expected):
    message = event()
    store.receive_event(message)
    store.set_event_state([message.event_id], stage)
    if stage == "sending":
        store.claim_delivery([message.event_id], "Alice", "hash")
    reopened = WechatDesktopStore(store.path)
    try:
        reopened.recover_interrupted_events()
        assert reopened.event_state(message.event_id)["state"] == expected
        assert not reopened.receive_event(message).accepted
        reopened.recover_interrupted_events()
        assert reopened.event_state(message.event_id)["state"] == expected
    finally:
        reopened._get_connection().close()


@pytest.mark.parametrize("queue_type", ["reply", "materialize"])
def test_full_queue_has_durable_rejection_and_cannot_replay(store, queue_type):
    channel = make_channel(store, SimpleNamespace())
    first, second = event("one"), event("two")
    for message in (first, second):
        store.receive_event(message)
    if queue_type == "reply":
        assert channel._enqueue_reply_event(first)
        assert not channel._enqueue_reply_event(second)
    else:
        assert channel._submit_materialization([first])
        assert not channel._submit_materialization([second])
    assert store.event_state(second.event_id)["state"] == "rejected"
    assert "full" in store.event_state(second.event_id)["reason"]
    assert not store.receive_event(second).accepted
    assert not channel._enqueue_reply_event(second)


def test_expired_item_is_terminal_without_entering_agent(store):
    channel = make_channel(store, SimpleNamespace())
    message = event()
    message.task.created_at -= 1000
    store.receive_event(message)
    channel._enqueue_reply_event(message)
    channel._dispatch_message = lambda *args: pytest.fail("expired task entered agent")
    finish = channel._on_reply_worker_finish

    def stop(item, terminal):
        finish(item, terminal)
        channel._stop_event.set()

    channel._on_reply_worker_finish = stop
    channel._consume_reply_queue()
    assert store.event_state(message.event_id)["state"] == "expired"


@pytest.mark.parametrize("pending_send,expected", [(False, "timeout"), (True, "uncertain")])
def test_timeout_with_possible_submission_is_not_reported_as_safe_failure(store, monkeypatch, pending_send, expected):
    channel = make_channel(store, SimpleNamespace())
    message = event()
    store.receive_event(message)
    channel._enqueue_reply_event(message)
    item = channel._reply_queue.get()
    item.done = SimpleNamespace(wait=lambda timeout: False, set=lambda: None)
    channel._dispatch_message = lambda *args: True
    notices = []
    channel._send_agent_failure_notice = lambda **kwargs: notices.append(kwargs)
    if pending_send:
        store.claim_delivery([message.event_id], "Alice", "hash")
    outcome = channel._process_reply_item(item)
    channel._on_reply_worker_finish(item, outcome)
    assert outcome == expected
    assert bool(notices) is (not pending_send)
    assert not channel._reply_queue.is_active(item.token)
    store.set_event_state([message.event_id], "completed", "late_callback")
    assert store.event_state(message.event_id)["state"] == expected
    channel._reply_queue.finish(item, outcome)


def service_with_journal(store, send):
    return DeliveryService(load_wechat_desktop_config({}),
                           SimpleNamespace(allows_send=lambda *args, **kwargs: True, reserve_send=lambda units: True),
                           SimpleNamespace(send_text=send), lambda: False, lambda: False, lambda token: True, store)


@pytest.mark.parametrize("raw", [{"success": True, "verified": True}, {"success": True, "verified": False}, {"status": "uncertain"}])
def test_duplicate_send_callback_reuses_receipt_instead_of_sending_again(store, raw):
    message = event()
    store.receive_event(message)
    calls = []
    service = service_with_journal(store, lambda *args: calls.append(args) or raw)
    for _ in range(2):
        service.send("Alice", "reply", policy_target="Alice", source_event_ids=[message.event_id])
    assert len(calls) == 1


def test_result_write_failure_prevents_replay_even_after_restart(store, monkeypatch):
    message = event()
    store.receive_event(message)
    calls = []
    service = service_with_journal(store, lambda *args: calls.append(args) or {"success": True, "verified": True})
    monkeypatch.setattr(store, "finish_delivery", lambda *args: (_ for _ in ()).throw(RuntimeError("disk failed after click")))
    result = service.send("Alice", "reply", policy_target="Alice", source_event_ids=[message.event_id])
    assert result.status == SendStatus.UNCERTAIN
    reopened = WechatDesktopStore(store.path)
    try:
        reopened.recover_interrupted_events()
        again = service_with_journal(reopened, lambda *args: pytest.fail("replayed uncertain send"))
        assert again.send("Alice", "reply", policy_target="Alice", source_event_ids=[message.event_id]).status == SendStatus.UNCERTAIN
    finally:
        reopened._get_connection().close()
    assert len(calls) == 1


def test_delivery_claim_failure_never_calls_backend(store, monkeypatch):
    monkeypatch.setattr(store, "claim_delivery", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk unavailable")))
    service = service_with_journal(store, lambda *args: pytest.fail("sent without durable claim"))
    with pytest.raises(RuntimeError, match="disk unavailable"):
        service.send("Alice", "reply", policy_target="Alice", source_event_ids=["event"])


def test_parent_chat_channel_retry_is_disabled():
    channel = _bare_wechat_channel()
    calls = []

    def fail(*args):
        calls.append(1)
        raise RuntimeError("exception after UI action")

    channel.send = fail
    context = {}
    channel._send(Reply(ReplyType.TEXT, "reply"), context)
    assert calls == [1]
    assert context["wechat_desktop_queue_terminal"] == "uncertain"


def test_concurrent_store_connections_cannot_claim_same_send_twice(store):
    barrier = threading.Barrier(2)
    claims, errors = [], []
    stores = [WechatDesktopStore(store.path), WechatDesktopStore(store.path)]

    def claim(instance):
        try:
            barrier.wait(timeout=2)
            claims.append(instance.claim_delivery(["same-event"], "Alice", "same-hash"))
        except Exception as exc:
            errors.append(exc)
        finally:
            instance._get_connection().close()

    threads = [threading.Thread(target=claim, args=(instance,)) for instance in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    for instance in stores:
        instance._get_connection().close()
    assert not errors
    assert len(claims) == 2
    assert claims[0][0] == claims[1][0]
    assert sum(previous is None for _, previous in claims) == 1


def test_not_sent_delivery_is_not_automatically_reclaimed(store):
    claim_id, _ = store.claim_delivery(["event"], "Alice", "hash")
    store.finish_delivery(claim_id, SendResult(SendStatus.NOT_SENT, "cancelled before first chunk"))
    for _ in range(2):
        duplicate_id, previous = store.claim_delivery(["event"], "Alice", "hash")
        assert duplicate_id == claim_id and previous.status == SendStatus.NOT_SENT


def test_database_backfill_phase_survives_receipt_retry(tmp_path, store, monkeypatch):
    source, reader, _ = database_source_with_messages(tmp_path, store)
    source = WechatDatabaseSource({}, reader=reader, checkpoint_store=store)
    observation, messages = source.observe_events()
    assert messages[0].receipt_phase == "offline_backfill"
    channel = make_channel(store, source)
    routed = []
    channel._route_reply_event = lambda message: routed.append(message)
    receive = store.receive_source_batch
    monkeypatch.setattr(store, "receive_source_batch", lambda batch: (_ for _ in ()).throw(RuntimeError("disk unavailable")))
    channel._poll_once()
    monkeypatch.setattr(store, "receive_source_batch", receive)
    channel._poll_once()
    assert not routed
    assert store.event_state(messages[0].event_id)["reason"] == "offline_backfill"
