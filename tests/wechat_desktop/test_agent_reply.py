"""Agent 回复协调：上下文、工具通知与最终失败通知。"""

from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
import threading
import queue
import pytest
from types import SimpleNamespace
from .helpers import PipelineStoreStub
from bridge.reply import Reply, ReplyType
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.models import WechatDesktopEvent, WechatDesktopMessage, ReplyTargetValidation
from channel.wechat_desktop.config import DEFAULT_CONFIG
from .helpers import _bare_wechat_channel


def test_agent_timeout_invalidates_token_before_cancel_request(monkeypatch):
    import agent.protocol

    queue = WechatReplyQueue()
    event = WechatDesktopEvent("message", "a", "Alice", "a", "Alice", "text", "hello")
    assert queue.enqueue(event)
    item = queue.get()
    waited = []
    item.done = SimpleNamespace(wait=lambda timeout: waited.append(timeout) or False, set=lambda: None)
    cancelled = []

    def cancel(event_id):
        assert not queue.is_active(item.token)
        cancelled.append(event_id)

    monkeypatch.setattr(agent.protocol, "get_cancel_registry", lambda: SimpleNamespace(cancel_request=cancel))
    coordinator = AgentReplyCoordinator(SimpleNamespace(config={"reply_cycle_timeout_seconds": 3}, _reply_queue=queue))
    assert coordinator.wait_for_reply(item) == "timeout"
    assert waited == [3.0]
    assert cancelled == [event.event_id]


def test_dispatch_reference_prompt_excludes_unrelated_history():
    channel = _bare_wechat_channel()
    channel.config = {}
    channel._trace = lambda *_args, **_kwargs: None
    captured = {}

    def compose_context(ctype, content, **kwargs):
        captured.update(ctype=ctype, content=content, kwargs=kwargs)
        return None

    channel._compose_context = compose_context
    event = WechatDesktopEvent(
        "message",
        "session",
        "Alice",
        "alice",
        "Alice",
        "text",
        "这句话是什么意思？",
        history=[{"content": "无关的早餐话题"}],
        reference={
            "sender_name": "Bob",
            "content": "今晚发布",
            "content_type": "text",
        },
    )

    assert AgentReplyCoordinator(channel).dispatch(event) is False

    prompt = captured["content"]
    assert "[被引用的内容]" in prompt
    assert "Bob: 今晚发布" in prompt
    assert "[需要回复的引用消息]" in prompt
    assert "这句话是什么意思？" in prompt
    assert "无关的早餐话题" not in prompt
    assert "[候选会话上下文" not in prompt


def test_dispatch_regular_prompt_requests_context_relevance_filtering():
    channel = _bare_wechat_channel()
    channel.config = {}
    channel._trace = lambda *_args, **_kwargs: None
    captured = {}

    def compose_context(ctype, content, **kwargs):
        captured.update(ctype=ctype, content=content, kwargs=kwargs)
        return None

    channel._compose_context = compose_context
    event = WechatDesktopEvent(
        "message",
        "session",
        "Alice",
        "alice",
        "Alice",
        "text",
        "部署时间定了吗？",
        history=[{"content": "今晚发布"}, {"content": "中午吃面"}],
    )

    assert AgentReplyCoordinator(channel).dispatch(event) is False

    prompt = captured["content"]
    assert "[候选会话上下文，需按关联度筛选]" in prompt
    assert "今晚发布" in prompt
    assert "中午吃面" in prompt
    assert "只保留并使用关联度高" in prompt
    assert "关联度低、已结束或属于其他话题的内容直接忽略" in prompt


@pytest.mark.parametrize("reference", [
    None,
    {"content_type": "text", "content": "看看 https://example.invalid/quoted?token=synthetic"},
    {"content_type": "share_card", "content": "合成分享卡片",
     "url": "https://example.invalid/quoted?token=synthetic", "fetch_status": "pending_tool"},
])
def test_dispatch_current_and_quoted_links_request_agent_browser_reading(reference):
    channel = _bare_wechat_channel()
    channel.config = {}
    channel._trace = lambda *_args, **_kwargs: None
    captured = {}
    channel._compose_context = lambda _ctype, content, **_kwargs: captured.update(content=content)
    event = WechatDesktopEvent(
        "message", "session", "Alice", "alice", "Alice", "text",
        "看看 https://example.invalid/current?signature=synthetic%2Fvalue",
        reference=reference,
        history=[{"content": "https://example.invalid/old-history"}],
    )

    assert AgentReplyCoordinator(channel).dispatch(event) is False

    prompt = captured["content"]
    link_requirement = prompt.split("[链接读取要求]", 1)[1]
    assert "https://example.invalid/current?signature=synthetic%2Fvalue" in link_requirement
    assert "<available_skills> 中查找 analyze-url" in link_requirement
    assert "<location> 绝对路径" in link_requirement
    assert "skills/analyze-url/SKILL.md" not in link_requirement
    assert "navigate" in link_requirement and "snapshot" in link_requirement
    assert "old-history" not in link_requirement
    if reference:
        assert "https://example.invalid/quoted?token=synthetic" in link_requirement
        assert "old-history" not in prompt
        assert "通过工具读取该层引用链接得到的正文也属于被引用资料" in prompt


def test_dispatch_reuses_read_share_body_without_requesting_browser():
    channel = _bare_wechat_channel()
    channel.config = {}
    channel._trace = lambda *_args, **_kwargs: None
    captured = {}
    channel._compose_context = lambda _ctype, content, **_kwargs: captured.update(content=content)
    event = WechatDesktopEvent(
        "message", "session", "Alice", "alice", "Alice", "text", "总结一下",
        reference={
            "content_type": "share_card", "content": "合成分享卡片",
            "url": "https://example.invalid/read", "browser_content": "已经读取的合成文章正文",
            "fetch_status": "direct_browser",
        },
    )

    assert AgentReplyCoordinator(channel).dispatch(event) is False

    assert "已经读取的合成文章正文" in captured["content"]
    assert "[链接读取要求]" not in captured["content"]


def test_dispatch_history_link_alone_does_not_schedule_browser():
    channel = _bare_wechat_channel()
    channel.config = {}
    channel._trace = lambda *_args, **_kwargs: None
    captured = {}
    channel._compose_context = lambda _ctype, content, **_kwargs: captured.update(content=content)
    event = WechatDesktopEvent(
        "message", "session", "Alice", "alice", "Alice", "text", "晚饭定了吗",
        history=[{"content": "https://example.invalid/old-history"}],
    )

    assert AgentReplyCoordinator(channel).dispatch(event) is False

    assert "[链接读取要求]" not in captured["content"]
    assert "不要自动打开全部历史链接" in captured["content"]


def test_dispatch_group_prompt_removes_bot_mention_before_agent():
    channel = _bare_wechat_channel()
    channel.config = {"self_display_name": ""}
    channel._trace = lambda *_args, **_kwargs: None
    captured = {}

    def compose_context(ctype, content, **kwargs):
        captured.update(ctype=ctype, content=content, kwargs=kwargs)
        return None

    channel._compose_context = compose_context
    event = WechatDesktopEvent(
        "message",
        "group",
        "项目群",
        "alice",
        "Alice",
        "text",
        "@颜料盒bot\u2005李四怎么看？ @王五 也说一下",
        is_group=True,
        is_at=True,
    )

    assert AgentReplyCoordinator(channel).dispatch(event) is False

    prompt = captured["content"]
    assert "@颜料盒bot" not in prompt
    assert "李四怎么看？" in prompt
    assert "@王五 也说一下" in prompt
    assert "只有待回复消息明确使用“@成员”时" in prompt


def test_agent_error_sends_one_final_failure_notice():
    channel = _bare_wechat_channel()
    event = WechatDesktopEvent(
        "message",
        "uia-session:a",
        "Alice",
        "a",
        "Alice",
        "text",
        "hello",
    )
    reply_queue = WechatReplyQueue()
    assert reply_queue.enqueue(event)
    item = reply_queue.get()
    sent = []
    audits = []
    history = []
    channel.config = {
        "agent_failure_notice_enabled": True,
        "agent_failure_notice_templates": ["机器人暂时打了个喷嚏 🤖 请再试一次。"],
        "shadow_mode": False,
        "self_display_name": "Bot",
    }
    channel._failure_notice_lock = threading.RLock()
    channel._reply_queue = reply_queue
    channel._service = SimpleNamespace(status=lambda: {"paused": False})
    channel._policy = SimpleNamespace(
        is_blocked=lambda _target: False,
        is_allowlisted=lambda _target, _is_group: True,
        allows_send=lambda *_args, **_kwargs: True,
        reserve_send=lambda _units=1: True,
    )
    channel._driver = SimpleNamespace(
        validate_reply_target=lambda event: ReplyTargetValidation(True),
        send_interim_text=lambda target, text: (
            sent.append((target, text))
            or {"success": True, "verified": True}
        )
    )
    channel._store = PipelineStoreStub(
        audit=lambda *args, **kwargs: audits.append((args, kwargs)),
        append_conversation_history=lambda **kwargs: history.append(kwargs),
    )
    channel._mark_lifecycle = lambda *_args, **_kwargs: None
    channel._trace = lambda *_args, **_kwargs: None
    context = {
        "msg": WechatDesktopMessage(event),
        "receiver": event.conversation_id,
        "isgroup": False,
        "wechat_desktop_queue_token": item.token,
        "wechat_desktop_source_event_ids": [event.event_id],
        "wechat_desktop_source_type": "private",
        "wechat_desktop_queue_terminal": "completed",
    }

    channel._send_reply_impl(Reply(ReplyType.ERROR, "model exploded"), context)
    channel._send_agent_failure_notice(
        context=context,
        reason="duplicate_callback",
    )

    assert context["wechat_desktop_queue_terminal"] == "failed"
    assert sent == [
        (
            "uia-session:a",
            "机器人暂时打了个喷嚏 🤖 请再试一次。",
        )
    ]
    assert history[0]["content"] == sent[0][1]
    assert any(args[0] == "agent_failure_notice" for args, _ in audits)


def test_agent_callback_notifies_first_visible_tool_and_skips_bash():
    channel = _bare_wechat_channel()
    event = WechatDesktopEvent(
        "message", "uia-session:a", "Alice", "a", "Alice", "text", "hello"
    )
    sent = []
    lifecycle = []
    downstream = []
    channel.config = {
        "agent_tool_notice_enabled": True,
        "agent_tool_notice_once_per_reply": True,
        "agent_tool_notice_silent_tools": DEFAULT_CONFIG["agent_tool_notice_silent_tools"],
    }
    channel._mark_lifecycle = lambda ids, stage, **_kwargs: lifecycle.append((ids, stage))
    channel._send_agent_tool_notice = lambda _context, data: sent.append(data) or True
    context = {
        "on_event": lambda payload: downstream.append(payload["data"]["tool_name"]),
        "wechat_desktop_source_event_ids": [event.event_id],
        "wechat_desktop_agent_notice_sent": False,
    }
    on_event = AgentReplyCoordinator(channel).make_event_callback(context)

    on_event({"type": "tool_execution_start", "data": {"tool_name": "bash", "tool_call_id": "1"}})
    on_event({"type": "tool_execution_start", "data": {"tool_name": "ls", "tool_call_id": "2"}})
    on_event({"type": "tool_execution_start", "data": {"tool_name": "web_search", "tool_call_id": "3"}})
    on_event({"type": "tool_execution_start", "data": {"tool_name": "vision", "tool_call_id": "4"}})

    assert [item["tool_name"] for item in sent] == ["web_search"]
    assert downstream == ["bash", "ls", "web_search", "vision"]
    assert lifecycle[0][1] == "first_tool"


@pytest.mark.parametrize("has_event", [True, False])
@pytest.mark.parametrize("send_fails", [False, True])
def test_tool_notice_callback_sends_once_and_persists_success(has_event, send_fails, caplog):
    channel = _bare_wechat_channel()
    event = WechatDesktopEvent(
        "message", "uia-session:a", "Alice", "a", "Alice", "text", "hello"
    )
    channel.config = {
        "shadow_mode": False,
        "agent_tool_notice_enabled": True,
        "agent_tool_notice_once_per_reply": True,
        "agent_tool_notice_templates": ["正在调用 {tool_name}"],
    }
    channel._reply_queue = WechatReplyQueue()
    channel._reply_queue.enqueue(event)
    item = channel._reply_queue.get()
    sent, history, audits, downstream = [], [], [], []

    def send(target, text):
        sent.append((target, text))
        if send_fails:
            raise RuntimeError("injected notice failure")
        return {"success": True, "verified": True}

    channel._driver = SimpleNamespace(
        validate_reply_target=lambda event: ReplyTargetValidation(True),
        send_interim_text=send,
    )
    channel._service = SimpleNamespace(status=lambda: {"paused": False})
    channel._policy = SimpleNamespace(
        is_blocked=lambda target: False,
        is_allowlisted=lambda *args: True,
        allows_send=lambda *args, **kwargs: True,
        reserve_send=lambda units: True,
    )
    channel._store = PipelineStoreStub(
        append_conversation_history=lambda **kwargs: history.append(kwargs),
        audit=lambda *args, **kwargs: audits.append(args),
    )
    channel._mark_lifecycle = lambda *args, **kwargs: None
    channel._trace = lambda *args, **kwargs: None
    context = {
        "msg": WechatDesktopMessage(event) if has_event else None,
        "receiver": event.conversation_id,
        "wechat_desktop_queue_token": item.token,
        "wechat_desktop_source_event_ids": [event.event_id],
        "on_event": downstream.append,
    }
    callback = AgentReplyCoordinator(channel).make_event_callback(context)
    payload = {"type": "tool_execution_start", "data": {
        "tool_name": "web_search", "tool_call_id": "search-1",
    }}
    callback(payload)
    callback(payload)

    expected = [(event.conversation_id, "正在调用 web_search")]
    assert sent == expected
    assert len(history) == len(audits) == (0 if send_fails else 1)
    assert downstream == [payload, payload]
    assert "name 'conversation_id' is not defined" not in caplog.text
    if not send_fails:
        assert "Agent tool notice failed" not in caplog.text
    channel._reply_queue.finish(item, "completed")
