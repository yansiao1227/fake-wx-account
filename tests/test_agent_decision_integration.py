"""并行 Jev 与真实 Agent loop 的交接；模型和工具均为本地可控替身。"""

import copy
import json
import threading

import pytest

from agent.decision import runtime
from agent.decision.runtime import CompletionDecision, DecisionRun, RoutingDecision
from agent.protocol.agent import Agent
from agent.protocol.agent_stream import AgentStreamExecutor
from agent.protocol.steer import SteerInbox
from agent.tools.base_tool import BaseTool, ToolResult


def text(value):
    return {"choices": [{"delta": {"content": value}, "finish_reason": "stop"}]}


def tool_call(call_id="call_1", query="第一步"):
    return {"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": call_id,
        "function": {"name": "lookup", "arguments": json.dumps({"query": query})},
    }]}, "finish_reason": "tool_calls"}]}


class ControlledStream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        chunk = next(self.chunks)
        return chunk() if callable(chunk) else chunk

    def close(self):
        self.closed = True


class ScriptedModel:
    model = "mock-model"

    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []
        self.streams = []
        self.before_call = None

    def call_stream(self, request):
        if self.before_call:
            self.before_call(len(self.requests))
        self.requests.append(copy.deepcopy(request))
        stream = ControlledStream(next(self.turns))
        self.streams.append(stream)
        return stream


class LookupTool(BaseTool):
    name = "lookup"
    description = "查询必需证据"
    params = {"type": "object", "properties": {"query": {"type": "string"}}}

    def __init__(self):
        self.calls = []

    def execute(self, params):
        self.calls.append(copy.deepcopy(params))
        return ToolResult.success("证据：" + params["query"])


class ControlledDecision:
    route_wait_seconds = 0
    completion_wait_seconds = 0

    def __init__(self, route="direct", hint="", ready=True, completion=None):
        self.result = RoutingDecision("request-v1", route, ("lookup",) if hint else (), (), .95, hint)
        self.ready = ready
        self.closed = False
        self.invalidated = False
        self.completion = completion
        self.completion_enabled = completion is not None
        self.completion_starts = []
        self.completion_resolves = 0
        self.route_resolves = []
        self.resolve_error = None

    def resolve(self, wait_seconds=0):
        self.route_resolves.append(wait_seconds)
        if self.resolve_error:
            raise self.resolve_error
        if self.closed or self.invalidated or not self.ready:
            return None
        return self.result

    def start_completion(self, goal, evidence):
        self.completion_starts.append((goal, copy.deepcopy(evidence)))
        return self.completion_enabled and bool(evidence)

    def resolve_completion(self, wait_seconds=0):
        self.completion_resolves += 1
        return self.completion

    def invalidate(self):
        self.invalidated = True
        self.closed = True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def isolate_external_services(monkeypatch):
    import config
    import agent.tools

    monkeypatch.setattr(config, "conf", lambda: {})
    monkeypatch.setattr(agent.tools, "ToolManager", lambda: type("Manager", (), {
        "sync_mcp_into_agent": lambda self, executor: None,
    })())


def executor(model, decision=None, messages=None, **kwargs):
    lookup = LookupTool()
    agent = Agent("基础上下文", model=model, tools=[lookup], enable_skills=False)
    events = []
    instance = AgentStreamExecutor(
        agent, model, agent.system_prompt, agent.tools, messages=messages,
        max_turns=8, on_event=events.append, decision_run=decision, **kwargs,
    )
    return instance, events, lookup


def streamed_text(events):
    return "".join(event["data"].get("delta", "") for event in events
                   if event["type"] == "message_update")


def history_text(messages):
    return "\n".join(block.get("text", "") for message in messages
                     for block in message.get("content", []) if isinstance(block, dict))


def assert_tool_chain(messages, expected_ids):
    uses = []
    results = []
    for message in messages:
        for block in message["content"]:
            if block.get("type") == "tool_use":
                uses.append(block["id"])
            if block.get("type") == "tool_result":
                results.append(block["tool_use_id"])
                assert not block.get("is_error")
    assert uses == results == expected_ids


def direct_answer(value):
    return ToolResult.success({
        "direct_final_answer": True,
        "final_answer_text": value,
    })


def test_direct_tool_answer_obeys_completion_continue_once():
    decision = ControlledDecision(completion=CompletionDecision("continue", .98))
    model = ScriptedModel([tool_call()], [tool_call("call_2", "补充验证")])
    instance, events, lookup = executor(model, decision)

    def execute(params):
        lookup.calls.append(copy.deepcopy(params))
        return direct_answer("遗漏验证的现成答复" if len(lookup.calls) == 1 else "验证后的现成答复")

    lookup.execute = execute
    assert instance.run_stream("查询并验证") == "验证后的现成答复"
    assert len(model.requests) == 2
    assert len(decision.completion_starts) == decision.completion_resolves == 1
    assert decision.completion_starts[0] == ("查询并验证", [{
        "tool": "lookup", "status": "success",
        "content": json.dumps({
            "direct_final_answer": True,
            "final_answer_text": "遗漏验证的现成答复",
        }, ensure_ascii=False),
    }])
    assert "内部完成复核" in model.requests[1].system
    assert "遗漏验证的现成答复" not in history_text(instance.messages)
    assert "遗漏验证的现成答复" not in streamed_text(events)
    assert sum(bool(event["data"].get("completion_replan")) for event in events) == 1
    assert_tool_chain(instance.messages, ["call_1", "call_2"])
    assert decision.closed


@pytest.mark.parametrize("verdict", ["finalize", "uncertain", None])
def test_direct_tool_answer_checks_completion_without_extra_summary(verdict):
    completion = CompletionDecision(verdict, .98) if verdict else None
    decision = ControlledDecision(completion=completion)
    # A missing result represents a started check that has not completed yet.
    decision.completion_enabled = True
    model = ScriptedModel([tool_call()])
    instance, events, lookup = executor(model, decision)
    lookup.execute = lambda params: direct_answer("工具现成答复")
    assert instance.run_stream("查询") == "工具现成答复"
    assert len(model.requests) == 1
    assert len(decision.completion_starts) == decision.completion_resolves == 1
    assert_tool_chain(instance.messages, ["call_1"])
    assert history_text(instance.messages).endswith("工具现成答复")


def test_cancel_during_direct_tool_execution_prevents_ready_reply():
    cancel = threading.Event()
    decision = ControlledDecision(completion=CompletionDecision("finalize", .98))
    model = ScriptedModel([tool_call()])
    instance, events, lookup = executor(model, decision, cancel_event=cancel)

    def execute(params):
        cancel.set()
        return direct_answer("已取消任务的现成答复")

    lookup.execute = execute
    response = instance.run_stream("查询")
    assert "已取消任务的现成答复" not in response
    assert "已取消任务的现成答复" not in history_text(instance.messages)
    assert decision.completion_resolves == 0
    assert decision.closed
    assert len(model.requests) == 1
    assert events[-1]["data"]["cancelled"] is True
    assert_tool_chain(instance.messages, ["call_1"])


def test_cancel_during_initial_route_wait_prevents_main_model_request():
    cancel = threading.Event()
    decision = ControlledDecision()
    model = ScriptedModel([text("取消后不应开始的请求")])
    instance, events, lookup = executor(model, decision, cancel_event=cancel)

    def resolve(wait_seconds=0):
        cancel.set()
        return decision.result

    decision.resolve = resolve
    instance.run_stream("查询")
    assert model.requests == []
    assert streamed_text(events) == ""
    assert lookup.calls == []
    assert decision.closed
    assert events[-1]["data"]["cancelled"] is True
    assert not any(event["type"] == "decision_route" for event in events)


def test_steer_during_initial_route_wait_replaces_hint_before_model_request():
    inbox = SteerInbox()
    decision = ControlledDecision("tools", "旧目标的技能建议")
    model = ScriptedModel([text("新目标答复")])
    instance, events, lookup = executor(model, decision, steer_inbox=inbox)

    def resolve(wait_seconds=0):
        assert inbox.submit("新的目标").accepted
        return decision.result

    decision.resolve = resolve
    assert instance.run_stream("旧目标") == "新目标答复"
    assert decision.invalidated
    assert len(model.requests) == 1
    assert "旧目标的技能建议" not in model.requests[0].system
    assert "新的目标" in json.dumps(model.requests[0].messages, ensure_ascii=False)
    assert lookup.calls == []
    assert not any(event["type"] == "decision_route" for event in events)


def test_steer_before_late_route_keeps_old_stream_buffered_until_safe_checkpoint():
    inbox = SteerInbox()
    decision = ControlledDecision("tools", "旧目标的技能建议", ready=False)

    def redirect():
        assert inbox.submit("新的目标").accepted
        decision.ready = True
        return text("旧目标流的剩余草稿")

    model = ScriptedModel(
        [text("旧目标流的开始草稿"), redirect],
        [text("新目标答复")],
    )
    instance, events, lookup = executor(model, decision, steer_inbox=inbox)

    assert instance.run_stream("旧目标") == "新目标答复"

    assert streamed_text(events) == "新目标答复"
    assert "旧目标流" not in history_text(instance.messages)
    assert not any(event["type"] == "decision_route" for event in events)
    assert "旧目标的技能建议" not in model.requests[1].system
    assert "新的目标" in history_text(instance.messages)
    assert decision.invalidated
    assert lookup.calls == []


@pytest.mark.parametrize("direct", [False, True])
def test_cancel_during_completion_wait_prevents_final_publication(direct):
    cancel = threading.Event()
    decision = ControlledDecision(completion=CompletionDecision("finalize", .98))
    turns = [[tool_call()]] if direct else [[tool_call()], [text("等待检查的草稿")]]
    model = ScriptedModel(*turns)
    instance, events, lookup = executor(model, decision, cancel_event=cancel)
    if direct:
        lookup.execute = lambda params: direct_answer("等待检查的草稿")

    def resolve(wait_seconds=0):
        cancel.set()
        return decision.completion

    decision.resolve_completion = resolve
    response = instance.run_stream("查询")
    assert streamed_text(events) == ""
    assert "等待检查的草稿" not in response
    assert "等待检查的草稿" not in history_text(instance.messages)
    assert not any(event["data"].get("direct_final_answer") for event in events)
    assert events[-1]["data"]["cancelled"] is True
    assert decision.closed
    assert_tool_chain(instance.messages, ["call_1"])
    assert not any(event["type"] == "decision_completion" for event in events)


def test_steer_during_direct_tool_execution_invalidates_completion():
    inbox = SteerInbox()
    decision = ControlledDecision("tools", "旧任务的技能建议",
                                  completion=CompletionDecision("continue", .98))
    model = ScriptedModel([tool_call()], [text("新目标答复")])
    instance, events, lookup = executor(model, decision, steer_inbox=inbox)

    def execute(params):
        assert inbox.submit("新的目标").accepted
        return direct_answer("旧目标现成答复")

    lookup.execute = execute
    assert instance.run_stream("旧目标") == "新目标答复"
    assert decision.invalidated
    assert decision.completion_resolves == 0
    assert "旧目标现成答复" not in history_text(instance.messages)
    assert "旧任务的技能建议" not in model.requests[1].system
    assert "新的目标" in history_text(instance.messages)
    assert inbox.submit("完成后的指令").accepted is False
    assert_tool_chain(instance.messages, ["call_1"])


@pytest.mark.parametrize("verdict", ["continue", "finalize"])
def test_steer_during_direct_completion_wait_discards_previous_goal(verdict):
    inbox = SteerInbox()
    decision = ControlledDecision("tools", "旧任务的技能建议",
                                  completion=CompletionDecision(verdict, .98))
    model = ScriptedModel([tool_call()], [text("追加指令后的答复")])
    instance, events, lookup = executor(model, decision, steer_inbox=inbox)
    lookup.execute = lambda params: direct_answer("旧目标现成答复")

    def resolve(wait_seconds=0):
        assert inbox.submit("新的目标").accepted
        return decision.completion

    decision.resolve_completion = resolve
    assert instance.run_stream("旧目标") == "追加指令后的答复"
    assert decision.invalidated
    assert "旧目标现成答复" not in history_text(instance.messages)
    assert "旧任务的技能建议" not in model.requests[1].system
    assert "内部完成复核" not in model.requests[1].system
    assert not any(event["data"].get("direct_final_answer") for event in events)
    assert_tool_chain(instance.messages, ["call_1"])
    assert not any(event["type"] == "decision_completion" for event in events)
    assert instance._decision_corrections == 0


@pytest.mark.parametrize("direct", [False, True])
def test_finish_at_last_allowed_step_does_not_overwrite_reply_with_summary(direct):
    model = ScriptedModel([tool_call()] if direct else [text("最后一步的答复")])
    instance, events, lookup = executor(model)
    instance.max_turns = 1
    if direct:
        lookup.execute = lambda params: direct_answer("最后一步的答复")
    assert instance.run_stream("查询") == "最后一步的答复"
    assert len(model.requests) == 1
    assert events[-1]["data"]["final_response"] == "最后一步的答复"
    if direct:
        assert_tool_chain(instance.messages, ["call_1"])


def test_direct_completion_continue_at_step_limit_requests_progress_summary():
    decision = ControlledDecision(completion=CompletionDecision("continue", .98))
    model = ScriptedModel([tool_call()], [text("步数已用尽，仍缺少验证")])
    instance, events, lookup = executor(model, decision)
    instance.max_turns = 1
    lookup.execute = lambda params: direct_answer("遗漏验证的现成答复")
    assert instance.run_stream("查询并验证") == "步数已用尽，仍缺少验证"
    assert decision.completion_resolves == 1
    assert len(model.requests) == 2
    assert "内部完成复核" in model.requests[1].system
    assert "遗漏验证的现成答复" not in history_text(instance.messages)
    assert not any(event["data"].get("direct_final_answer") for event in events)
    assert_tool_chain(instance.messages, ["call_1"])


def test_agent_starts_decision_before_context_and_passes_run_filter(monkeypatch):
    import config

    order = []
    decision = ControlledDecision()
    model = ScriptedModel([text("完成")])
    agent = Agent("基础", model=model, enable_skills=False)
    monkeypatch.setattr(config, "conf", lambda: {"agent_decision": {"enabled": True}})

    def start(*args, **kwargs):
        order.append("decision_started")
        assert args[0] == "你好"
        assert kwargs["skill_filter"] == ["chosen-skill"]
        return decision

    def build_context(**kwargs):
        order.append("context_built")
        assert not decision.closed
        return "准备后的上下文"

    monkeypatch.setattr(DecisionRun, "start", start)
    monkeypatch.setattr(agent, "get_full_system_prompt", build_context)
    assert agent.run_stream("你好", skill_filter=["chosen-skill"]) == "完成"
    assert order == ["decision_started", "context_built"]
    assert decision.closed


def test_disabled_decision_preserves_original_tool_loop(monkeypatch):
    monkeypatch.setattr(DecisionRun, "start", lambda *a, **k: pytest.fail("关闭时不应启动 Jev"))
    model = ScriptedModel([tool_call()], [text("已完成")])
    lookup = LookupTool()
    agent = Agent("基础", model=model, tools=[lookup], enable_skills=False)
    monkeypatch.setattr(agent, "get_full_system_prompt", lambda **k: "基础")
    events = []
    assert agent.run_stream("查询", on_event=events.append) == "已完成"
    assert lookup.calls == [{"query": "第一步"}]
    assert streamed_text(events) == "已完成"
    assert_tool_chain(agent.messages, ["call_1"])


def test_late_tool_route_discards_draft_and_closes_provider_without_cancel():
    decision = ControlledDecision("tools", "仅本次运行的技能指引", ready=False)

    def decision_arrives():
        decision.ready = True
        return text("旧草稿尾部")

    model = ScriptedModel([text("旧草稿"), decision_arrives], [tool_call()], [text("已验证答案")])
    instance, events, lookup = executor(model, decision)
    assert instance.run_stream("核实信息") == "已验证答案"
    assert model.streams[0].closed
    assert len(model.requests) == 3
    assert "仅本次运行的技能指引" not in model.requests[0].system
    assert "仅本次运行的技能指引" in model.requests[1].system
    assert "旧草稿" not in streamed_text(events)
    assert "旧草稿" not in history_text(instance.messages)
    assert not any(event["type"] == "agent_cancelled" for event in events)
    assert events[-1]["data"]["cancelled"] is False
    assert lookup.calls == [{"query": "第一步"}]
    assert_tool_chain(instance.messages, ["call_1"])


def test_late_routing_does_not_consume_last_tool_decision_step():
    decision = ControlledDecision("tools", "建议 lookup", ready=False)

    def decision_arrives():
        decision.ready = True
        return text("未发布的草稿")

    model = ScriptedModel([decision_arrives], [tool_call()], [text("步数内工具已执行")])
    instance, events, lookup = executor(model, decision)
    instance.max_turns = 1
    assert instance.run_stream("查询") == "步数内工具已执行"
    assert lookup.calls == [{"query": "第一步"}]
    assert "未发布的草稿" not in history_text(instance.messages)
    assert "未发布的草稿" not in streamed_text(events)
    assert_tool_chain(instance.messages, ["call_1"])


def test_route_arriving_after_tool_call_keeps_existing_call_once():
    decision = ControlledDecision("tools", "建议 lookup", ready=False)

    def decision_arrives():
        decision.ready = True
        return text("正在查询")

    model = ScriptedModel([tool_call(), decision_arrives], [text("已完成")])
    instance, events, lookup = executor(model, decision)
    assert instance.run_stream("查询") == "已完成"
    assert len(model.requests) == 2
    assert lookup.calls == [{"query": "第一步"}]
    assert not any(event["data"].get("decision_replan") for event in events)
    assert_tool_chain(instance.messages, ["call_1"])


def test_uncertain_route_releases_text_before_provider_finishes():
    decision = ControlledDecision("uncertain", ready=False)
    observed = []

    def uncertainty_arrives():
        decision.ready = True
        return text("直接答复")

    def provider_continues():
        observed.append(streamed_text(events))
        return text("结束")

    model = ScriptedModel([uncertainty_arrives, provider_continues])
    instance, events, lookup = executor(model, decision)
    assert instance.run_stream("你好") == "直接答复结束"
    assert observed == ["直接答复"]
    assert len(model.requests) == 1
    assert lookup.calls == []


def test_completion_continue_vetoes_only_once_preserving_tool_pairs():
    decision = ControlledDecision(completion=CompletionDecision("continue", .98))
    model = ScriptedModel(
        [tool_call()], [text("尚无依据的完成草稿")],
        [tool_call("call_2", "补充验证")], [text("证据完整后的回复")],
    )
    instance, events, lookup = executor(model, decision)
    completion_already_started = []
    model.before_call = lambda index: completion_already_started.append(
        (index, len(decision.completion_starts)))
    assert instance.run_stream("查询并验证") == "证据完整后的回复"
    assert completion_already_started[1] == (1, 1)
    assert decision.completion_resolves == 1
    assert len(lookup.calls) == 2
    assert "尚无依据" not in streamed_text(events)
    assert "尚无依据" not in history_text(instance.messages)
    assert streamed_text(events) == "证据完整后的回复"
    assert_tool_chain(instance.messages, ["call_1", "call_2"])
    assert decision.completion_starts[0][0] == "查询并验证"
    assert decision.completion_starts[0][1] == [{
        "tool": "lookup", "status": "success", "content": "证据：第一步",
    }]


def test_completion_finalize_does_not_prevent_main_model_tool_call():
    decision = ControlledDecision(completion=CompletionDecision("finalize", .98))
    model = ScriptedModel([tool_call()], [tool_call("call_2", "仍需检查")], [text("完成")])
    instance, events, lookup = executor(model, decision)
    assert instance.run_stream("查询") == "完成"
    assert len(lookup.calls) == 2
    assert decision.completion_resolves == 1
    assert_tool_chain(instance.messages, ["call_1", "call_2"])


def test_user_cancel_closes_decision_and_stream_without_route_restart():
    cancel = threading.Event()
    decision = ControlledDecision(ready=False)

    def request_cancel():
        cancel.set()
        return text("未提交草稿")

    model = ScriptedModel([request_cancel] + [text("片段")] * 12)
    instance, events, lookup = executor(model, decision, cancel_event=cancel)
    instance.run_stream("查询")
    assert decision.closed
    assert model.streams[0].closed
    assert len(model.requests) == 1
    assert streamed_text(events) == ""
    assert lookup.calls == []
    assert any(event["type"] == "agent_cancelled" for event in events)
    assert not any(event["data"].get("decision_replan") for event in events)
    assert events[-1]["data"]["cancelled"] is True


def test_steer_invalidates_previous_route_and_removes_run_hint():
    inbox = SteerInbox()
    decision = ControlledDecision("tools", "过期技能内容")

    def user_redirects():
        assert inbox.submit("改成直接解释，不再查询").accepted
        return text("原任务草稿")

    model = ScriptedModel([user_redirects], [text("新任务答复")])
    instance, events, lookup = executor(model, decision, steer_inbox=inbox)
    assert instance.run_stream("原任务") == "新任务答复"
    assert decision.invalidated
    assert "过期技能内容" in model.requests[0].system
    assert "过期技能内容" not in model.requests[1].system
    assert lookup.calls == []
    assert not any(event["data"].get("decision_replan") for event in events)
    assert "改成直接解释" in history_text(instance.messages)


def test_steer_removes_buffered_old_goal_draft_from_history():
    inbox = SteerInbox()
    decision = ControlledDecision(ready=False)

    def user_redirects():
        assert inbox.submit("新的目标").accepted
        return text("旧目标未发布草稿")

    model = ScriptedModel([user_redirects], [text("新目标答复")])
    instance, events, lookup = executor(model, decision, steer_inbox=inbox)
    assert instance.run_stream("旧目标") == "新目标答复"
    assert "旧目标未发布草稿" not in history_text(instance.messages)
    assert "旧目标未发布草稿" not in json.dumps(model.requests[1].messages, ensure_ascii=False)
    assert "旧目标未发布草稿" not in streamed_text(events)
    assert decision.invalidated


def test_partial_evidence_invalidates_completion_instead_of_vetoing():
    decision = ControlledDecision(completion=CompletionDecision("continue", .99))
    model = ScriptedModel([tool_call()], [text("主模型答复")])
    instance, events, lookup = executor(model, decision)
    lookup.execute = lambda params: ToolResult.success("长" * 13000)
    assert instance.run_stream("处理长结果") == "主模型答复"
    assert decision.completion_starts == [("处理长结果", [])]
    assert decision.completion_resolves == 0
    assert len(model.requests) == 2


def test_executor_construction_failure_closes_started_decision(monkeypatch):
    import config
    import agent.protocol.agent as agent_module

    decision = ControlledDecision()
    agent = Agent("基础", model=ScriptedModel(), enable_skills=False)
    monkeypatch.setattr(config, "conf", lambda: {"agent_decision": {"enabled": True}})
    monkeypatch.setattr(DecisionRun, "start", lambda *a, **k: decision)
    monkeypatch.setattr(agent, "get_full_system_prompt", lambda **k: "基础")

    def fail_constructor(**kwargs):
        raise RuntimeError("构造失败")

    monkeypatch.setattr(agent_module, "AgentStreamExecutor", fail_constructor)
    with pytest.raises(RuntimeError, match="构造失败"):
        agent.run_stream("目标")
    assert decision.closed


def test_skill_hint_is_run_local_and_never_persisted_for_next_run(monkeypatch):
    import config

    hint = "SKILL.md 预读内容：当前运行唯一标记"
    runs = iter([ControlledDecision("tools", hint), ControlledDecision("direct")])
    model = ScriptedModel([tool_call()], [text("第一轮答复")], [text("第二轮答复")])
    agent = Agent("基础", model=model, tools=[LookupTool()], enable_skills=False)
    monkeypatch.setattr(config, "conf", lambda: {"agent_decision": {"enabled": True}})
    monkeypatch.setattr(DecisionRun, "start", lambda *a, **k: next(runs))
    monkeypatch.setattr(agent, "get_full_system_prompt", lambda **k: "基础")
    assert agent.run_stream("查询") == "第一轮答复"
    assert hint in model.requests[0].system
    assert hint not in history_text(agent.messages)
    assert agent.system_prompt == "基础"
    assert agent.run_stream("普通问题") == "第二轮答复"
    assert hint not in model.requests[-1].system
    assert hint not in history_text(agent.messages)


@pytest.mark.parametrize("failure", ["no_key", "timeout"])
def test_unavailable_decision_service_falls_back_to_main_model(monkeypatch, failure):
    import config

    settings = {"agent_decision": {
        "enabled": True, "request_timeout": .1, "route_wait_seconds": 0,
        "route_min_probability": .85, "skill_min_probability": .85,
        "completion_enabled": True, "completion_min_probability": .9,
        "completion_wait_seconds": 0, "max_skill_chars": 100,
    }}
    monkeypatch.setattr(config, "conf", lambda: settings)
    credential_checks = []
    if failure == "no_key":
        def missing_credentials(settings):
            credential_checks.append(settings)
            return None
        monkeypatch.setattr(runtime.JevClient, "from_config", missing_credentials)
    else:
        decision = ControlledDecision(ready=False)
        decision.resolve_error = TimeoutError("模拟决策请求超时")
        monkeypatch.setattr(DecisionRun, "start", lambda *a, **k: decision)
    model = ScriptedModel([text("主模型正常答复")])
    agent = Agent("基础", model=model, enable_skills=False)
    monkeypatch.setattr(agent, "get_full_system_prompt", lambda **k: "基础")
    events = []
    assert agent.run_stream("你好", on_event=events.append) == "主模型正常答复"
    assert len(model.requests) == 1
    assert streamed_text(events) == "主模型正常答复"
    assert not any(event["type"] in {"error", "agent_cancelled"} for event in events)
    if failure == "timeout":
        assert decision.closed
    else:
        assert credential_checks == [settings["agent_decision"]]
