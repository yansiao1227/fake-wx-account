"""只使用合成会话验证微信执行输入精简，不启动 UI 或真实模型。"""

from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

import agent.memory as memory_module
import bridge.agent_bridge as bridge_module
from agent.memory.conversation_store import ConversationStore
from bridge.agent_bridge import AgentBridge, AgentLLMModel
from bridge.agent_initializer import AgentInitializer
from bridge.context import Context, ContextType
from bridge.reply import ReplyType
from channel.wechat_desktop.pipeline.session_context import (
    USER_SOURCE,
    compact_session_messages,
    current_run_messages,
    is_wechat_auto_reply,
    prepare_agent_session,
    preserve_image_pointers,
    text_of,
    user_message,
)
from agent.protocol import LLMRequest


def _assistant(text, *other_blocks):
    return {"role": "assistant", "content": [{"type": "text", "text": text}, *other_blocks]}


def _legacy_prompt(user="旧原消息", history="不应注入的旧候选历史"):
    return (
        "[候选会话上下文，需按关联度筛选]\n历史消息: " + history
        + "\n[需要回复的新消息]\n" + user
        + "\n[回复要求]\n像本人聊天一样直接回复正文，尽量自然、简短、口语化。"
        + "不要复述筛选过程。\n[链接读取要求]\n其他旧回复指令"
    )


def _compact(messages, **kwargs):
    return compact_session_messages(messages, max_turns=kwargs.get("max_turns", 2),
                                    max_chars=kwargs.get("max_chars", 1500),
                                    is_reference=kwargs.get("is_reference", False))


def _context(**overrides):
    values = dict(
        channel_type="wechat_desktop", wechat_desktop_auto_reply=True,
        session_id="synthetic_wechat", wechat_desktop_user_message="新的原消息",
        wechat_desktop_is_reference=False,
        wechat_desktop_session_max_turns=2, wechat_desktop_session_max_chars=1500,
    )
    values.update(overrides)
    return Context(ContextType.TEXT, "", values)


def test_latest_two_turns_keep_original_and_final_only():
    messages = []
    for i in range(4):
        messages.extend([
            user_message(f"原消息 {i}"),
            _assistant("中间工具说明", {"type": "thinking", "thinking": "思考不该注入"},
                       {"type": "tool_use", "id": f"t{i}", "name": "bash", "input": {}}),
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                            "content": "旧工具正文" * 1000}]},
            _assistant(f"最终回复 {i}", {"type": "thinking", "thinking": "旧思考"}),
        ])
    original = deepcopy(messages)
    compact = _compact(messages)
    assert [text_of(msg) for msg in compact] == ["原消息 2", "最终回复 2", "原消息 3", "最终回复 3"]
    assert messages == original
    assert all(block["type"] == "text" for msg in compact for block in msg["content"])


def test_total_character_budget_and_latest_turn_priority():
    messages = [user_message("甲" * 1000), _assistant("乙" * 1000),
                user_message("最新问句"), _assistant("最新答案")]
    compact = _compact(messages, max_chars=1500)
    assert sum(len(text_of(message)) for message in compact) <= 1500
    assert [text_of(msg) for msg in compact][-2:] == ["最新问句", "最新答案"]
    assert len(compact) == 4
    assert "[已截断]" in text_of(compact[0])
    assert "[已截断]" in text_of(compact[1])


@pytest.mark.parametrize("max_turns,max_chars,is_reference", [(0, 1500, False), (2, 0, False), (2, 1500, True)])
def test_disabled_or_reference_old_history_is_empty(max_turns, max_chars, is_reference):
    messages = [user_message("旧话题"), _assistant("旧答复")]
    assert _compact(messages, max_turns=max_turns, max_chars=max_chars, is_reference=is_reference) == []


def test_legacy_fixed_prompt_is_migrated_without_old_injection():
    messages = [{"role": "user", "content": [{"type": "text", "text": _legacy_prompt()}]},
                _assistant("旧最终答复")]
    compact = _compact(messages)
    assert [text_of(msg) for msg in compact] == ["旧原消息", "旧最终答复"]
    assert compact[0]["content"][0]["source"] == USER_SOURCE


def test_legacy_reference_prompt_keeps_only_the_actual_question():
    prompt = (
        "[被引用的内容]\n张三: 不该复用整页引用正文"
        "\n[需要回复的引用消息]\n请总结"
        "\n[回复要求]\n像本人聊天一样直接回复正文，尽量自然、简短、口语化。只看这一层"
    )
    compact = _compact([{"role": "user", "content": prompt}, _assistant("合成摘要")])
    assert [text_of(msg) for msg in compact] == ["请总结", "合成摘要"]


def test_unknown_restored_user_formats_are_not_injected():
    messages = [{"role": "user", "content": "未知全量包装\n[需要回复的新消息]\n似是而非"},
                _assistant("不能借这个未知格式推断上下文"),
                user_message("可靠原文"), _assistant("可靠答复")]
    assert [text_of(msg) for msg in _compact(messages)] == ["可靠原文", "可靠答复"]


def test_unfinished_tool_turn_does_not_reuse_earlier_assistant_preface():
    messages = [user_message("执行工具"), _assistant("稍等，我来查"),
                _assistant("工具调用说明", {"type": "tool_use", "id": "unfinished", "name": "bash", "input": {}}),
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "unfinished", "content": "工具异常"}]}]
    assert _compact(messages) == []


def test_known_internal_tool_hint_does_not_split_final_from_original_on_restart(tmp_path):
    hint = "工具已成功执行并返回结果。请基于这些信息向用户做出回复，不要重复调用相同的工具。"
    messages = [user_message("需要调用工具的原消息"),
                _assistant("工具步骤", {"type": "tool_use", "id": "tool", "name": "bash", "input": {}}),
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool", "content": "合成结果"}]},
                {"role": "user", "content": [{"type": "text", "text": hint}]},
                _assistant("最终回复")]
    assert [text_of(msg) for msg in _compact(messages)] == ["需要调用工具的原消息", "最终回复"]
    store = ConversationStore(tmp_path / "tool_hint.db")
    store.append_messages("synthetic", messages, channel_type="wechat_desktop")
    restored = AgentInitializer._filter_text_only_messages(store.load_messages("synthetic"))
    assert [text_of(msg) for msg in _compact(restored)] == ["需要调用工具的原消息", "最终回复"]
    assert hint in str(store.load_messages("synthetic"))


def test_same_tool_hint_in_marked_original_is_not_swallowed():
    hint = "工具已成功执行并返回结果。请基于这些信息向用户做出回复，不要重复调用相同的工具。"
    messages = [user_message("上一轮原文"), _assistant("上一轮答复"),
                user_message(hint), _assistant("真实用户的这条原文答复")]
    restored = AgentInitializer._filter_text_only_messages(messages)
    assert [text_of(msg) for msg in _compact(restored)] == [
        "上一轮原文", "上一轮答复", hint, "真实用户的这条原文答复",
    ]


def test_other_channel_tool_hint_restore_keeps_existing_behavior():
    hint = "工具已成功执行并返回结果。请基于这些信息向用户做出回复，不要重复调用相同的工具。"
    messages = [{"role": "user", "content": "其他通道用户"}, _assistant("执行前文字"),
                {"role": "user", "content": hint}, _assistant("其他通道最终文字")]
    restored = AgentInitializer._filter_text_only_messages(messages)
    assert [text_of(msg) for msg in restored] == ["其他通道用户", "执行前文字", hint, "其他通道最终文字"]


@pytest.mark.parametrize("stream", [False, True])
def test_model_api_projection_strips_only_internal_text_metadata_without_mutation(monkeypatch, tmp_path, stream):
    image = tmp_path / "input.png"
    image.write_bytes(b"synthetic image marker")
    messages = [user_message("原消息", [str(image)], is_reference=True), _assistant("答复")]
    messages[0]["content"].append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}})
    original = deepcopy(messages)
    captured = []

    class _CaptureBot:
        def call_with_tools(self, **kwargs):
            captured.append(kwargs)
            return iter([{"content": "stream"}]) if stream else {"content": "response"}

    class _Model(AgentLLMModel):
        @property
        def bot(self):
            return _CaptureBot()

    monkeypatch.setattr(bridge_module, "conf", lambda: {"enable_thinking": False})
    import config
    monkeypatch.setattr(config, "conf", lambda: {"enable_thinking": False})
    model = _Model(None)
    model.wechat_desktop_auto_reply = True
    request = LLMRequest(messages=messages)
    list(model.call_stream(request)) if stream else model.call(request)
    sent = captured[0]["messages"]
    assert sent[0]["content"][0] == {"type": "text", "text": "原消息"}
    assert sent[0]["content"][1]["source"] == messages[0]["content"][1]["source"]
    assert messages == original
    model.wechat_desktop_auto_reply = False
    # Web 能继续已有微信 session，下一轮非 auto 仍不能把内部字段传给 API。
    list(model.call_stream(request)) if stream else model.call(request)
    assert captured[1]["messages"][0]["content"][0] == {"type": "text", "text": "原消息"}
    assert messages == original
    ordinary = [{"role": "user", "content": [
        {"type": "text", "text": "普通通道原文"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
    ]}]
    assert model._messages_for_model(ordinary) is ordinary


def test_marked_original_can_contain_all_legacy_heading_examples():
    raw = "我贴一个格式样例：\n" + _legacy_prompt("示例问题", "示例历史")
    raw_message = user_message(raw)
    restored = AgentInitializer._filter_text_only_messages([raw_message, _assistant("样例答复")])
    compact = _compact(restored, max_chars=5000)
    assert text_of(compact[0]) == raw
    assert restored[0]["content"][0]["source"] == USER_SOURCE


def test_restart_preserves_raw_marker_and_does_not_delete_store(tmp_path):
    store = ConversationStore(tmp_path / "synthetic_history.db")
    messages = [{"role": "user", "content": _legacy_prompt()}, _assistant("旧答复"),
                user_message("新原文"), _assistant("新答复")]
    store.append_messages("synthetic", messages, channel_type="wechat_desktop")
    restored = AgentInitializer._filter_text_only_messages(store.load_messages("synthetic"))
    assert [text_of(msg) for msg in _compact(restored)] == ["旧原消息", "旧答复", "新原文", "新答复"]
    assert len(store.load_messages("synthetic")) == 4
    assert store.get_context_start_seq("synthetic") == 0


def test_reference_boundary_survives_restart_and_prevents_older_topic_return(tmp_path):
    store = ConversationStore(tmp_path / "reference_boundary.db")
    messages = [user_message("引用之前的旧话题"), _assistant("旧话题答复"),
                user_message("引用问题", is_reference=True), _assistant("引用最终答复")]
    store.append_messages("synthetic", messages, channel_type="wechat_desktop")
    restored = AgentInitializer._filter_text_only_messages(store.load_messages("synthetic"))
    assert [text_of(msg) for msg in _compact(restored)] == ["引用问题", "引用最终答复"]
    assert len(store.load_messages("synthetic")) == 4


@pytest.mark.parametrize("context", [None, _context(channel_type="web"),
                                    _context(wechat_desktop_auto_reply=False),
                                    _context(is_scheduled_task=True),
                                    _context(session_id="scheduler_synthetic")])
def test_non_wechat_auto_reply_or_scheduler_is_unchanged(context):
    messages = [{"role": "user", "content": "其他通道原本的消息"}, _assistant("完整答复")]
    agent = SimpleNamespace(messages=deepcopy(messages), messages_lock=threading.Lock())
    assert not is_wechat_auto_reply(context)
    assert not prepare_agent_session(agent, context)
    assert agent.messages == messages


def test_current_run_boundary_is_independent_of_executor_trimmed_counts():
    old = [user_message("旧轮次"), _assistant("旧答复")]
    current = [{"role": "user", "content": "本轮完整prompt"},
               _assistant("本轮中间说明", {"type": "tool_use", "id": "current", "name": "bash", "input": {}}),
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "current", "content": "当前正文"}]},
               _assistant("本轮最终答复")]
    assert current_run_messages(old + current, "本轮完整prompt", current[-1:]) == current


def test_image_artifact_pointer_survives_compaction_and_restart(tmp_path):
    image = tmp_path / "tmp" / "generated.png"
    image.parent.mkdir()
    image.write_bytes(b"synthetic image marker")
    messages = [user_message("画一张图"), _assistant("画好了")]
    messages = preserve_image_pointers(messages, [{"file_type": "image", "path": str(image)}])
    store = ConversationStore(tmp_path / "image_history.db")
    store.append_messages("synthetic", messages, channel_type="wechat_desktop")
    restored = AgentInitializer._filter_text_only_messages(store.load_messages("synthetic"))
    compact = _compact(restored)
    assert str(image) in text_of(compact[-1])
    assert _compact(restored, is_reference=True) == []
    assert "画好了" in text_of(compact[-1])


def test_known_tool_image_artifact_retains_only_pointer(tmp_path):
    image = tmp_path / "tmp" / "generated.png"
    image.parent.mkdir()
    image.write_bytes(b"synthetic image marker")
    import json
    output = json.dumps({"images": [{"url": str(image)}]})
    messages = [user_message("作图"),
                _assistant("工具调用", {"type": "tool_use", "id": "img", "name": "bash", "input": {}}),
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "img", "content": output}]},
                _assistant("好了")]
    compact = _compact(messages)
    assert str(image) in text_of(compact[-1])
    assert "工具调用" not in str(compact)
    assert "images" not in str(compact)


def test_input_attachment_path_survives_long_text_truncation_and_restart(tmp_path):
    attachment = tmp_path / "input.txt"
    attachment.write_text("synthetic document", encoding="utf-8")
    messages = [user_message("长原文" * 1000, [str(attachment)]), _assistant("附件答复" * 1000)]
    restored = AgentInitializer._filter_text_only_messages(messages)
    compact = _compact(restored)
    assert str(attachment) in text_of(compact[-1])
    assert sum(len(text_of(msg)) for msg in compact) <= 1500


class _FakeAgent:
    def __init__(self, messages, files=None, fail=False):
        self.messages = deepcopy(messages)
        self.messages_lock = threading.Lock()
        self.tools = []
        self.model = SimpleNamespace()
        self.stream_executor = SimpleNamespace(files_to_send=files or [])
        self.run_calls = []
        self.fail = fail

    def run_stream(self, **kwargs):
        self.run_calls.append((deepcopy(self.messages), kwargs))
        if self.fail:
            self.messages = []
            raise RuntimeError("synthetic run failure")
        current = [{"role": "user", "content": [{"type": "text", "text": kwargs["user_message"]}]},
                   _assistant("当前中间工具说明", {"type": "tool_use", "id": "tool", "name": "bash", "input": {}}),
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool", "content": "当前完整工具正文"}]},
                   _assistant("本轮最终答复")]
        self.messages.extend(current)
        self._last_run_new_messages = current[-1:]  # 合成旧执行器裁剪后的计数偏移。
        return "本轮最终答复"


def _bridge(monkeypatch, tmp_path, agent):
    store = ConversationStore(tmp_path / "bridge_history.db")
    monkeypatch.setattr(memory_module, "get_conversation_store", lambda: store)
    monkeypatch.setattr(bridge_module, "conf", lambda: {"conversation_persistence": True, "enable_thinking": False})
    import config
    monkeypatch.setattr(config, "conf", lambda: {"conversation_persistence": True, "enable_thinking": False})
    import agent.evolution.trigger as trigger
    monkeypatch.setattr(trigger, "mark_run_active", lambda *args: None)
    monkeypatch.setattr(trigger, "note_user_turn", lambda *args, **kwargs: None)
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.get_agent = lambda session_id=None: agent
    bridge._schedule_mcp_hot_reload = lambda target: None
    return bridge, store


def test_bridge_compacts_before_run_and_persists_original_with_full_tool_audit(monkeypatch, tmp_path):
    agent = _FakeAgent([user_message("旧原文"), _assistant("旧答复")])
    bridge, store = _bridge(monkeypatch, tmp_path, agent)
    query = _legacy_prompt("新的原消息", "本轮少量候选历史")
    reply = bridge.agent_reply(query, _context())
    assert reply.type == ReplyType.TEXT
    assert [text_of(msg) for msg in agent.run_calls[0][0]] == ["旧原文", "旧答复"]
    assert agent.run_calls[0][1]["user_message"] == query
    stored = store.load_messages("synthetic_wechat")
    assert text_of(stored[0]) == "新的原消息"
    assert stored[0]["content"][0]["source"] == USER_SOURCE
    assert "当前完整工具正文" in str(stored)
    assert "本轮少量候选历史" not in str(stored)
    assert "当前完整工具正文" in str(agent.messages)
    assert "本轮少量候选历史" not in str(agent.messages)


def test_bridge_reference_ignores_old_agent_history_without_clearing_persistence(monkeypatch, tmp_path):
    old = [user_message("旧话题"), _assistant("旧回复")]
    agent = _FakeAgent(old)
    bridge, store = _bridge(monkeypatch, tmp_path, agent)
    store.append_messages("synthetic_wechat", old, channel_type="wechat_desktop")
    bridge.agent_reply("当前引用的完整prompt", _context(wechat_desktop_is_reference=True), clear_history=True)
    assert agent.run_calls[0][0] == []
    assert agent.run_calls[0][1]["clear_history"] is False
    assert agent._current_session_id is None
    stored = store.load_messages("synthetic_wechat")
    assert [text_of(msg) for msg in stored[:2]] == ["旧话题", "旧回复"]
    assert text_of(stored[2]) == "新的原消息"
    assert store.get_context_start_seq("synthetic_wechat") == 0


def test_bridge_error_does_not_delete_wechat_history(monkeypatch, tmp_path):
    old = [user_message("原消息"), _assistant("答复")]
    agent = _FakeAgent(old, fail=True)
    bridge, store = _bridge(monkeypatch, tmp_path, agent)
    store.append_messages("synthetic_wechat", old, channel_type="wechat_desktop")
    reply = bridge.agent_reply("当前完整prompt", _context(wechat_desktop_is_reference=True))
    assert reply.type == ReplyType.ERROR
    assert [text_of(msg) for msg in store.load_messages("synthetic_wechat")[:2]] == ["原消息", "答复"]


def test_bridge_image_delivery_and_future_pointer_both_survive(monkeypatch, tmp_path):
    image = tmp_path / "generated.png"
    image.write_bytes(b"synthetic image marker")
    agent = _FakeAgent([], files=[{"file_type": "image", "path": str(image)}])
    bridge, store = _bridge(monkeypatch, tmp_path, agent)
    reply = bridge.agent_reply("当前完整作图prompt", _context(wechat_desktop_user_message="画一张图"))
    assert reply.type == ReplyType.IMAGE_URL
    assert reply.content == "file://" + str(image)
    restored = AgentInitializer._filter_text_only_messages(store.load_messages("synthetic_wechat"))
    assert str(image) in text_of(_compact(restored)[-1])


@pytest.mark.parametrize("with_image", [False, True])
def test_bridge_blocks_idle_evolution_through_session_finalization(monkeypatch, tmp_path, with_image):
    import agent.evolution.trigger as trigger

    mark_run_active = trigger.mark_run_active
    note_user_turn = trigger.note_user_turn
    files = []
    if with_image:
        image = tmp_path / "generated.png"
        image.write_bytes(b"synthetic image marker")
        files.append({"file_type": "image", "path": str(image)})
    agent = _FakeAgent([], files=files)
    agent._evo_turns = 1
    bridge, store = _bridge(monkeypatch, tmp_path, agent)
    bridge.agents = {"synthetic_wechat": agent}
    now = [1000.0]
    monkeypatch.setattr(trigger.time, "time", lambda: now[0])
    cfg = SimpleNamespace(min_turns=1, idle_seconds=10)
    transcripts = []

    def evolve(*args, **kwargs):
        with agent.messages_lock:
            transcripts.append(deepcopy(agent.messages))

    monkeypatch.setattr(trigger, "run_evolution_for_session", evolve)
    marks = []
    checkpoints = []

    def mark(target, active):
        marks.append(active)
        mark_run_active(target, active)
        if not active:
            trigger._scan_once(bridge, cfg)

    def scan_checkpoint(phase):
        now[0] = 1100.0  # 模拟本轮已超过 idle_seconds。
        checkpoints.append((phase, agent._evo_run_active))
        trigger._scan_once(bridge, cfg)

    monkeypatch.setattr(trigger, "mark_run_active", mark)
    monkeypatch.setattr(bridge_module.AgentEventHandler, "log_summary", lambda self: scan_checkpoint("summary"))
    persist_messages = bridge._persist_messages

    def persist(*args, **kwargs):
        scan_checkpoint("persist")
        persist_messages(*args, **kwargs)

    def note(target, **kwargs):
        scan_checkpoint("note")
        note_user_turn(target, **kwargs)

    monkeypatch.setattr(bridge, "_persist_messages", persist)
    monkeypatch.setattr(trigger, "note_user_turn", note)
    reply = bridge.agent_reply(_legacy_prompt("新的原消息", "不应进入 evolution 的候选历史"), _context())

    assert reply.type == (ReplyType.IMAGE_URL if with_image else ReplyType.TEXT)
    assert marks == [True, False]
    assert checkpoints == [("summary", True), ("persist", True), ("note", True)]
    assert transcripts == []
    assert agent._evo_run_active is False
    assert agent._evo_last_active == 1100.0
    assert text_of(store.load_messages("synthetic_wechat")[0]) == "新的原消息"
    now[0] = 1200.0
    trigger._scan_once(bridge, cfg)
    assert len(transcripts) == 1
    assert text_of(transcripts[0][0]) == "新的原消息"
    assert "不应进入 evolution 的候选历史" not in str(transcripts[0])
    assert "[回复要求]" not in str(transcripts[0])
    if with_image:
        assert str(image) in text_of(transcripts[0][-1])


@pytest.mark.parametrize("phase", ["run", "summary", "sanitize", "persist", "note"])
def test_bridge_releases_evolution_flag_when_finalization_fails(monkeypatch, tmp_path, phase):
    import agent.evolution.trigger as trigger
    import channel.wechat_desktop.pipeline.session_context as session_context

    mark_run_active = trigger.mark_run_active
    agent = _FakeAgent([])
    bridge, store = _bridge(monkeypatch, tmp_path, agent)
    marks = []
    active_at_failure = []

    def mark(target, active):
        marks.append(active)
        mark_run_active(target, active)

    def fail(*args, **kwargs):
        active_at_failure.append(agent._evo_run_active)
        raise RuntimeError("synthetic finalization failure")

    monkeypatch.setattr(trigger, "mark_run_active", mark)
    if phase == "run":
        monkeypatch.setattr(agent, "run_stream", fail)
    elif phase == "summary":
        monkeypatch.setattr(bridge_module.AgentEventHandler, "log_summary", fail)
    elif phase == "sanitize":
        monkeypatch.setattr(session_context, "replace_run_user", fail)
    elif phase == "persist":
        monkeypatch.setattr(bridge, "_persist_messages", fail)
    else:
        monkeypatch.setattr(trigger, "note_user_turn", fail)

    reply = bridge.agent_reply(_legacy_prompt(), _context())
    assert reply.type == (ReplyType.TEXT if phase == "note" else ReplyType.ERROR)
    assert active_at_failure == [True]
    assert marks == [True, False]
    assert agent._evo_run_active is False
    assert text_of(store.load_messages("synthetic_wechat")[0]) == "新的原消息"
