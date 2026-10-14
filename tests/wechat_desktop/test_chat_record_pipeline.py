"""仅显式引用合并转发才读取回复；原记录静默保存，读取失败不启动 Agent。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.contracts import ConversationTarget, SendResult, SendStatus, TargetResolution, TargetStatus
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.prompts import _render_event_context, _reply_requirements
from .helpers import PipelineStoreStub, _bare_wechat_channel


TITLE = "南街和九月底的聊天记录"
BODY = "南街: 明天上午九点开会\n九月底: 收到，会议室见"
UNAVAILABLE_REPLY = "目前没有读到这份合并转发记录的正文，请把需要处理的消息单独发给我。"
NATIVE_XML_UNAVAILABLE_REPLY = "目前没有读到这条应用消息的正文，请展开后重新发送。"


def record_event(*, quoted=True, status="success", content=BODY, entries=None, truncated=False):
    payload = {
        "title": TITLE,
        "content": content,
        "status": status,
        "entries": [{"sender_name": "南街", "content": "明天上午九点开会"}]
        if entries is None else entries,
        "truncated": truncated,
    }
    event = WechatDesktopEvent(
        "message", "stable-session", "九月底", "sender", "九月底",
        "text" if quoted else "chat_record", "总结这份记录" if quoted else TITLE,
        source_type="private", history=[{"content": "不相关的当前会话历史"}],
    )
    if quoted:
        event.reference = {
            "content_type": "chat_record", "content": TITLE, "sender_name": "九月底",
            "resolved": True, "chat_record": payload,
        }
    else:
        event.chat_record = payload
    return event


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("status,truncated", [("success", False), ("partial", True)])
def test_ready_record_renders_extracted_body_without_whole_conversation_history(quoted, status, truncated):
    event = record_event(quoted=quoted, status=status, truncated=truncated)

    heading, lines, history = _render_event_context(event)
    rendered = "\n".join([heading, *lines])

    assert BODY in rendered
    assert "合并转发聊天记录正文" in rendered
    assert "不相关的当前会话历史" not in rendered
    assert history == []
    requirements = _reply_requirements(event)
    assert "禁止" in requirements and "wechat_history" in requirements
    assert "search_contacts" in requirements and "read_history" in requirements
    if truncated:
        assert "部分" in rendered and "补全" in requirements


@pytest.fixture
def reply_channel(monkeypatch):
    channel = _bare_wechat_channel()
    channel.config = {"chat_record_unavailable_reply": UNAVAILABLE_REPLY, "self_display_name": "Bot"}
    channel.config["native_xml_unavailable_reply"] = NATIVE_XML_UNAVAILABLE_REPLY
    channel._reply_queue = WechatReplyQueue()
    channel._service = SimpleNamespace(status=lambda: {"paused": False}, update_status=lambda **kwargs: None)
    channel._policy = SimpleNamespace(allows_send=lambda *args, **kwargs: True)
    sent, stages, history, failures = [], [], [], []
    channel._trace = lambda *args, **kwargs: None
    channel._store = PipelineStoreStub(
        audit=lambda *args, **kwargs: None,
        append_conversation_history=lambda **kwargs: history.append(kwargs),
    )
    channel._driver = SimpleNamespace(
        resolve_send_target=lambda identity: TargetResolution(
            TargetStatus.RESOLVED, ConversationTarget("stable-session", "九月底", False)
        ),
        validate_reply_target=lambda event: SimpleNamespace(valid=True, reason=""),
    )
    channel._deliver = lambda target, text, **kwargs: sent.append((target, text, kwargs)) or {
        "success": True, "verified": True,
    }
    channel._mark_lifecycle = lambda event_ids, stage, **kwargs: stages.append(stage)
    channel._send_agent_failure_notice = lambda **kwargs: failures.append(kwargs)
    channel._dispatch_message = lambda *args: pytest.fail("正文不可用的合并转发记录不能启动 Agent")
    monkeypatch.setattr(
        AgentReplyCoordinator, "wait_for_reply",
        lambda *args: pytest.fail("正文不可用的合并转发记录不能等待 Agent 超时"),
    )
    return channel, sent, stages, history, failures


def process(channel, event):
    assert channel._reply_queue.enqueue(event)
    item = channel._reply_queue.get()
    terminal = channel._process_reply_item(item)
    channel._reply_queue.finish(item, terminal)
    return terminal


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("status,content,entries", [
    ("unavailable", "", []),
    ("invalid", "", []),
    ("success", "", [{"content": "不能只信结构存在"}]),
    ("success", TITLE, []),
    ("partial", "  ", [{"content": "不能把空白当正文"}]),
])
def test_unavailable_record_sends_one_prepared_reply_and_finishes_without_agent(
    reply_channel, quoted, status, content, entries
):
    channel, sent, stages, history, failures = reply_channel
    event = record_event(quoted=quoted, status=status, content=content, entries=entries)

    if not quoted:
        assert process(channel, event) == "observed"
        assert sent == stages == history == failures == []
        return

    assert process(channel, event) == "completed"
    assert [(target, text) for target, text, _ in sent] == [("stable-session", UNAVAILABLE_REPLY)]
    assert len(history) == 1 and history[0]["content"] == UNAVAILABLE_REPLY
    assert "agent_started" not in stages and "agent_done" not in stages
    assert failures == []


@pytest.mark.parametrize("source_type", ["private", "group"])
@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("status", ["unavailable", "invalid"])
def test_unknown_native_xml_failure_finishes_once_without_agent(reply_channel, source_type, quoted, status):
    channel, sent, stages, history, failures = reply_channel
    event = record_event(quoted=quoted)
    event.source_type = source_type
    event.is_group = source_type == "group"
    event.chat_record = {}
    if quoted:
        event.reference = {"content_type": "app_message", "content": "[应用消息正文不可用]",
                           "native_xml_status": status, "resolved": False}
    else:
        event.content_type = "app_message"
        event.content = "[应用消息正文不可用]"
        event.attachment_status = "native_xml_unavailable"

    if not quoted:
        assert process(channel, event) == "observed"
        assert sent == stages == history == failures == []
        return

    assert process(channel, event) == "completed"
    assert len(sent) == 1 and sent[0][1] == NATIVE_XML_UNAVAILABLE_REPLY
    assert len(history) == 1 and "agent_started" not in stages
    assert failures == []


def test_unknown_native_xml_failure_cannot_bypass_dispatch_gate():
    event = record_event()
    event.reference = {"content_type": "app_message", "native_xml_status": "unavailable"}
    channel = SimpleNamespace(config={}, _compose_context=lambda *args, **kwargs: pytest.fail("进入Agent"))

    assert AgentReplyCoordinator(channel).dispatch(event) is False


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("invalid_when", ["already_invalid", "attachment_invalid", "reference_invalid", "before_send"])
def test_invalid_source_record_is_silent(reply_channel, quoted, invalid_when):
    channel, sent, stages, history, failures = reply_channel
    event = record_event(quoted=quoted, status="unavailable", content="", entries=[])
    if invalid_when == "already_invalid":
        event.task.source_invalid = True
    elif invalid_when == "attachment_invalid":
        event.attachment_status = "source_invalid"
    elif invalid_when == "reference_invalid":
        if not quoted:
            event.attachment_status = "source_invalid"
        else:
            event.reference["fetch_status"] = "source_invalid"
    else:
        channel._driver.validate_reply_target = lambda event: SimpleNamespace(valid=False, reason="source_changed")

    expected = "observed" if not quoted and invalid_when == "before_send" else "skipped"
    assert process(channel, event) == expected
    assert sent == history == failures == []


@pytest.mark.parametrize("gate", ["paused", "blacklist", "shadow", "expired"])
def test_unavailable_record_obeys_existing_send_gates(reply_channel, gate):
    channel, sent, _, history, failures = reply_channel
    event = record_event(status="unavailable", content="", entries=[])
    assert channel._reply_queue.enqueue(event)
    item = channel._reply_queue.get()
    if gate == "paused":
        channel._service.status = lambda: {"paused": True}
    elif gate == "blacklist":
        channel._policy.allows_send = lambda *args: False
    elif gate == "shadow":
        from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
        channel.config["shadow_mode"] = True
        channel._policy = WechatDesktopPolicy(channel.config, channel._store)
    else:
        channel._reply_queue.expire(item.token)

    assert channel._process_reply_item(item) == "skipped"
    assert sent == history == failures == []


@pytest.mark.parametrize("ready_after", [False, True])
def test_deferred_record_is_decided_after_materialization(reply_channel, monkeypatch, ready_after):
    channel, sent, stages, _, failures = reply_channel
    event = record_event(status="unavailable", content="", entries=[])
    event.task.deferred_materialization_events = [event]
    order = []
    channel._send_deferred_attachment_notice = lambda item: pytest.fail("物化前不再发送附件预测通知")

    def materialize(events):
        order.append("materialize")
        result = deepcopy(events[0])
        if ready_after:
            result.reference["chat_record"].update(
                status="success", content=BODY, entries=[{"content": "明天上午九点开会"}],
            )
        return result

    channel._materialize_batch = materialize
    if ready_after:
        channel._dispatch_message = lambda event, token: order.append("dispatch") or True
        monkeypatch.setattr(AgentReplyCoordinator, "wait_for_reply", lambda *args: "completed")

    assert process(channel, event) == "completed"
    assert order == (["materialize", "dispatch"] if ready_after else ["materialize"])
    assert len(sent) == (0 if ready_after else 1)
    assert failures == []


def test_deferred_record_read_failure_still_finishes_with_prepared_reply(reply_channel):
    channel, sent, stages, _, failures = reply_channel
    event = record_event(status="unavailable", content="", entries=[])
    event.task.deferred_materialization_events = [event]
    channel._send_deferred_attachment_notice = lambda item: False

    def materialize(events):
        raise RuntimeError("模拟有界前置读取失败")

    channel._materialize_batch = materialize

    assert process(channel, event) == "completed"
    assert len(sent) == 1 and sent[0][1] == UNAVAILABLE_REPLY
    assert "agent_started" not in stages and failures == []


@pytest.mark.parametrize("source_type", ["private", "group"])
@pytest.mark.parametrize("status", ["success", "unavailable"])
def test_queued_standalone_record_skips_notice_materialization_and_agent(reply_channel, source_type, status):
    channel, sent, stages, history, failures = reply_channel
    event = record_event(quoted=False, status=status, content=BODY if status == "success" else "")
    event.source_type = source_type
    event.is_group = source_type == "group"
    event.task.deferred_materialization_events = [event]
    channel._send_deferred_attachment_notice = lambda item: pytest.fail("原合并记录不能发送读取通知")
    channel._materialize_batch = lambda events: pytest.fail("原合并记录不能打开 UIA 记录窗口")

    assert process(channel, event) == "observed"
    assert sent == stages == history == failures == []


@pytest.mark.parametrize("source_type", ["private", "group"])
@pytest.mark.parametrize("content_type", ["image", "file", "share_card"])
def test_queued_standalone_attachment_cannot_bypass_scan_gate(reply_channel, source_type, content_type):
    channel, sent, stages, history, failures = reply_channel
    event = record_event(quoted=False)
    event.content_type = content_type
    event.chat_record = {}
    event.source_type = source_type
    event.is_group = source_type == "group"
    event.task.deferred_materialization_events = [event]
    channel._send_deferred_attachment_notice = lambda item: pytest.fail("原附件不能发送读取通知")
    channel._materialize_batch = lambda events: pytest.fail("回复 FIFO 不负责原附件的缓存")

    assert process(channel, event) == "observed"
    assert sent == stages == history == failures == []


@pytest.mark.parametrize("content_type", ["image", "file", "share_card", "chat_record"])
def test_expired_standalone_attachment_remains_skipped(reply_channel, content_type):
    channel, sent, stages, history, failures = reply_channel
    event = record_event(quoted=False)
    event.content_type = content_type
    assert channel._reply_queue.enqueue(event)
    item = channel._reply_queue.get()
    channel._reply_queue.expire(item.token)

    assert channel._process_reply_item(item) == "skipped"
    assert sent == stages == history == failures == []


@pytest.mark.parametrize("send_status,terminal", [
    (SendStatus.SENT, "completed"),
    (SendStatus.UNVERIFIED, "uncertain"),
    (SendStatus.PARTIAL, "partial"),
    (SendStatus.UNCERTAIN, "uncertain"),
    (SendStatus.NOT_SENT, "failed"),
])
def test_prepared_record_reply_uses_actual_send_terminal_without_second_notice(
    reply_channel, send_status, terminal
):
    channel, sent, _, _, failures = reply_channel
    event = record_event(status="unavailable", content="", entries=[])

    def deliver(target, text, **kwargs):
        sent.append((target, text, kwargs))
        return SendResult(send_status)

    channel._deliver = deliver
    assert process(channel, event) == terminal
    assert len(sent) == 1 and failures == []


@pytest.mark.parametrize("source_type", ["private", "group"])
@pytest.mark.parametrize("quoted", [False, True])
def test_dispatch_ready_record_marks_current_run_and_preserves_body(source_type, quoted):
    event = record_event(quoted=quoted)
    event.source_type = source_type
    event.is_group = source_type == "group"
    captured = {}
    channel = SimpleNamespace(
        config={}, _driver=SimpleNamespace(), _trace=lambda *args, **kwargs: None,
        _compose_context=lambda ctype, content, **kwargs: captured.update(content=content, kwargs=kwargs),
    )

    assert AgentReplyCoordinator(channel).dispatch(event) is False
    if not quoted:
        assert captured == {}
        return
    assert BODY in captured["content"]
    assert "不相关的当前会话历史" not in captured["content"]
    assert captured["kwargs"]["wechat_desktop_chat_record"] is True
    assert captured["kwargs"]["wechat_desktop_is_reference"] is True
    assert set(captured["kwargs"]["wechat_desktop_disabled_tools"]) == {
        "wechat_history", "wechat_desktop", "bash",
    }


def test_dispatch_does_not_trust_record_title_or_old_resolved_flag():
    event = record_event(status="unavailable", content="", entries=[])
    captured = []
    channel = SimpleNamespace(
        config={}, _driver=SimpleNamespace(), _trace=lambda *args, **kwargs: None,
        _compose_context=lambda *args, **kwargs: captured.append(args),
    )

    assert AgentReplyCoordinator(channel).dispatch(event) is False
    assert captured == []


def test_legacy_resolved_app_card_title_does_not_start_agent(reply_channel):
    channel, sent, _, _, _ = reply_channel
    event = record_event(status="unavailable", content="", entries=[])
    event.reference = {"content_type": "text", "native_type": 49, "resolved": True, "content": TITLE}

    assert process(channel, event) == "completed"
    assert len(sent) == 1 and sent[0][1] == UNAVAILABLE_REPLY


@pytest.mark.parametrize("source_type", ["private", "group"])
@pytest.mark.parametrize("content_type", ["image", "file", "share_card", "chat_record"])
def test_dispatch_standalone_attachment_does_not_compose_agent_context(source_type, content_type):
    event = record_event(quoted=False)
    event.content_type = content_type
    event.source_type = source_type
    event.is_group = source_type == "group"
    channel = SimpleNamespace(
        config={}, _driver=SimpleNamespace(), _trace=lambda *args, **kwargs: None,
        _compose_context=lambda *args, **kwargs: pytest.fail("原特殊消息不能创建 Agent 上下文"),
    )

    assert AgentReplyCoordinator(channel).dispatch(event) is False


def test_quoted_record_isolation_and_tool_flags_apply_only_to_current_reply(monkeypatch, tmp_path):
    from channel.wechat_desktop.pipeline.session_context import text_of, user_message
    from .test_agent_session_context import _FakeAgent, _assistant, _bridge, _context

    captured = {}
    channel = SimpleNamespace(
        config={}, _driver=SimpleNamespace(), _trace=lambda *args, **kwargs: None,
        _compose_context=lambda ctype, content, **kwargs: captured.update(content=content, kwargs=kwargs),
    )
    old = [user_message("旧会话正文"), _assistant("旧回复")]
    agent = _FakeAgent(old)
    bridge, store = _bridge(monkeypatch, tmp_path, agent)
    store.append_messages("synthetic_wechat", old, channel_type="wechat_desktop")
    event = record_event(quoted=True)
    AgentReplyCoordinator(channel).dispatch(event)
    bridge.agent_reply(captured["content"], _context(**captured["kwargs"]))

    assert agent.run_calls[0][0] == []
    assert [text_of(message) for message in store.load_messages("synthetic_wechat")[:2]] == [
        "旧会话正文", "旧回复",
    ]

    normal = WechatDesktopEvent("message", "stable-session", "九月底", "sender", "九月底", "text", "接着聊")
    AgentReplyCoordinator(channel).dispatch(normal)
    assert captured["kwargs"]["wechat_desktop_is_reference"] is False
    assert captured["kwargs"]["wechat_desktop_chat_record"] is False
    assert captured["kwargs"]["wechat_desktop_disabled_tools"] == []
    bridge.agent_reply(captured["content"], _context(**captured["kwargs"]))
    assert agent.run_calls[1][0]
    assert event.content in text_of(agent.run_calls[1][0][0])
