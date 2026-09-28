"""广播编排回归：目标策略、FIFO 入队和预写文案发送。"""

from __future__ import annotations
from channel.wechat_desktop.contracts import ConversationTarget, TargetResolution, TargetStatus
from channel.wechat_desktop.pipeline.broadcast import BroadcastCoordinator
import threading
import pytest
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.storage.service import reset_wechat_desktop_service_for_tests
from channel.wechat_desktop.storage.store import WechatDesktopStore
from channel.wechat_desktop.pipeline.lifecycle import LifecycleRecorder


class _FakeDriver:
    def __init__(self):
        self.sent = []
        self.cycles = 0

    def begin_reply_cycle(self, conversation, conversation_id=""):
        self.cycles += 1

    def end_reply_cycle(self):
        self.cycles = max(0, self.cycles - 1)

    def send_text(self, conversation, text):
        self.sent.append((conversation, text))
        return {"success": True, "verified": True}

    def resolve_target(self, conversation):
        return TargetResolution(TargetStatus.RESOLVED, ConversationTarget(conversation, conversation))


def _channel_impl():
    from channel.wechat_desktop.pipeline.channel import WechatDesktopChannel

    # ``@singleton`` wraps the class; recover the original type for unit tests.
    return WechatDesktopChannel.__closure__[0].cell_contents


def _make_channel_for_enqueue(tmp_path, config_overrides=None):
    """Build a minimal channel-like object for enqueue/send tests without UIA."""
    impl = _channel_impl()
    service = reset_wechat_desktop_service_for_tests(str(tmp_path / "wechat.sqlite3"))
    channel = object.__new__(impl)
    channel.config = {
        "shadow_mode": False,
        "auto_reply_groups": ["自动回复群"],
        "daily_hot_broadcast_groups": ["群A", "群B", "黑名单群"],
        "auto_reply_blacklist": ["黑名单群"],
        "auto_reply_groups_all": False,
        "self_display_name": "我",
        "max_send_per_minute": 50,
        "max_send_per_hour": 500,
    }
    if config_overrides:
        channel.config.update(config_overrides)
    channel._service = service
    channel._store = service.store
    channel._policy = WechatDesktopPolicy(channel.config, channel._store)
    channel._reply_queue = WechatReplyQueue()
    channel._daily_hot_scheduler = None
    channel._lifecycle = LifecycleRecorder()
    channel._driver = _FakeDriver()
    channel._stop_event = threading.Event()
    # Bind real methods.
    channel._start_lifecycle = impl._start_lifecycle.__get__(channel)
    channel._mark_lifecycle = impl._mark_lifecycle.__get__(channel)
    channel._finish_lifecycle = impl._finish_lifecycle.__get__(channel)
    channel._trace = lambda *args, **kwargs: None
    channel._content_hash = impl._content_hash
    channel._enqueue_reply_event = impl._enqueue_reply_event.__get__(channel)
    channel._resolve_daily_hot_target = impl._resolve_daily_hot_target.__get__(
        channel
    )
    channel.enqueue_daily_hot_broadcast = impl.enqueue_daily_hot_broadcast.__get__(
        channel
    )
    channel._send_precomposed_reply = impl._send_precomposed_reply.__get__(channel)
    return channel


def test_enqueue_daily_hot_broadcast_filters_groups(tmp_path):
    channel = _make_channel_for_enqueue(tmp_path)
    queued = BroadcastCoordinator(channel).enqueue("热点正文")
    assert queued == 2
    assert channel._reply_queue.status()["queue_depth"] == 2

    item1 = channel._reply_queue.get(timeout=0.5)
    item2 = channel._reply_queue.get(timeout=0.5)
    names = {item1.event.conversation_name, item2.event.conversation_name}
    assert names == {"群A", "群B"}
    assert item1.event.task.proactive_send is True
    assert item1.event.task.precomposed_reply_text == "热点正文"


def test_enqueue_daily_hot_broadcast_resolves_session_id(tmp_path):
    channel = _make_channel_for_enqueue(
        tmp_path,
        {
            "daily_hot_broadcast_groups": ["小小地下联络站"],
            "auto_reply_groups": ["其他自动回复群"],
            "auto_reply_blacklist": [],
        },
    )

    class _ResolverDriver(_FakeDriver):
        def resolve_target(self, conversation):
            return TargetResolution(TargetStatus.RESOLVED, ConversationTarget("uia-session:42.9.9.9", conversation))

    channel._driver = _ResolverDriver()
    queued = BroadcastCoordinator(channel).enqueue("热点正文")
    assert queued == 1
    item = channel._reply_queue.get(timeout=0.5)
    assert item.event.conversation_name == "小小地下联络站"
    assert item.event.conversation_id == "uia-session:42.9.9.9"


def test_enqueue_daily_hot_broadcast_shadow_mode(tmp_path):
    channel = _make_channel_for_enqueue(tmp_path, {"shadow_mode": True})
    assert BroadcastCoordinator(channel).enqueue("热点正文") == 0
    assert channel._reply_queue.status()["queue_depth"] == 0


def test_enqueue_daily_hot_broadcast_ignores_auto_reply_groups(tmp_path):
    channel = _make_channel_for_enqueue(
        tmp_path,
        {
            "auto_reply_groups": ["自动回复群"],
            "daily_hot_broadcast_groups": ["热点群"],
            "auto_reply_blacklist": [],
            "auto_reply_groups_all": True,
        },
    )
    queued = BroadcastCoordinator(channel).enqueue("热点正文")
    assert queued == 1
    item = channel._reply_queue.get(timeout=0.5)
    assert item.event.conversation_name == "热点群"


def test_policy_daily_hot_target_is_independent_from_auto_reply(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    policy = WechatDesktopPolicy(
        {
            "shadow_mode": False,
            "auto_reply_groups": ["自动回复群"],
            "auto_reply_groups_all": False,
            "daily_hot_broadcast_groups": ["热点群"],
            "auto_reply_blacklist": [],
            "max_send_per_minute": 5,
            "max_send_per_hour": 60,
        },
        store,
    )
    assert policy.is_allowlisted("热点群", True) is False
    assert policy.allows_send("热点群", True, "text") is False
    assert policy.is_daily_hot_target("热点群") is True
    assert policy.allows_send("热点群", True, "text", daily_hot=True) is True
    assert policy.is_daily_hot_target("自动回复群") is False
    assert policy.allows_send("自动回复群", True, "text", daily_hot=True) is False


def test_send_precomposed_reply(tmp_path):
    channel = _make_channel_for_enqueue(tmp_path)
    BroadcastCoordinator(channel).enqueue("推送内容")
    item = channel._reply_queue.get(timeout=0.5)
    terminal = BroadcastCoordinator(channel).send(item)
    assert terminal == "completed"
    assert channel._driver.sent == [(item.event.conversation_name, "推送内容")]


@pytest.mark.parametrize("status", ["uncertain", "partial", "unverified"])
def test_broadcast_uncertain_delivery_is_not_reenqueued_or_replayed(tmp_path, status):
    from channel.wechat_desktop.contracts import SendResult, SendStatus

    channel = _make_channel_for_enqueue(tmp_path, {"daily_hot_broadcast_groups": ["群A"]})
    coordinator = BroadcastCoordinator(channel)
    assert coordinator.enqueue("推送内容", "2026-09-24") == 1
    item = channel._reply_queue.get(timeout=0.5)
    calls = []

    def send(target, text):
        calls.append((target, text))
        return SendResult(SendStatus(status))

    channel._driver.send_text = send
    terminal = coordinator.send(item)
    assert terminal == ("partial" if status == "partial" else "uncertain")
    assert coordinator.enqueue("推送内容", "2026-09-24") == 0
    coordinator.send(item)
    assert calls == [("群A", "推送内容")]
