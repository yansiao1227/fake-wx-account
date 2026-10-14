"""合并转发正文的本轮工具约束只用合成模型和工具验证。"""

from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from agent.protocol.agent import Agent
from agent.protocol.agent_stream import AgentStreamExecutor, _completion_gate_prompt
from agent.tools.base_tool import ToolResult, ToolStage
from bridge.agent_bridge import AgentBridge
from bridge.context import Context, ContextType
from bridge.reply import ReplyType


DISABLED_TOOLS = ["wechat_history", "wechat_desktop", "bash"]
GATE_CONTEXT = "合并转发正文已由前置读取提供。按现有正文答复，并明确截断或媒体缺失限制。"
POLICY_REPLY = "这份转发记录的正文已读到，但这次没能完成分析。请告诉我要总结或查找哪部分内容。"


class _Tool:
    stage = ToolStage.PRE_PROCESS
    params = {"type": "object", "properties": {}}
    description = "合成工具"

    def __init__(self, name):
        self.name = name
        self.calls = []

    def get_json_schema(self):
        return {"parameters": self.params}

    def execute_tool(self, arguments):
        self.calls.append(arguments)
        return ToolResult.success("合成结果")


class _Model:
    model = "synthetic-model"

    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or ["合成答复"])

    def call_stream(self, request):
        self.requests.append(deepcopy(request))
        response = self.responses.pop(0)
        if isinstance(response, str):
            delta = {"content": response}
        else:
            calls = response if isinstance(response, list) else [response]
            delta = {"tool_calls": [{"index": index, "id": f"synthetic-tool-{index}", "function": {
                "name": call["name"], "arguments": "{}",
            }} for index, call in enumerate(calls)]}
        yield {"choices": [{"delta": delta, "finish_reason": "stop"}]}


@pytest.fixture
def isolated_tools(monkeypatch):
    import config
    from agent.tools import ToolManager

    monkeypatch.setattr(config, "conf", lambda: {})
    monkeypatch.setattr(ToolManager, "sync_mcp_into_agent", lambda *_args: ([], []))


def _executor(tools, **kwargs):
    return AgentStreamExecutor(
        agent=SimpleNamespace(memory_manager=None), model=kwargs.pop("model", _Model()),
        system_prompt="合成系统提示", tools=tools,
        messages=[{"role": "user", "content": "合成问题"}], **kwargs,
    )


def test_disabled_tools_are_removed_from_schema(isolated_tools):
    tools = [_Tool(name) for name in DISABLED_TOOLS + ["read"]]
    executor = _executor(tools, disabled_tools=DISABLED_TOOLS)

    executor._call_llm_stream()

    assert [tool["name"] for tool in executor.model.requests[0].tools] == ["read"]
    assert [tool.name for tool in tools] == DISABLED_TOOLS + ["read"]


@pytest.mark.parametrize("registered", [True, False])
def test_guessed_disabled_call_never_executes_or_suggests_retry(isolated_tools, registered):
    tool = _Tool("wechat_history")
    executor = _executor([tool] if registered else [], disabled_tools=DISABLED_TOOLS,
                         completion_gate_context=GATE_CONTEXT)

    result = executor._execute_tool({"name": tool.name, "id": "synthetic", "arguments": {}})

    assert result["status"] == "error"
    assert tool.calls == []
    assert "禁用" in result["result"]
    assert GATE_CONTEXT in result["result"]
    assert "尝试完全不同的方法" not in result["result"]


def test_mid_run_mcp_sync_cannot_restore_disabled_schema_or_execution(isolated_tools, monkeypatch):
    from agent.tools import ToolManager

    blocked = _Tool("wechat_history")
    executor = _executor([_Tool("read")], disabled_tools=DISABLED_TOOLS)

    def sync(_manager, target):
        target.tools[blocked.name] = blocked

    monkeypatch.setattr(ToolManager, "sync_mcp_into_agent", sync)
    executor._call_llm_stream()
    executor.model.responses.append("第二轮答复")
    executor._call_llm_stream()

    assert all([tool["name"] for tool in request.tools] == ["read"]
               for request in executor.model.requests)
    assert executor._execute_tool({"name": blocked.name, "id": "synthetic", "arguments": {}})["status"] == "error"
    assert blocked.calls == []


@pytest.mark.parametrize("selection", ["retrieved", "unavailable", "failed"])
def test_mcp_retrieval_and_full_fallback_exclude_disabled_tools(isolated_tools, monkeypatch, selection):
    import config
    from agent.tools import ToolManager
    from agent.tools.mcp.mcp_tool import McpTool
    import agent.tools.mcp.tool_retrieval as retrieval

    monkeypatch.setattr(config, "conf", lambda: {"mcp_tool_retrieval_enabled": True,
                                               "mcp_tool_retrieval_threshold": 1})
    monkeypatch.setattr(ToolManager, "get_mcp_tool_vectors", lambda _manager: {})
    monkeypatch.setattr(ToolManager, "embed_query", lambda _manager, _query: [1.0])

    def select(*_args):
        if selection == "failed":
            raise RuntimeError("synthetic embedding failure")
        if selection == "unavailable":
            return None
        return {"wechat_history", "mcp_two"}

    monkeypatch.setattr(retrieval, "select_mcp_tools", select)
    mcp_tools = [McpTool(None, {"name": name}, "synthetic")
                 for name in ["wechat_history", "mcp_one", "mcp_two"]]
    executor = _executor([_Tool("bash"), _Tool("read"), *mcp_tools], disabled_tools=DISABLED_TOOLS)

    selected = [tool.name for tool in executor._select_tools_for_injection()]

    assert set(selected).isdisjoint(DISABLED_TOOLS)
    assert selected == (["read", "mcp_two"] if selection == "retrieved"
                        else ["read", "mcp_one", "mcp_two"])


def test_completion_gate_accepts_preloaded_record_body(isolated_tools):
    model = _Model([{"name": "read"}, "合成最终答复"])
    executor = _executor([_Tool("read")], model=model, disabled_tools=DISABLED_TOOLS,
                         completion_gate_context=GATE_CONTEXT)
    executor.agent = Agent("合成系统提示", model=model, enable_skills=False)

    assert executor.run_stream("合并转发正文") == "合成最终答复"
    assert GATE_CONTEXT in str(model.requests[1].messages)
    assert _completion_gate_prompt() not in str(model.requests[1].messages)


@pytest.mark.parametrize("max_turns", [1, 50])
def test_forbidden_call_batch_stops_once_preserving_all_pairs(isolated_tools, max_turns):
    tools = [_Tool("read"), _Tool("wechat_history"), _Tool("bash")]
    guesses = [{"name": "read"}, {"name": "wechat_history"}, {"name": "bash"}, {"name": "read"}]
    model = _Model([guesses, guesses, guesses])
    events = []
    executor = _executor(tools, model=model, disabled_tools=DISABLED_TOOLS,
                         completion_gate_context=GATE_CONTEXT, disabled_tool_reply=POLICY_REPLY,
                         on_event=events.append, max_turns=max_turns)
    executor.agent = Agent("合成系统提示", model=model, enable_skills=False)

    assert executor.run_stream("合成正文和问题") == POLICY_REPLY
    assert len(model.requests) == 1
    assert all(not tool.calls for tool in tools)
    uses = [block["id"] for message in executor.messages for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_use"]
    results = [block["tool_use_id"] for message in executor.messages for block in message.get("content", [])
               if isinstance(block, dict) and block.get("type") == "tool_result"]
    assert uses == results
    assert len(results) == len(guesses)
    assert executor.messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": POLICY_REPLY}]}
    assert events[-1]["type"] == "agent_end"
    assert events[-1]["data"]["final_response"] == POLICY_REPLY


@pytest.mark.parametrize("max_turns", [1, 50])
def test_jev_policy_rejection_filters_route_and_skips_completion(isolated_tools, monkeypatch, max_turns):
    import config
    from unittest.mock import Mock
    from agent.decision.runtime import CompletionDecision, DecisionRun, RoutingDecision

    decision = SimpleNamespace(
        version="synthetic-jev", route_wait_seconds=0, completion_wait_seconds=0,
        completion_enabled=True,
        resolve=Mock(return_value=RoutingDecision("synthetic-jev", "direct", (), (), .98)),
        start_completion=Mock(return_value=True),
        resolve_completion=Mock(return_value=CompletionDecision("continue", .98)),
        close=Mock(),
    )
    start = Mock(return_value=decision)
    monkeypatch.setattr(config, "conf", lambda: {"agent_decision": {"enabled": True}})
    monkeypatch.setattr(DecisionRun, "start", start)
    tools = [_Tool(name) for name in ["read", *DISABLED_TOOLS]]
    guesses = [{"name": "read"}, {"name": "wechat_history"}, {"name": "bash"}]
    model = _Model([guesses])
    agent = Agent("合成系统提示", model=model, tools=tools, enable_skills=False, max_steps=max_turns)
    monkeypatch.setattr(agent, "get_full_system_prompt", lambda **_kwargs: "合成系统提示")
    events = []

    assert agent.run_stream("合成转发正文和问题", on_event=events.append,
                            disabled_tools=DISABLED_TOOLS, completion_gate_context=GATE_CONTEXT,
                            disabled_tool_reply=POLICY_REPLY) == POLICY_REPLY

    start.assert_called_once()
    assert [tool.name for tool in start.call_args.args[1]] == ["read"]
    assert any(event["type"] == "decision_route" for event in events)
    decision.start_completion.assert_not_called()
    decision.resolve_completion.assert_not_called()
    assert decision.close.called
    assert len(model.requests) == 1
    assert all(not tool.calls for tool in tools)
    blocks = [block for message in agent.messages for block in message.get("content", [])
              if isinstance(block, dict)]
    uses = [block["id"] for block in blocks if block.get("type") == "tool_use"]
    results = [block for block in blocks if block.get("type") == "tool_result"]
    assert uses == [block["tool_use_id"] for block in results]
    assert len(results) == len(guesses)
    assert all(block["is_error"] for block in results)
    assert agent.messages[-1] == {"role": "assistant", "content": [{"type": "text", "text": POLICY_REPLY}]}
    assert events[-1]["data"]["final_response"] == POLICY_REPLY


def test_agent_reuses_full_tools_for_next_ordinary_request(isolated_tools, monkeypatch):
    tools = [_Tool(name) for name in DISABLED_TOOLS + ["read"]]
    model = _Model(["记录答复", "普通答复"])
    agent = Agent("合成系统提示", model=model, tools=tools, enable_skills=False)
    monkeypatch.setattr(agent, "get_full_system_prompt", lambda **_kwargs: "合成系统提示")

    agent.run_stream("记录问题", disabled_tools=DISABLED_TOOLS, completion_gate_context=GATE_CONTEXT)
    record_executor = agent.stream_executor
    agent.run_stream("普通问题")

    assert [tool["name"] for tool in model.requests[0].tools] == ["read"]
    assert [tool["name"] for tool in model.requests[1].tools] == DISABLED_TOOLS + ["read"]
    assert record_executor.disabled_tools == frozenset(DISABLED_TOOLS)
    assert not agent.stream_executor.disabled_tools
    assert GATE_CONTEXT in model.requests[0].system
    assert GATE_CONTEXT not in model.requests[1].system
    assert [tool.name for tool in agent.tools] == DISABLED_TOOLS + ["read"]
    assert not hasattr(agent, "disabled_tools")


def test_policy_stop_does_not_affect_next_ordinary_agent_call(isolated_tools, monkeypatch):
    blocked = _Tool("wechat_history")
    model = _Model([{"name": blocked.name}, {"name": blocked.name}, "普通请求最终答复"])
    agent = Agent("合成系统提示", model=model, tools=[blocked], enable_skills=False)
    monkeypatch.setattr(agent, "get_full_system_prompt", lambda **_kwargs: "合成系统提示")

    assert agent.run_stream("转发记录问题", disabled_tools=DISABLED_TOOLS,
                            completion_gate_context=GATE_CONTEXT, disabled_tool_reply=POLICY_REPLY) == POLICY_REPLY
    assert blocked.calls == []
    assert agent.run_stream("普通会话查询") == "普通请求最终答复"
    assert blocked.calls == [{}]
    assert len(model.requests) == 3


def test_system_tool_directory_filters_request_local_disabled_tools(isolated_tools, monkeypatch):
    import agent.prompt

    captured = []

    class _Builder:
        def __init__(self, **_kwargs):
            pass

        def build(self, **kwargs):
            captured.append([tool.name for tool in kwargs["tools"]])
            return "合成工具目录"

    monkeypatch.setattr(agent.prompt, "PromptBuilder", _Builder)
    agent = Agent("缓存系统提示", model=_Model(), tools=[_Tool(name) for name in DISABLED_TOOLS + ["read"]],
                  enable_skills=False)

    agent.get_full_system_prompt(disabled_tools=DISABLED_TOOLS)
    agent.get_full_system_prompt()

    assert captured == [["read"], DISABLED_TOOLS + ["read"]]


def _bridge(monkeypatch):
    class _Agent:
        def __init__(self):
            self.messages = []
            self.messages_lock = threading.Lock()
            self.tools = [_Tool(name) for name in DISABLED_TOOLS]
            self.model = SimpleNamespace()
            self.calls = []
            self.stream_executor = SimpleNamespace(files_to_send=[])

        def run_stream(self, **kwargs):
            self.calls.append(kwargs)
            self.messages.append({"role": "user", "content": kwargs["user_message"]})
            self._last_run_new_messages = []
            return "合成答复"

    import agent.evolution.trigger as trigger
    monkeypatch.setattr(trigger, "mark_run_active", lambda *_args: None)
    monkeypatch.setattr(trigger, "note_user_turn", lambda *_args, **_kwargs: None)
    agent = _Agent()
    bridge = AgentBridge.__new__(AgentBridge)
    bridge.get_agent = lambda session_id=None: agent
    bridge._pre_persist_user_message = lambda *_args: False
    bridge._persist_messages = lambda *_args: None
    bridge._schedule_mcp_hot_reload = lambda *_args: None
    return bridge, agent


@pytest.mark.parametrize("overrides,restricted", [
    ({}, True),
    ({"wechat_desktop_chat_record": False}, False),
    ({"wechat_desktop_auto_reply": False}, False),
    ({"channel_type": "web"}, False),
    ({"is_scheduled_task": True}, False),
])
def test_bridge_only_passes_policy_for_auto_reply_chat_record(monkeypatch, overrides, restricted):
    bridge, agent = _bridge(monkeypatch)
    original_tools = agent.tools
    values = {"channel_type": "wechat_desktop", "wechat_desktop_auto_reply": True,
              "wechat_desktop_chat_record": True, "wechat_desktop_disabled_tools": DISABLED_TOOLS,
              "wechat_desktop_disabled_tool_reply": POLICY_REPLY,
              "wechat_desktop_is_reference": True, "wechat_desktop_user_message": "请总结"}
    values.update(overrides)

    reply = bridge.agent_reply("合成前置正文", Context(ContextType.TEXT, "", values))

    assert reply.type == ReplyType.TEXT
    passed = agent.calls[0]
    if restricted:
        assert set(passed["disabled_tools"]) == set(DISABLED_TOOLS)
        assert passed["disabled_tool_reply"] == POLICY_REPLY
        gate = passed["completion_gate_context"]
        assert "前置" in gate and "正文" in gate
        assert "截断" in gate and "媒体" in gate
        assert "其他会话" in gate and "原始库" in gate
    else:
        assert "disabled_tools" not in passed
        assert "completion_gate_context" not in passed
        assert "disabled_tool_reply" not in passed
    assert agent.tools is original_tools


def test_tool_not_found_help_does_not_advertise_disabled_tools(isolated_tools):
    executor = _executor([_Tool("bash"), _Tool("read")], disabled_tools=DISABLED_TOOLS)

    message = executor._build_tool_not_found_message("synthetic_missing_tool")

    assert "bash" not in message
    assert "read" in message
