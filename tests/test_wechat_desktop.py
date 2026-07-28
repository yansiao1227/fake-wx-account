import hashlib
import json

import pytest

from agent.tools.wechat_desktop.wechat_desktop_tool import WechatDesktopTool
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.policy import WechatDesktopPolicy
from channel.wechat_desktop.service import reset_wechat_desktop_service_for_tests
from channel.wechat_desktop.store import WechatDesktopStore
from channel.wechat_desktop.wechat_desktop_channel import _normalize_auto_reply_text
from channel.wechat_desktop.wechat_desktop_message import WechatDesktopMessage






def test_event_ledger_deduplicates_across_store_reopen(tmp_path):
    path = str(tmp_path / "wechat.sqlite3")
    event = WechatDesktopEvent(
        kind="message",
        conversation_id="c1",
        conversation_name="Alice",
        sender_id="alice",
        sender_name="Alice",
        content_type="text",
        content="hello",
        bounds=(10, 20, 100, 50),
    )
    assert WechatDesktopStore(path).record_event(event) is True
    same = WechatDesktopEvent(
        kind="message",
        conversation_id="c1",
        conversation_name="Alice",
        sender_id="alice",
        sender_name="Alice",
        content_type="text",
        content="hello",
        bounds=(10, 20, 100, 50),
    )
    assert WechatDesktopStore(path).record_event(same) is False


def test_image_event_fingerprint_uses_bytes_not_capture_path(tmp_path):
    from PIL import Image

    first_path = tmp_path / "capture-a.png"
    second_path = tmp_path / "capture-b.png"
    Image.new("RGB", (10, 10), "red").save(first_path)
    Image.new("RGB", (10, 10), "red").save(second_path)
    base = dict(
        kind="message",
        conversation_id="c1",
        conversation_name="Alice",
        sender_id="alice",
        sender_name="Alice",
        content_type="image",
        bounds=(10, 20, 100, 80),
    )
    first = WechatDesktopEvent(content=str(first_path), **base)
    second = WechatDesktopEvent(content=str(second_path), **base)
    assert first.fingerprint() == second.fingerprint()


def test_policy_requires_allowlist_and_non_shadow(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    config = {
        "shadow_mode": False,
        "auto_reply_contacts": ["Alice"],
        "auto_reply_groups": ["Team"],
        "max_send_per_minute": 2,
        "max_send_per_hour": 10,
    }
    policy = WechatDesktopPolicy(config, store)
    assert policy.can_auto_send("Alice", False, "text") is True
    assert policy.can_auto_send("Mallory", False, "text") is False
    assert policy.can_auto_send("Alice", False, "image") is False
    assert policy.can_auto_send("Alice", False, "text") is True
    assert policy.can_auto_send("Alice", False, "text") is False


def test_policy_can_allow_all_private_and_group_conversations(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    policy = WechatDesktopPolicy(
        {
            "shadow_mode": False,
            "auto_reply_private_all": True,
            "auto_reply_groups_all": True,
            "auto_reply_contacts": [],
            "auto_reply_groups": [],
            "max_send_per_minute": 5,
            "max_send_per_hour": 60,
        },
        store,
    )
    assert policy.is_allowlisted("Any contact", False) is True
    assert policy.is_allowlisted("Any group", True) is True
    assert policy.can_auto_send("Any contact", False, "text") is True
    assert policy.can_auto_send("Any group", True, "text") is True


def test_policy_blacklist_overrides_allow_all(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    policy = WechatDesktopPolicy(
        {
            "shadow_mode": False,
            "auto_reply_private_all": True,
            "auto_reply_blacklist": ["腾讯新闻"],
            "max_send_per_minute": 5,
            "max_send_per_hour": 60,
        },
        store,
    )
    assert policy.is_blocked("腾讯新闻") is True
    assert policy.can_auto_send("腾讯新闻", False, "text") is False
    assert policy.can_auto_send("Alice", False, "text") is True


def test_conversation_history_persists_and_deduplicates_recent_outgoing(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    assert store.has_conversation_history("alice") is False
    event = WechatDesktopEvent(
        kind="message",
        conversation_id="alice",
        conversation_name="Alice",
        sender_id="alice",
        sender_name="Alice",
        content_type="text",
        content="hello",
        direction="incoming",
        source_type="private",
    )
    assert store.append_event_history(event) is True
    assert store.has_conversation_history("alice") is True
    assert store.has_conversation_history("bob") is False
    assert store.append_event_history(event) is False
    assert store.append_conversation_history(
        "alice", "Alice", "我", "outgoing", "text", "hi", "private"
    ) is True
    assert store.append_conversation_history(
        "alice", "Alice", "我", "outgoing", "text", "hi", "private"
    ) is False
    assert store.append_conversation_history(
        "alice",
        "Alice",
        "我",
        "outgoing",
        "text",
        "可以回：**“see you”**",
        "private",
    ) is True
    assert store.normalize_outgoing_history(_normalize_auto_reply_text) == 1
    history = store.list_conversation_history("alice")
    assert [item["content"] for item in history] == ["hello", "hi", "see you"]
    history_without_event = store.list_conversation_history(
        "alice", exclude_source_event_id=event.event_id
    )
    assert [item["content"] for item in history_without_event] == ["hi", "see you"]


def test_outgoing_text_anchors_are_scoped_to_conversation_name(tmp_path):
    store = WechatDesktopStore(str(tmp_path / "wechat.sqlite3"))
    store.append_conversation_history(
        "group-1", "同名群", "我", "outgoing", "text", "第一条回复", "group"
    )
    store.append_conversation_history(
        "group-1", "同名群", "我", "outgoing", "text", "第二条回复", "group"
    )
    store.append_conversation_history(
        "group-2", "另一个群", "我", "outgoing", "text", "不应返回", "group"
    )

    assert store.list_outgoing_texts_by_name("同名群") == [
        "第二条回复",
        "第一条回复",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("可以回复：\n\n**“今晚八点见～”**", "今晚八点见～"),
        ("可以回：吃火锅吧", "吃火锅吧"),
        ("建议回复：\n> 周末应该挺多人", "周末应该挺多人"),
        ("直接说正文", "直接说正文"),
    ],
)
def test_auto_reply_text_removes_drafting_wrappers(value, expected):
    assert _normalize_auto_reply_text(value) == expected


































def test_group_trigger_defaults_to_at_or_prefix(tmp_path):
    policy = WechatDesktopPolicy(
        {"group_reply_mode": "at_or_prefix", "group_command_prefixes": ["/cow"]},
        WechatDesktopStore(str(tmp_path / "wechat.sqlite3")),
    )
    base = dict(
        kind="message",
        conversation_id="g1",
        conversation_name="Team",
        sender_id="u1",
        sender_name="User",
        content_type="text",
        is_group=True,
    )
    assert policy.group_triggered(WechatDesktopEvent(content="/cow status", **base))
    assert policy.group_triggered(WechatDesktopEvent(content="hello", is_at=True, **base))
    assert not policy.group_triggered(WechatDesktopEvent(content="hello", **base))


def test_group_trigger_at_only_rejects_prefix_without_mention(tmp_path):
    policy = WechatDesktopPolicy(
        {"group_reply_mode": "at_only", "group_command_prefixes": ["/cow"]},
        WechatDesktopStore(str(tmp_path / "wechat-at-only.sqlite3")),
    )
    base = dict(
        kind="message",
        conversation_id="g1",
        conversation_name="Team",
        sender_id="u1",
        sender_name="User",
        content_type="text",
        is_group=True,
    )
    assert not policy.group_triggered(
        WechatDesktopEvent(content="/cow status", **base)
    )
    assert policy.group_triggered(
        WechatDesktopEvent(content="@小牛 hello", is_at=True, **base)
    )






















def test_wechat_desktop_tool_routes_send_through_channel_executor(
    tmp_path, monkeypatch
):
    service = reset_wechat_desktop_service_for_tests(
        str(tmp_path / "wechat-tool.sqlite3")
    )
    calls = []
    service.set_agent_executor(
        lambda action, **params: calls.append((action, params))
        or {
            "status": "sent",
            "verified": True,
            "conversation": params["conversation"],
        }
    )
    monkeypatch.setattr(
        "agent.tools.wechat_desktop.wechat_desktop_tool.get_wechat_desktop_service",
        lambda: service,
    )
    tool = WechatDesktopTool()

    result = tool.execute(
        {
            "action": "send_text",
            "conversation": "颜料盒",
            "text": "今晚八点见",
            "is_group": False,
        }
    )

    assert result.status == "success"
    assert json.loads(result.result)["status"] == "sent"
    assert calls == [
        (
            "send_text",
            {
                "conversation": "颜料盒",
                "text": "今晚八点见",
                "is_group": False,
            },
        )
    ]














def test_wechat_message_maps_group_fields():
    event = WechatDesktopEvent(
        kind="message",
        conversation_id="group",
        conversation_name="Test Group",
        sender_id="alice",
        sender_name="Alice",
        content_type="text",
        content="@bot hello",
        is_group=True,
        is_at=True,
    )
    message = WechatDesktopMessage(event)
    assert message.is_group is True
    assert message.is_at is True
    assert message.other_user_id == "group"
    assert message.actual_user_nickname == "Alice"


def test_web_status_handler_has_no_approval_state(monkeypatch, tmp_path):
    from channel.web import web_channel
    import channel.wechat_desktop.service as service_module

    service = reset_wechat_desktop_service_for_tests(str(tmp_path / "wechat.sqlite3"))
    monkeypatch.setattr(service_module, "get_wechat_desktop_service", lambda: service)
    monkeypatch.setattr(web_channel, "_require_auth", lambda: None)
    monkeypatch.setattr(web_channel.web, "header", lambda *args, **kwargs: None)

    status = json.loads(web_channel.WechatDesktopStatusHandler().GET())
    assert status["status"] == "success"
    assert "pending_approvals" not in status["desktop"]


def test_web_desktop_endpoints_require_loopback_or_password(monkeypatch):
    from channel.web import web_channel

    class DummyHTTPError(Exception):
        def __init__(self, status, headers=None, data=""):
            super().__init__(status)

    monkeypatch.setattr(web_channel, "_require_auth", lambda: None)
    monkeypatch.setattr(web_channel, "_is_password_enabled", lambda: False)
    monkeypatch.setattr(web_channel, "conf", lambda: {"web_host": "0.0.0.0"})
    # Some tests replace the web.py module with a minimal process-wide stub.
    monkeypatch.setattr(web_channel.web, "HTTPError", DummyHTTPError, raising=False)

    with pytest.raises(DummyHTTPError) as exc:
        web_channel._require_wechat_desktop_security()
    assert "403 Forbidden" in str(exc.value)

    monkeypatch.setattr(web_channel, "_is_password_enabled", lambda: True)
    web_channel._require_wechat_desktop_security()
