"""Tests for wechat_desktop daily Baidu-hot broadcast."""

from __future__ import annotations
from datetime import datetime
from unittest.mock import MagicMock, patch
import pytest
from channel.wechat_desktop.daily_hot.baidu_hot import (
    build_daily_hot_message,
    fallback_summary_from_materials,
    format_top_item_message,
    fetch_hot_detail,
    fetch_trending,
    resolve_qianfan_api_key,
    summarize_hot_with_commentary,
)
from channel.wechat_desktop.config import DEFAULT_CONFIG, load_wechat_desktop_config
from channel.wechat_desktop.daily_hot.scheduler import DailyHotScheduler, is_due, parse_hhmm
from channel.wechat_desktop.models import ConversationInfo
from channel.wechat_desktop.uia.operations import (
    conversation_titles_match,
    resolve_conversation_selector,
    strip_member_count_suffix,
)
from channel.wechat_desktop.storage.store import WechatDesktopStore


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
    assert "daily_hot_broadcast_groups" in loaded
    assert loaded["daily_hot_broadcast_groups"] == DEFAULT_CONFIG["daily_hot_broadcast_groups"]
    assert loaded["auto_reply_groups"] == DEFAULT_CONFIG["auto_reply_groups"]


def test_global_config_drops_wechat_desktop_keys():
    from config import Config, _drop_wechat_desktop_keys_from_global

    loaded = Config(
        {
            "model": "keep-me",
            "shadow_mode": True,
            "auto_reply_groups": ["误放在外层的群"],
            "wechat_desktop": {"shadow_mode": True},
        }
    )
    dropped = _drop_wechat_desktop_keys_from_global(loaded)
    assert "shadow_mode" in dropped
    assert "auto_reply_groups" in dropped
    assert "wechat_desktop" in dropped
    assert "shadow_mode" not in loaded
    assert "auto_reply_groups" not in loaded
    assert "wechat_desktop" not in loaded
    assert loaded.get("model") == "keep-me"


def test_load_wechat_desktop_config_ignores_json_section(monkeypatch):
    import channel.wechat_desktop.config as desktop_config

    class DummyConf(dict):
        def get(self, key, default=None):
            if key == "wechat_desktop":
                return {"shadow_mode": True, "daily_hot_broadcast_enabled": False}
            return super().get(key, default)

    monkeypatch.setattr(desktop_config, "conf", lambda: DummyConf())
    loaded = load_wechat_desktop_config()
    assert loaded["shadow_mode"] is DEFAULT_CONFIG["shadow_mode"]
    assert loaded["daily_hot_broadcast_enabled"] is DEFAULT_CONFIG[
        "daily_hot_broadcast_enabled"
    ]


def test_resolve_qianfan_api_key_prefers_env_over_config():
    import os

    import channel.wechat_desktop.daily_hot.baidu_hot as baidu_hot

    baidu_hot._COW_ENV_LOADED = True
    old = os.environ.get("QIANFAN_API_KEY")
    try:
        os.environ["QIANFAN_API_KEY"] = "from-env"
        with patch("channel.wechat_desktop.daily_hot.baidu_hot.conf") as conf_mock:
            conf_mock.return_value.get.return_value = "from-config"
            assert resolve_qianfan_api_key() == "from-env"

        os.environ.pop("QIANFAN_API_KEY", None)
        with patch("channel.wechat_desktop.daily_hot.baidu_hot.conf") as conf_mock:
            conf_mock.return_value.get.return_value = "from-config"
            assert resolve_qianfan_api_key() == "from-config"
    finally:
        if old is None:
            os.environ.pop("QIANFAN_API_KEY", None)
        else:
            os.environ["QIANFAN_API_KEY"] = old


def test_fetch_trending_requires_api_key():
    with patch(
        "channel.wechat_desktop.daily_hot.baidu_hot.resolve_qianfan_api_key",
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
        "channel.wechat_desktop.daily_hot.baidu_hot.resolve_qianfan_api_key",
        return_value="test-key",
    ), patch(
        "channel.wechat_desktop.daily_hot.baidu_hot.requests.get",
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
        "channel.wechat_desktop.daily_hot.baidu_hot.resolve_qianfan_api_key",
        return_value="test-key",
    ), patch(
        "channel.wechat_desktop.daily_hot.baidu_hot.requests.post",
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
        "channel.wechat_desktop.daily_hot.baidu_hot.resolve_chat_endpoint",
        return_value={
            "api_base": "https://api.example.com/v1",
            "api_key": "sk-test",
            "model": "demo-model",
        },
    ), patch(
        "channel.wechat_desktop.daily_hot.baidu_hot.requests.post",
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
        "channel.wechat_desktop.daily_hot.baidu_hot.fetch_trending",
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
        "channel.wechat_desktop.daily_hot.baidu_hot.fetch_hot_detail",
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
        "channel.wechat_desktop.daily_hot.baidu_hot.summarize_hot_with_commentary",
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
        "channel.wechat_desktop.daily_hot.baidu_hot.fetch_trending",
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
        "channel.wechat_desktop.daily_hot.baidu_hot.fetch_hot_detail",
        return_value={
            "ok": True,
            "references": [{"title": "报道", "content": "详细经过在此。"}],
        },
    ), patch(
        "channel.wechat_desktop.daily_hot.baidu_hot.summarize_hot_with_commentary",
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

    def enqueue(message: str, fire_date: str) -> int:
        assert fire_date == "2026-08-03"
        enqueued.append(message)
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
        enqueue_callback=lambda message, fire_date: called.append(message) or 1,
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
        enqueue_callback=lambda message, fire_date: enqueued.append(message) or 1,
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


def test_conversation_titles_match_ignores_member_count_suffix():
    assert strip_member_count_suffix("小小地下联络站(9)") == "小小地下联络站"
    assert strip_member_count_suffix("小小地下联络站（12）") == "小小地下联络站"
    assert strip_member_count_suffix("小小地下联络站") == "小小地下联络站"
    assert conversation_titles_match("小小地下联络站", "小小地下联络站(9)")
    assert conversation_titles_match("小小地下联络站(9)", "小小地下联络站（10）")
    assert not conversation_titles_match("小小地下联络站", "测试群(3)")


def test_resolve_conversation_selector_matches_unique_title():
    row = ConversationInfo(
        conversation_title="小小地下联络站",
        runtime_id="42.1.2.3",
        row_index=2,
    )
    selectors = {"uia-session:42.1.2.3": row}
    selector = resolve_conversation_selector(selectors, "小小地下联络站")
    assert selector.title == "小小地下联络站"
    assert selector.runtime_id == "42.1.2.3"
    assert selector.row_index == 2


def test_resolve_conversation_selector_matches_title_with_member_suffix():
    row = ConversationInfo(
        conversation_title="小小地下联络站",
        runtime_id="42.1.2.3",
        row_index=0,
    )
    selectors = {"uia-session:42.1.2.3": row}
    selector = resolve_conversation_selector(selectors, "小小地下联络站(9)")
    assert selector.runtime_id == "42.1.2.3"


def test_resolve_conversation_selector_ignores_stale_uia_session_key():
    selector = resolve_conversation_selector({}, "uia-session:missing")
    assert selector.title == ""
    assert selector.runtime_id == ""
