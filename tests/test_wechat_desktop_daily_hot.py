"""Tests for wechat_desktop daily Baidu-hot broadcast."""

from __future__ import annotations

import threading
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from channel.wechat_desktop.baidu_hot import (
    build_daily_hot_message,
    fallback_summary_from_materials,
    format_top_item_message,
    fetch_hot_detail,
    fetch_trending,
    resolve_qianfan_api_key,
    summarize_hot_with_commentary,
)
from channel.wechat_desktop.config import DEFAULT_CONFIG, load_wechat_desktop_config
from channel.wechat_desktop.daily_hot_scheduler import (
    DailyHotScheduler,
    is_due,
    parse_hhmm,
)
from channel.wechat_desktop.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.policy import WechatDesktopPolicy
from channel.wechat_desktop.service import reset_wechat_desktop_service_for_tests
from channel.wechat_desktop.store import WechatDesktopStore


def test_parse_hhmm_and_is_due():
    assert parse_hhmm("18:00") == (18, 0)
    assert parse_hhmm("9:05") == (9, 5)
    with pytest.raises(ValueError):
        parse_hhmm("25:00")
    with pytest.raises(ValueError):
        parse_hhmm("bad")

    now = datetime(2026, 8, 3, 18, 0, 0)
    assert is_due(now, "18:00", "") is True
    assert is_due(now, "18:00", "2026-08-03") is False
    assert is_due(datetime(2026, 8, 3, 17, 59, 59), "18:00", "") is False
    assert is_due(datetime(2026, 8, 3, 18, 1, 0), "18:00", "2026-08-02") is True


def test_format_top_item_message_omits_empty_fields():
    text = format_top_item_message(
        {
            "rank": 1,
            "word": "示例热点",
            "hot_score": "100万",
            "change": "沸",
            "description": "这是简介",
            "url": "https://example.com/hot",
        },
        prefix="📰 今日热点",
        body="概括正文\n我的碎碎念：有点意思。",
    )
    assert "📰 今日热点" in text
    assert "1. 示例热点" in text
    assert "热度 100万" in text
    assert "沸" in text
    assert "概括正文" in text
    assert "我的碎碎念" in text
    assert text.strip().endswith("链接：https://example.com/hot")
    # body should win over raw description when provided
    assert "这是简介" not in text

    short = format_top_item_message({"rank": 1, "word": "只有标题"}, prefix="")
    assert short == "1. 只有标题"


def test_load_wechat_desktop_config_defaults_and_override():
    loaded = load_wechat_desktop_config({})
    assert loaded["daily_hot_broadcast_time"] == DEFAULT_CONFIG["daily_hot_broadcast_time"]
    assert loaded["daily_hot_broadcast_enabled"] is DEFAULT_CONFIG[
        "daily_hot_broadcast_enabled"
    ]
    assert "daily_hot_broadcast_enabled" in DEFAULT_CONFIG
    overridden = load_wechat_desktop_config(
        {"daily_hot_broadcast_enabled": True, "shadow_mode": False}
    )
    assert overridden["daily_hot_broadcast_enabled"] is True
    assert overridden["shadow_mode"] is False
    assert overridden["daily_hot_broadcast_tab"] == "livelihood"


def test_resolve_qianfan_api_key_prefers_env_over_config():
    import os

    import channel.wechat_desktop.baidu_hot as baidu_hot

    baidu_hot._COW_ENV_LOADED = True
    old = os.environ.get("QIANFAN_API_KEY")
    try:
        os.environ["QIANFAN_API_KEY"] = "from-env"
        with patch("channel.wechat_desktop.baidu_hot.conf") as conf_mock:
            conf_mock.return_value.get.return_value = "from-config"
            assert resolve_qianfan_api_key() == "from-env"

        os.environ.pop("QIANFAN_API_KEY", None)
        with patch("channel.wechat_desktop.baidu_hot.conf") as conf_mock:
            conf_mock.return_value.get.return_value = "from-config"
            assert resolve_qianfan_api_key() == "from-config"
    finally:
        if old is None:
            os.environ.pop("QIANFAN_API_KEY", None)
        else:
            os.environ["QIANFAN_API_KEY"] = old


def test_fetch_trending_requires_api_key():
    with patch(
        "channel.wechat_desktop.baidu_hot.resolve_qianfan_api_key",
        return_value="",
    ):
        result = fetch_trending("livelihood", 1)
    assert result["ok"] is False
    assert "QIANFAN_API_KEY" in result["error"]
    assert ".env" in result["error"]


def test_fetch_trending_parses_payload():
    payload = {
        "code": "0",
        "data": [
            {
                "index": 0,
                "word": "首条",
                "query": "首条",
                "hotScore": "999",
                "hotChange": "新",
                "desc": "简介A",
                "url": "https://baidu.com/1",
            },
            {
                "index": 1,
                "word": "次条",
                "hotScore": "1",
            },
        ],
    }
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    with patch(
        "channel.wechat_desktop.baidu_hot.resolve_qianfan_api_key",
        return_value="test-key",
    ), patch(
        "channel.wechat_desktop.baidu_hot.requests.get",
        return_value=response,
    ) as get_mock:
        result = fetch_trending("livelihood", 1)
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["items"][0]["word"] == "首条"
    assert result["items"][0]["description"] == "简介A"
    get_mock.assert_called_once()


def test_fetch_hot_detail_parses_references():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "references": [
            {
                "title": "详情报道",
                "content": "事情是这样的……",
                "url": "https://example.com/a",
                "date": "2026-08-03",
                "website": "示例站",
            }
        ]
    }
    with patch(
        "channel.wechat_desktop.baidu_hot.resolve_qianfan_api_key",
        return_value="test-key",
    ), patch(
        "channel.wechat_desktop.baidu_hot.requests.post",
        return_value=response,
    ) as post_mock:
        result = fetch_hot_detail("示例热点", count=3)
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["references"][0]["title"] == "详情报道"
    assert "事情是这样的" in result["references"][0]["content"]
    post_mock.assert_called_once()
    kwargs = post_mock.call_args.kwargs
    assert kwargs["json"]["search_source"] == "baidu_search_v2"


def test_summarize_hot_with_commentary_success():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "事件大概是空调常开省电这事儿被辟谣了。\n我觉得吧，标题党害人不浅。"
                }
            }
        ]
    }
    with patch(
        "channel.wechat_desktop.baidu_hot.resolve_chat_endpoint",
        return_value={
            "api_base": "https://api.example.com/v1",
            "api_key": "sk-test",
            "model": "demo-model",
        },
    ), patch(
        "channel.wechat_desktop.baidu_hot.requests.post",
        return_value=response,
    ):
        result = summarize_hot_with_commentary("空调热点", "资料A\n资料B")
    assert result["ok"] is True
    assert "辟谣" in result["text"]
    assert "标题党" in result["text"]


def test_fallback_summary_from_materials():
    text = fallback_summary_from_materials(
        "示例热点",
        [{"title": "标题", "content": "正文细节很多很多"}],
        description="榜单简介",
    )
    assert "榜单简介" in text
    assert "正文细节" in text
    assert "碎碎念" in text


def test_build_daily_hot_message_success():
    with patch(
        "channel.wechat_desktop.baidu_hot.fetch_trending",
        return_value={
            "ok": True,
            "tab": "livelihood",
            "items": [
                {
                    "rank": 1,
                    "word": "热搜一",
                    "hot_score": "10",
                    "change": "",
                    "description": "详情简介",
                    "url": "https://baidu.com/hot/1",
                }
            ],
        },
    ), patch(
        "channel.wechat_desktop.baidu_hot.fetch_hot_detail",
        return_value={
            "ok": True,
            "references": [
                {
                    "title": "深度报道",
                    "content": "官方回应称说法不严谨，需分场景判断。",
                    "url": "https://news.example/1",
                    "date": "2026-08-03",
                    "site": "示例",
                }
            ],
        },
    ), patch(
        "channel.wechat_desktop.baidu_hot.summarize_hot_with_commentary",
        return_value={
            "ok": True,
            "text": "官方出来划重点了：短时外出调高温度就行，别傻乎乎全天空转。\n我的看法：省电秘籍最终还是得看场景，标题党可以散了。",
        },
    ):
        payload = build_daily_hot_message(prefix="热点")
    assert payload["ok"] is True
    assert payload["summary_source"] == "llm"
    assert "热搜一" in payload["message"]
    assert "官方出来划重点" in payload["message"]
    assert "标题党可以散了" in payload["message"]
    assert payload["message"].strip().endswith("链接：https://baidu.com/hot/1")


def test_build_daily_hot_message_falls_back_when_llm_fails():
    with patch(
        "channel.wechat_desktop.baidu_hot.fetch_trending",
        return_value={
            "ok": True,
            "tab": "livelihood",
            "items": [
                {
                    "rank": 1,
                    "word": "热搜二",
                    "description": "",
                    "url": "https://baidu.com/hot/2",
                }
            ],
        },
    ), patch(
        "channel.wechat_desktop.baidu_hot.fetch_hot_detail",
        return_value={
            "ok": True,
            "references": [{"title": "报道", "content": "详细经过在此。"}],
        },
    ), patch(
        "channel.wechat_desktop.baidu_hot.summarize_hot_with_commentary",
        return_value={"ok": False, "error": "llm down", "text": ""},
    ):
        payload = build_daily_hot_message(prefix="热点")
    assert payload["ok"] is True
    assert payload["summary_source"] == "fallback"
    assert "详细经过在此" in payload["message"]
    assert "碎碎念" in payload["message"]
    assert payload["message"].strip().endswith("链接：https://baidu.com/hot/2")


def test_scheduler_prepares_and_enqueues_once_per_day(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    enqueued = []
    ready = threading.Event()

    def enqueue(message: str) -> int:
        enqueued.append(message)
        ready.set()
        return 2

    config = {
        "daily_hot_broadcast_enabled": True,
        "daily_hot_broadcast_time": "18:00",
        "daily_hot_broadcast_tab": "livelihood",
        "daily_hot_broadcast_message_prefix": "📰 今日热点",
        "shadow_mode": False,
    }
    scheduler = DailyHotScheduler(
        config=config,
        store=store,
        enqueue_callback=enqueue,
        is_paused=lambda: False,
        tick_seconds=60,
        prepare_factory=lambda **kwargs: {
            "ok": True,
            "message": "📰 今日热点\n1. 测试",
            "item": {"word": "测试"},
            "tab": "livelihood",
        },
    )

    # Force a due tick at 18:00.
    scheduler.now_factory = lambda: datetime(2026, 8, 3, 18, 0, 5)
    scheduler._tick()
    assert scheduler.wait_prepare(timeout=2.0)
    assert enqueued == ["📰 今日热点\n1. 测试"]
    assert store.get_state("daily_hot_last_date") == "2026-08-03"
    assert store.get_state("daily_hot_last_status") == "queued"

    # Same day should not fire again.
    enqueued.clear()
    ready.clear()
    scheduler._tick()
    assert scheduler.wait_prepare(timeout=0.5)
    assert enqueued == []


def test_scheduler_skips_when_disabled_or_shadow(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    called = []

    scheduler = DailyHotScheduler(
        config={
            "daily_hot_broadcast_enabled": False,
            "daily_hot_broadcast_time": "18:00",
            "shadow_mode": False,
        },
        store=store,
        enqueue_callback=lambda message: called.append(message) or 1,
        prepare_factory=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("should not prepare")
        ),
    )
    scheduler.now_factory = lambda: datetime(2026, 8, 3, 18, 0, 0)
    scheduler._tick()
    assert called == []

    scheduler.config["daily_hot_broadcast_enabled"] = True
    scheduler.config["shadow_mode"] = True
    scheduler._tick()
    assert called == []


def test_scheduler_retries_after_prepare_failure(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    calls = {"n": 0}
    enqueued = []

    def prepare(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error": "temporary", "message": ""}
        return {"ok": True, "message": "ready", "item": {}, "tab": "livelihood"}

    scheduler = DailyHotScheduler(
        config={
            "daily_hot_broadcast_enabled": True,
            "daily_hot_broadcast_time": "18:00",
            "shadow_mode": False,
        },
        store=store,
        enqueue_callback=lambda message: enqueued.append(message) or 1,
        prepare_factory=prepare,
    )
    scheduler.now_factory = lambda: datetime(2026, 8, 3, 19, 0, 0)
    scheduler._tick()
    assert scheduler.wait_prepare(timeout=2.0)
    assert enqueued == []
    assert store.get_state("daily_hot_last_date") in ("", None)

    scheduler._tick()
    assert scheduler.wait_prepare(timeout=2.0)
    assert enqueued == ["ready"]
    assert store.get_state("daily_hot_last_date") == "2026-08-03"


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


def _channel_impl():
    from channel.wechat_desktop.wechat_desktop_channel import WechatDesktopChannel

    # ``@singleton`` wraps the class; recover the original type for unit tests.
    return WechatDesktopChannel.__closure__[0].cell_contents


def _make_channel_for_enqueue(tmp_path, config_overrides=None):
    """Build a minimal channel-like object for enqueue/send tests without UIA."""
    impl = _channel_impl()
    service = reset_wechat_desktop_service_for_tests(str(tmp_path / "wechat.sqlite3"))
    channel = object.__new__(impl)
    channel.config = {
        "shadow_mode": False,
        "auto_reply_groups": ["群A", "群B", "黑名单群"],
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
    channel._lifecycles = {}
    channel._lifecycle_lock = threading.RLock()
    channel._scan_count = 0
    channel._driver = _FakeDriver()
    channel._stop_event = threading.Event()
    # Bind real methods.
    channel._start_lifecycle = impl._start_lifecycle.__get__(channel)
    channel._mark_lifecycle = impl._mark_lifecycle.__get__(channel)
    channel._finish_lifecycle = impl._finish_lifecycle.__get__(channel)
    channel._trace = lambda *args, **kwargs: None
    channel._content_hash = impl._content_hash
    channel._enqueue_reply_event = impl._enqueue_reply_event.__get__(channel)
    channel.enqueue_daily_hot_broadcast = impl.enqueue_daily_hot_broadcast.__get__(
        channel
    )
    channel._send_precomposed_reply = impl._send_precomposed_reply.__get__(channel)
    return channel


def test_enqueue_daily_hot_broadcast_filters_groups(tmp_path):
    channel = _make_channel_for_enqueue(tmp_path)
    queued = channel.enqueue_daily_hot_broadcast("热点正文")
    assert queued == 2
    assert channel._reply_queue.status()["queue_depth"] == 2

    item1 = channel._reply_queue.get(timeout=0.5)
    item2 = channel._reply_queue.get(timeout=0.5)
    names = {item1.event.conversation_name, item2.event.conversation_name}
    assert names == {"群A", "群B"}
    assert getattr(item1.event, "_proactive_send") is True
    assert getattr(item1.event, "_precomposed_reply_text") == "热点正文"


def test_enqueue_daily_hot_broadcast_shadow_mode(tmp_path):
    channel = _make_channel_for_enqueue(tmp_path, {"shadow_mode": True})
    assert channel.enqueue_daily_hot_broadcast("热点正文") == 0
    assert channel._reply_queue.status()["queue_depth"] == 0


def test_send_precomposed_reply(tmp_path):
    channel = _make_channel_for_enqueue(tmp_path)
    channel.enqueue_daily_hot_broadcast("推送内容")
    item = channel._reply_queue.get(timeout=0.5)
    terminal = channel._send_precomposed_reply(item)
    assert terminal == "completed"
    assert channel._driver.sent == [(item.event.conversation_name, "推送内容")]
