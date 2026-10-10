"""默认准入和分会话黑名单回归；仅使用临时数据库与发送替身。"""

import threading
from types import SimpleNamespace

import pytest

from bridge.reply import Reply, ReplyType
from channel.wechat_desktop.config import load_wechat_desktop_config
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .helpers import _bare_wechat_channel


@pytest.fixture
def store(tmp_path):
    ledger = WechatDesktopStore(str(tmp_path / "blacklists.sqlite3"))
    yield ledger
    ledger._get_connection().close()


def native_event(*, name="同名会话", group=False, at=False, sender="群成员"):
    return WechatDesktopEvent(
        "message", "native-group" if group else "native-private", name,
        "member-id", sender, "text", "@机器人 帮忙" if at else "帮忙",
        is_group=group, is_at=at, source_type="group" if group else "private",
        source_message_id="native-message-1", direction="incoming", receipt_phase="live",
    )


def admission_channel(store, **overrides):
    channel = _bare_wechat_channel()
    channel.config = load_wechat_desktop_config(overrides)
    channel._store = store
    channel._policy = WechatDesktopPolicy(channel.config, store)
    channel._trace = lambda *args, **kwargs: None
    channel._finish_lifecycle = lambda *args, **kwargs: None
    scheduled = []
    channel._submit_materialization = scheduled.extend
    channel._defer_private_event = scheduled.append
    return channel, scheduled


@pytest.mark.parametrize("at,expected", [(True, True), (False, False)])
def test_new_group_is_admitted_by_default_and_keeps_at_trigger(store, at, expected):
    channel, scheduled = admission_channel(store)
    event = native_event(name="萌新打怪躺平日记", group=True, at=at)
    assert store.record_event(event)

    channel._process_received_event(event)

    assert scheduled == ([event] if expected else [])


@pytest.mark.parametrize("content", ["@机器人 帮忙", "/cow 帮忙", "/cancel", "/steer 调整任务"])
def test_group_blacklist_rejects_mentions_and_control_commands_before_scheduling(store, content):
    channel, scheduled = admission_channel(store, auto_reply_group_blacklist=["萌新打怪躺平日记"])
    event = native_event(name="萌新打怪躺平日记", group=True, at=True)
    event.content = content
    channel._dispatch_control_event = lambda event: pytest.fail("黑名单群不能触发控制命令")
    assert store.record_event(event)

    channel._process_received_event(event)

    assert scheduled == []
    state = store.event_state(event.event_id)
    assert state["state"] == "skipped"
    assert state["reason"] == "blacklist"


@pytest.mark.parametrize("blocked_group", [False, True])
@pytest.mark.parametrize("group", [False, True])
def test_admission_blacklists_do_not_cross_same_named_private_and_group(store, blocked_group, group):
    key = "auto_reply_group_blacklist" if blocked_group else "auto_reply_private_blacklist"
    channel, scheduled = admission_channel(store, **{key: ["同名会话"]})
    event = native_event(group=group, at=True)
    assert store.record_event(event)

    channel._process_received_event(event)

    blocked = group == blocked_group
    assert scheduled == ([] if blocked else [event])
    if blocked:
        assert store.event_state(event.event_id)["reason"] == "blacklist"


def test_private_blacklist_does_not_suppress_group_member_with_same_name(store):
    channel, scheduled = admission_channel(store, auto_reply_private_blacklist=["群成员"])
    event = native_event(group=True, at=True, sender="群成员")
    assert store.record_event(event)

    channel._process_received_event(event)

    assert scheduled == [event]


@pytest.mark.parametrize("notice", ["tool", "failure"])
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
def test_blacklist_is_rechecked_before_tool_and_failure_notices(store, notice, group, blocked):
    channel = _bare_wechat_channel()
    channel.config = load_wechat_desktop_config({
        "shadow_mode": False,
        "agent_tool_notice_templates": ["正在调用 {tool_name}"],
        "agent_failure_notice_templates": ["请重新尝试"],
    })
    channel._store = store
    channel._policy = WechatDesktopPolicy(channel.config, store)
    channel._service = SimpleNamespace(status=lambda: {"paused": False})
    channel._stop_event = threading.Event()
    channel._trace = lambda *args, **kwargs: None
    channel._mark_lifecycle = lambda *args, **kwargs: None
    channel._reply_queue = WechatReplyQueue()
    event = native_event(group=group, at=True)
    assert channel._reply_queue.enqueue(event)
    item = channel._reply_queue.get()
    deliveries = []
    channel._driver = SimpleNamespace(send_interim_text=lambda target, content: (
        deliveries.append((target, content)) or {"success": True, "verified": True}
    ))
    context = {
        "msg": SimpleNamespace(event=event, other_user_nickname="伪造显示名", other_user_id="forged-id"),
        "receiver": "forged-receiver",
        "isgroup": not group,
        "wechat_desktop_queue_token": item.token,
        "wechat_desktop_source_event_ids": [event.event_id],
        "wechat_desktop_source_type": event.source_type,
    }
    # 入队后修改名单，确保进行中任务也必须在实际通知前重新检查。
    blocked_key = "auto_reply_group_blacklist" if (group if blocked else not group) else "auto_reply_private_blacklist"
    channel.config[blocked_key] = [event.conversation_name]

    if notice == "tool":
        sent = AgentReplyCoordinator(channel).send_tool_notice(context, {"tool_name": "web_search"})
    else:
        sent = channel._send_agent_failure_notice(context=context, reason="合成失败")

    assert sent is not blocked
    assert len(deliveries) == (0 if blocked else 1)
    if not blocked:
        assert deliveries[0][0] == event.conversation_id
    assert len(store.list_conversation_history(event.conversation_id)) == (0 if blocked else 1)


@pytest.mark.parametrize("reply_type", [ReplyType.TEXT, ReplyType.IMAGE])
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("blocked", [False, True])
def test_final_reply_uses_native_event_identity_and_kind_over_forged_context(store, reply_type, group, blocked):
    channel = _bare_wechat_channel()
    channel.config = load_wechat_desktop_config({"shadow_mode": False, "auto_send_images": True})
    channel._store = store
    channel._policy = WechatDesktopPolicy(channel.config, store)
    channel._service = SimpleNamespace(status=lambda: {"paused": False})
    channel._trace = lambda *args, **kwargs: None
    channel._mark_lifecycle = lambda *args, **kwargs: None
    channel._reply_queue = WechatReplyQueue()
    event = native_event(group=group, at=True)
    assert channel._reply_queue.enqueue(event)
    item = channel._reply_queue.get()
    validations, deliveries = [], []
    channel._validate_source_before_reply = lambda *args: validations.append(args) or True
    channel._deliver = lambda *args, **kwargs: deliveries.append((args, kwargs)) or {
        "status": "sent", "success": True, "verified": True,
    }
    context = {
        "msg": SimpleNamespace(event=event, other_user_nickname="伪造显示名", other_user_id="forged-id"),
        "receiver": "forged-receiver", "isgroup": not group,
        "wechat_desktop_queue_token": item.token,
        "wechat_desktop_source_event_ids": [event.event_id],
        "wechat_desktop_source_type": event.source_type,
    }
    key = "auto_reply_group_blacklist" if (group if blocked else not group) else "auto_reply_private_blacklist"
    channel.config[key] = [event.conversation_name]

    channel._send_reply_impl(Reply(reply_type, "synthetic reply"), context)

    assert len(deliveries) == len(validations) == (0 if blocked else 1)
    assert context["wechat_desktop_queue_terminal"] == ("skipped" if blocked else "completed")
    if not blocked:
        args, kwargs = deliveries[0]
        assert args[0] == event.conversation_id
        assert kwargs["policy_target"] == event.conversation_name
        assert kwargs["is_group"] is group
