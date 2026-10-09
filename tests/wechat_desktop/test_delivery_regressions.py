"""发送幂等额度与图片来源复核回归；仅使用合成数据库和发送替身。"""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bridge.reply import Reply, ReplyType
from channel.wechat_desktop.config import load_wechat_desktop_config
from channel.wechat_desktop.contracts import SendResult, SendStatus
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.pipeline.delivery import DeliveryBlocked, DeliveryService
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_contracts import make_channel
from .test_db_backend import Gateway
from .test_db_reader import add_message, make_reader, table


@pytest.fixture
def store(tmp_path):
    ledger = WechatDesktopStore(str(tmp_path / "delivery-regressions.sqlite3"))
    yield ledger
    ledger._get_connection().close()


def _service(store, send, *, minute=2):
    config = load_wechat_desktop_config({"shadow_mode": False, "auto_reply_private_all": True,
                                       "max_send_per_minute": minute, "max_send_per_hour": 10})
    return DeliveryService(config, WechatDesktopPolicy(config, store),
                           SimpleNamespace(send_text=send), lambda: False, lambda: False,
                           lambda token: True, store)


@pytest.mark.parametrize("status", [SendStatus.SENT, SendStatus.UNVERIFIED, SendStatus.UNCERTAIN])
def test_repeated_delivery_receipt_does_not_consume_send_quota(store, status):
    calls = []
    receipt = SendResult(status, chunks=1, submitted_chunks=1, verified_chunks=int(status == SendStatus.SENT))
    service = _service(store, lambda *args: calls.append(args) or receipt)
    first = service.send("Synthetic", "same reply", policy_target="Synthetic", source_event_ids=["event-1"])
    for _ in range(10):
        assert service.send("Synthetic", "same reply", policy_target="Synthetic",
                            source_event_ids=["event-1"]).status == first.status
    service.send("Synthetic", "new reply", policy_target="Synthetic", source_event_ids=["event-2"])
    assert len(calls) == 2
    assert store._get_connection().execute("SELECT count(*) FROM rate_events").fetchone()[0] == 2


def test_rate_blocked_claim_is_persisted_as_not_submitted(store):
    calls = []
    service = _service(store, lambda *args: calls.append(args) or SendResult(SendStatus.SENT), minute=1)
    service.send("Synthetic", "first", policy_target="Synthetic", source_event_ids=["event-1"])
    with pytest.raises(DeliveryBlocked, match="rate limit"):
        service.send("Synthetic", "second", policy_target="Synthetic", source_event_ids=["event-2"])
    rows = store._get_connection().execute("SELECT status FROM deliveries ORDER BY updated_at").fetchall()
    assert [row[0] for row in rows] == ["sent", "not_sent"]
    for _ in range(3):
        previous = service.send("Synthetic", "second", policy_target="Synthetic", source_event_ids=["event-2"])
        assert previous.status == SendStatus.NOT_SENT
    assert len(calls) == 1
    assert store._get_connection().execute("SELECT count(*) FROM rate_events").fetchone()[0] == 1


def _reply_setup(tmp_path, store):
    reader, talker = make_reader(tmp_path)
    backend = WechatDatabaseBackend({}, db_reader=reader, store=store, uia_gateway=Gateway())
    backend.observe_events()
    cache = reader.caches["message/message_0.db"]
    add_message(cache, talker, 1)
    observation, events = backend.observe_events()
    store.receive_source_batch(observation["source_batch"])
    backend.acknowledge_events([observation["source_batch"].batch_id])
    event = events[0]
    channel = make_channel(store, backend)
    channel.config["auto_send_images"] = True
    context = {"msg": SimpleNamespace(event=event, other_user_nickname=event.conversation_name,
                                      other_user_id=event.conversation_id),
               "receiver": event.conversation_id, "isgroup": False,
               "wechat_desktop_source_type": "private", "wechat_desktop_source_event_ids": [event.event_id]}
    return reader, talker, cache, channel, context


@pytest.mark.parametrize("kind", [ReplyType.TEXT, ReplyType.IMAGE, ReplyType.IMAGE_URL])
@pytest.mark.parametrize("change,reason", [("delete", "source_message_deleted"),
                                           ("replace", "source_message_changed"),
                                           ("generation", "source_generation_changed")])
def test_reply_revalidates_native_source_before_text_or_image_submission(tmp_path, store, kind, change, reason):
    _, talker, cache, channel, context = _reply_setup(tmp_path, store)
    if change == "generation":
        cache.status = replace(cache.status, generation="replacement-generation")
    else:
        with sqlite3.connect(cache.path) as connection:
            if change == "delete":
                connection.execute(f'DELETE FROM "{table(talker)}" WHERE local_id=1')
            else:
                connection.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=1', ("changed",))
    cache.changed = True
    submitted = []
    stages = []
    channel._deliver = lambda *args, **kwargs: submitted.append(args) or SendResult(SendStatus.SENT)
    channel._mark_lifecycle = lambda ids, stage, **kwargs: stages.append(stage)

    channel._send_reply_impl(Reply(kind, "synthetic reply"), context)

    assert submitted == []
    assert stages == []
    assert context["wechat_desktop_queue_terminal"] == "skipped"
    audit = store._get_connection().execute("SELECT action_type,result,detail FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(audit) == ("send_text" if kind == ReplyType.TEXT else "send_image", "stale_target", reason)


@pytest.mark.parametrize("kind", [ReplyType.TEXT, ReplyType.IMAGE, ReplyType.IMAGE_URL])
def test_reply_with_valid_native_source_still_submits(tmp_path, store, kind):
    _, _, _, channel, context = _reply_setup(tmp_path, store)
    submitted = []
    channel._deliver = lambda *args, **kwargs: submitted.append((args, kwargs)) or SendResult(SendStatus.SENT)

    channel._send_reply_impl(Reply(kind, "synthetic reply"), context)

    assert len(submitted) == 1
    assert context["wechat_desktop_queue_terminal"] == "completed"
