"""并行决策的隔离、超时和保守判断；不访问真实 Jev 接口。"""

import copy
import json
import threading
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from agent.decision import runtime
from agent.decision.runtime import DecisionRun


@pytest.fixture(autouse=True)
def isolated_slots(monkeypatch):
    monkeypatch.setattr(runtime, "_REQUEST_SLOTS", threading.BoundedSemaphore(4))


@pytest.fixture
def settings():
    return {"agent_decision": {
        "enabled": True, "request_timeout": 1.0, "route_wait_seconds": 0.1,
        "route_min_probability": 0.8, "skill_min_probability": 0.85,
        "completion_enabled": True, "completion_min_probability": 0.85,
        "completion_wait_seconds": 0.1, "max_skill_chars": 100,
    }}


def choice(selected, probability=0.95):
    return {"choice": selected, "probabilities": {selected: probability}, "confidence": 0.95}


def route_answers(route="tools", tool="tool_0", route_probability=0.95, tool_probability=0.95,
                  skills=(), tools=None):
    answers = {"route": choice(route, route_probability), "primary_tool": choice(tool, tool_probability)}
    if tools is None:
        tools = (tool_probability if tool == "tool_0" else 0.01,)
    answers.update({"tool_" + str(index): {"noul": probability} for index, probability in enumerate(tools)})
    answers.update({"skill_" + str(index): {"noul": probability} for index, probability in enumerate(skills)})
    return answers


class FakeClient:
    def __init__(self, answers=None, gate=None):
        self.answers = answers if answers is not None else route_answers()
        self.gate = gate
        self.entered = threading.Event()
        self.calls = []

    def evaluate(self, state, questions, timeout=None):
        self.calls.append((json.loads(state), copy.deepcopy(questions), timeout, threading.current_thread().daemon))
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(2)
        if isinstance(self.answers, Exception):
            raise self.answers
        if "missing_steps" in questions:
            return {"missing_steps": {"noul": 0.02}, "insufficient_evidence": {"noul": 0.03}}
        return self.answers


class FakeSkillManager:
    def __init__(self, skills, disabled=()):
        self.skills = skills
        self.disabled = set(disabled)
        self.filters = []

    def build_skill_snapshot(self, skill_filter=None):
        self.filters.append(skill_filter)
        allowed = [skill for skill in self.skills if skill.name not in self.disabled
                   and (skill_filter is None or skill.name in skill_filter)]
        return SimpleNamespace(resolved_skills=allowed)


def skill(name, content="技能内容", disable_model_invocation=False):
    return SimpleNamespace(name=name, description="技能用途 " + name,
                           file_path="D:/skills/" + name + "/SKILL.md",
                           base_dir="D:/skills/" + name, content=content,
                           disable_model_invocation=disable_model_invocation)


def start(settings, client=None, manager=None, recent=None, skill_filter=None):
    return DecisionRun.start("请搜索今天的消息", [SimpleNamespace(name="web_search", description="搜索最新资料")],
                             manager, recent or [], settings, client=client or FakeClient(), skill_filter=skill_filter)


def test_start_is_nonblocking_daemon_and_result_is_immutable(settings):
    gate = threading.Event()
    client = FakeClient(gate=gate)
    before = time.monotonic()
    run = start(settings, client)
    assert time.monotonic() - before < 0.2
    assert client.entered.wait(0.5)
    assert run.poll() is None
    assert client.calls[0][3] is True
    gate.set()
    result = run.resolve(wait_seconds=0.5)
    assert result.route == "tools"
    assert result.tool_names == ("web_search",)
    assert "web_search" in result.routing_hint
    with pytest.raises(FrozenInstanceError):
        result.route = "direct"
    run.close()


def test_snapshots_do_not_follow_mutated_history_tools_or_skills(settings):
    gate = threading.Event()
    client = FakeClient(route_answers(skills=(0.96,)), gate=gate)
    current_skill = skill("search", "原始技能")
    tool = SimpleNamespace(name="web_search", description="原始说明")
    recent = [{"role": "user", "content": "原始历史"}]
    run = DecisionRun.start("原始请求", [tool], FakeSkillManager([current_skill]), recent, settings, client=client)
    tool.description = "已修改"
    current_skill.content = "已修改"
    recent[0]["content"] = "已修改"
    assert client.entered.wait(0.5)
    gate.set()
    result = run.resolve(wait_seconds=0.5)
    assert result.preloaded_skills[0].content == "原始技能"
    assert client.calls[0][0]["recent_messages"][0]["text"] == "原始历史"
    assert client.calls[0][1]["primary_tool"]["criteria"]["tool_0"] == "web_search: 原始说明"
    assert run.poll(version="另一个请求") is None


def test_only_enabled_filtered_invocable_skills_are_considered(settings):
    manager = FakeSkillManager([skill("selected"), skill("disabled"), skill("manual", disable_model_invocation=True),
                                skill("filtered_out")], disabled=("disabled",))
    client = FakeClient(route_answers(skills=(0.96,)))
    run = start(settings, client, manager, skill_filter=["selected", "disabled", "manual"])
    result = run.resolve(wait_seconds=0.5)
    assert manager.filters == [["selected", "disabled", "manual"]]
    assert result.skill_names == ("selected",)
    questions = client.calls[0][1]
    assert set(questions) == {"route", "primary_tool", "tool_0", "skill_0"}
    assert "disabled" not in json.dumps(questions)
    assert "manual" not in json.dumps(questions)
    preview = result.preloaded_skills[0]
    assert preview.file_path.endswith("/selected/SKILL.md")
    assert preview.base_dir.endswith("/selected")
    assert preview.truncated is False
    assert "无需重复读取" in result.routing_hint
    assert len(client.calls) == 1


def test_preloaded_content_has_total_character_budget(settings):
    settings["agent_decision"]["max_skill_chars"] = 7
    manager = FakeSkillManager([skill("first", "甲" * 5), skill("second", "乙" * 5)])
    result = start(settings, FakeClient(route_answers(skills=(0.99, 0.98))), manager).resolve(wait_seconds=0.5)
    assert result.skill_names == ("first", "second")
    assert sum(len(item.content) for item in result.preloaded_skills) == 7
    assert result.preloaded_skills[1].content == "乙乙"
    assert result.preloaded_skills[0].truncated is False
    assert result.preloaded_skills[1].truncated is True
    assert "读取完整文件" in result.routing_hint


def test_all_required_tools_and_skills_are_selected_in_one_request(settings):
    tools = [SimpleNamespace(name="lookup", description="查询资料"),
             SimpleNamespace(name="save_file", description="保存成果")]
    manager = FakeSkillManager([skill("research"), skill("report")])
    client = FakeClient(route_answers(tool="tool_1", tools=(0.99, 0.96), skills=(0.97, 0.98)))
    run = DecisionRun.start("先查询资料，再生成报告并保存", tools, manager, [], settings, client=client)
    try:
        result = run.resolve(wait_seconds=0.5)
        assert result.tool_names == ("save_file", "lookup")
        assert result.skill_names == ("research", "report")
        assert len(result.preloaded_skills) == 2
        assert len(client.calls) == 1
        questions = client.calls[0][1]
        assert set(questions) == {"route", "primary_tool", "tool_0", "tool_1", "skill_0", "skill_1"}
        assert questions["tool_0"]["type"] == "noul"
        assert "lookup" in questions["tool_0"]["instructions"]
        assert "查询资料" in questions["tool_0"]["instructions"]
        assert "后续必需步骤" in questions["tool_1"]["instructions"]
        assert "互相替代" in questions["tool_0"]["instructions"]
        assert "技能依赖的执行工具" in questions["tool_0"]["instructions"]
        assert "建议工具计划" in result.routing_hint
        assert "建议首先考虑工具" not in result.routing_hint
    finally:
        run.close()


def test_tool_probability_filter_is_independent_of_primary_order(settings):
    tools = [SimpleNamespace(name="lookup", description="查询资料"),
             SimpleNamespace(name="save_file", description="保存成果"),
             SimpleNamespace(name="optional", description="可选处理")]
    manager = FakeSkillManager([skill("report"), skill("optional_skill")])
    client = FakeClient(route_answers(tool="tool_2", tool_probability=0.7,
                                     tools=(0.99, 0.8, 0.79), skills=(0.85, 0.84)))
    run = DecisionRun.start("查询后保存", tools, manager, [], settings, client=client)
    try:
        result = run.resolve(wait_seconds=0.5)
        assert result.tool_names == ("lookup", "save_file")
        assert result.skill_names == ("report",)
        assert "optional" not in result.routing_hint
        assert len(client.calls) == 1
    finally:
        run.close()


def test_high_probability_primary_is_first_and_not_duplicated(settings):
    client = FakeClient(route_answers(tools=(0.99,)))
    run = start(settings, client)
    try:
        assert run.resolve(wait_seconds=0.5).tool_names == ("web_search",)
        assert len(client.calls) == 1
    finally:
        run.close()


def test_primary_does_not_override_individual_tool_necessity(settings):
    tools = [SimpleNamespace(name="optional_primary", description="可选入口"),
             SimpleNamespace(name="required", description="必须执行的工具")]
    client = FakeClient(route_answers(tool="tool_0", tool_probability=0.99, tools=(0.01, 0.98)))
    run = DecisionRun.start("执行当前任务", tools, None, [], settings, client=client)
    try:
        result = run.resolve(wait_seconds=0.5)
        assert result.tool_names == ("required",)
        assert "optional_primary" not in result.routing_hint
        assert len(client.calls) == 1
    finally:
        run.close()


def test_low_probability_primary_keeps_selected_tools_in_catalog_order(settings):
    tools = [SimpleNamespace(name="lookup", description="查询资料"),
             SimpleNamespace(name="save_file", description="保存成果")]
    client = FakeClient(route_answers(tool="tool_1", tool_probability=0.7, tools=(0.99, 0.98)))
    run = DecisionRun.start("查询后保存", tools, None, [], settings, client=client)
    try:
        assert run.resolve(wait_seconds=0.5).tool_names == ("lookup", "save_file")
        assert len(client.calls) == 1
    finally:
        run.close()


@pytest.mark.parametrize("answer", [None, {}, {"noul": float("nan")}, {"noul": True},
                                    {"noul": 1.1}, {"noul": -0.1}, "yes"])
def test_missing_or_invalid_tool_answers_fail_open(settings, answer):
    answers = route_answers()
    if answer is None:
        answers.pop("tool_0")
    else:
        answers["tool_0"] = answer
    run = start(settings, FakeClient(answers))
    try:
        result = run.resolve(wait_seconds=0.5)
        assert result.route == "uncertain"
        assert result.tool_names == ()
        assert result.skill_names == ()
        assert result.routing_hint == ""
    finally:
        run.close()


@pytest.mark.parametrize("answers", [
    route_answers(route="direct"),
    route_answers(route="uncertain"),
    route_answers(route_probability=0.6),
    route_answers(tool_probability=0.6),
    route_answers(tool="none"),
    route_answers(tool="uncertain"),
    {"route": choice("不存在的选项"), "primary_tool": choice("tool_0")},
    {"route": choice("tools"), "primary_tool": choice("不在候选集")},
    {"route": choice("tools")},
    RuntimeError("模拟接口错误"),
])
def test_non_qualifying_results_never_inject_hint(settings, answers):
    result = start(settings, FakeClient(answers)).resolve(wait_seconds=0.5)
    assert result.routing_hint == ""
    assert result.tool_names == ()
    assert result.skill_names == ()


def test_no_skills_and_no_tools_is_valid(settings):
    client = FakeClient(route_answers(route="direct", tool="none", tools=()))
    run = DecisionRun.start("你好", [], None, [], settings, client=client)
    result = run.resolve(wait_seconds=0.5)
    assert result.route == "direct"
    assert set(client.calls[0][1]) == {"route", "primary_tool"}
    assert set(client.calls[0][1]["primary_tool"]["criteria"]) == {"none", "uncertain"}


def test_skill_candidates_can_work_without_builtin_tool_candidate(settings):
    client = FakeClient(route_answers(tool="none", tools=(), skills=(0.9,)))
    run = DecisionRun.start("生成图片", [], FakeSkillManager([skill("image-generation")]), [], settings, client=client)
    result = run.resolve(wait_seconds=0.5)
    assert result.tool_names == ()
    assert result.skill_names == ("image-generation",)
    assert result.routing_hint
    assert set(client.calls[0][1]) == {"route", "primary_tool", "skill_0"}
    run.close()


def test_low_or_malformed_skill_results_fail_open(settings):
    manager = FakeSkillManager([skill("selected")])
    result = start(settings, FakeClient(route_answers(tool="none", skills=(0.7,))), manager).resolve(wait_seconds=0.5)
    assert result.routing_hint == ""
    broken = route_answers(skills=(0.99,))
    broken["skill_0"] = {"noul": float("nan")}
    result = start(settings, FakeClient(broken), manager).resolve(wait_seconds=0.5)
    assert result.route == "uncertain"
    assert result.routing_hint == ""


def test_timeout_remains_uncertain_even_after_late_result(settings):
    settings["agent_decision"]["request_timeout"] = 0.04
    gate = threading.Event()
    client = FakeClient(gate=gate)
    run = start(settings, client)
    before = time.monotonic()
    result = run.resolve(wait_seconds=1)
    assert time.monotonic() - before < 0.2
    assert result.route == "uncertain"
    gate.set()
    assert run._route_job.done.wait(0.5)
    assert run.poll().route == "uncertain"


def test_timely_result_is_still_valid_when_consumed_later(settings):
    settings["agent_decision"]["request_timeout"] = 0.04
    run = start(settings)
    assert run._route_job.done.wait(0.5)
    time.sleep(0.05)
    assert run.poll().route == "tools"


@pytest.mark.parametrize("cancel", ["close", "invalidate"])
def test_cancel_invalidates_result_and_wakes_waiter(settings, cancel):
    gate = threading.Event()
    run = start(settings, FakeClient(gate=gate))
    results = []
    waiter = threading.Thread(target=lambda: results.append(run.resolve(wait_seconds=1)))
    waiter.start()
    getattr(run, cancel)()
    waiter.join(0.3)
    assert not waiter.is_alive()
    assert results == [None]
    gate.set()
    assert run.poll() is None
    assert not run.start_completion("目标", [{"result": "证据"}])


def test_capacity_is_bounded_and_does_not_queue(settings, monkeypatch):
    monkeypatch.setattr(runtime, "_REQUEST_SLOTS", threading.BoundedSemaphore(1))
    gate = threading.Event()
    first = start(settings, FakeClient(gate=gate))
    before = time.monotonic()
    assert start(settings) is None
    assert time.monotonic() - before < 0.1
    gate.set()
    assert first._route_job.done.wait(0.5)
    assert start(settings).resolve(wait_seconds=0.5).route == "tools"


def test_disabled_invalid_settings_or_missing_client_do_not_launch(settings, monkeypatch):
    monkeypatch.setattr(runtime.JevClient, "from_config", lambda config: None)
    assert DecisionRun.start("你好", [], None, [], settings) is None
    settings["agent_decision"]["enabled"] = False
    assert start(settings) is None
    settings["agent_decision"]["enabled"] = True
    settings["agent_decision"]["route_min_probability"] = 0.4
    assert start(settings) is None


class CompletionClient(FakeClient):
    def __init__(self, missing, insufficient):
        super().__init__()
        self.missing, self.insufficient = missing, insufficient

    def evaluate(self, state, questions, timeout=None):
        if "missing_steps" not in questions:
            return super().evaluate(state, questions, timeout)
        self.calls.append((json.loads(state), copy.deepcopy(questions), timeout, threading.current_thread().daemon))
        return {"missing_steps": {"noul": self.missing}, "insufficient_evidence": {"noul": self.insufficient}}


@pytest.mark.parametrize("missing,insufficient,action", [
    (0.01, 0.02, "finalize"), (0.96, 0.1, "continue"), (0.1, 0.98, "continue"),
    (0.6, 0.6, "uncertain"), (0.02, 0.3, "uncertain"),
])
def test_completion_requires_closed_evidence_and_both_checks(settings, missing, insufficient, action):
    client = CompletionClient(missing, insufficient)
    run = start(settings, client)
    result = run.evaluate_completion("查到当前价格", [{"tool": "search", "result": "99元"}], "草稿")
    assert result.action == action
    state, questions, timeout, daemon = client.calls[-1]
    assert state == {"user_goal": "查到当前价格", "latest_tool_evidence": [{"tool": "search", "result": "99元"}], "draft": "草稿"}
    assert set(questions) == {"missing_steps", "insufficient_evidence"}
    assert daemon is True


def test_completion_without_tools_or_with_bad_answer_fails_open(settings):
    client = CompletionClient(float("nan"), 0.01)
    run = start(settings, client)
    assert not run.start_completion("普通聊天", [])
    assert run.resolve_completion() is None
    assert run.evaluate_completion("目标", [{"result": "结果"}]) is None
    assert len(client.calls) == 2


def test_oversized_completion_input_invalidates_old_verdict_without_truncation(settings):
    client = CompletionClient(0.99, 0.99)
    run = start(settings, client)
    assert run.evaluate_completion("小目标", [{"result": "证据"}]).action == "continue"
    calls_before = len(client.calls)
    assert not run.start_completion("完整目标", [{"result": "长" * 20000}])
    assert run.resolve_completion() is None
    assert len(client.calls) == calls_before
    run.close()


def test_new_completion_version_invalidates_older_inflight_result(settings):
    old_gate = threading.Event()
    old_entered = threading.Event()

    class VersionedClient(FakeClient):
        def evaluate(self, state, questions, timeout=None):
            if "missing_steps" not in questions:
                return super().evaluate(state, questions, timeout)
            evidence = json.loads(state)["latest_tool_evidence"]
            if evidence == "旧证据":
                old_entered.set()
                old_gate.wait(1)
                return {"missing_steps": {"noul": 0.99}, "insufficient_evidence": {"noul": 0.99}}
            return {"missing_steps": {"noul": 0.01}, "insufficient_evidence": {"noul": 0.02}}

    run = start(settings, VersionedClient())
    assert run.start_completion("目标", "旧证据")
    assert old_entered.wait(0.5)
    old_job = run._completion_job
    assert run.start_completion("目标", "新证据")
    assert run.resolve_completion(wait_seconds=0.5).action == "finalize"
    old_gate.set()
    deadline = time.monotonic() + 0.5
    while old_job.finished_at is None and time.monotonic() < deadline:
        time.sleep(0.001)
    assert old_job.finished_at is not None
    assert old_job.result is None
    assert run.resolve_completion(wait_seconds=0.5).action == "finalize"
    assert not run.start_completion("普通聊天", [])
    assert run.resolve_completion() is None


def test_cancel_invalidates_and_wakes_completion_waiter(settings):
    gate = threading.Event()
    entered = threading.Event()

    class BlockingCompletionClient(FakeClient):
        def evaluate(self, state, questions, timeout=None):
            if "missing_steps" in questions:
                entered.set()
                gate.wait(1)
                return {"missing_steps": {"noul": 0.99}, "insufficient_evidence": {"noul": 0.99}}
            return super().evaluate(state, questions, timeout)

    run = start(settings, BlockingCompletionClient())
    assert run.start_completion("目标", "证据")
    assert entered.wait(0.5)
    results = []
    waiter = threading.Thread(target=lambda: results.append(run.resolve_completion(wait_seconds=1)))
    waiter.start()
    run.close()
    waiter.join(0.3)
    assert not waiter.is_alive()
    assert results == [None]
    gate.set()


def test_monotonic_deadline_limits_wait(settings):
    gate = threading.Event()
    run = start(settings, FakeClient(gate=gate))
    before = time.monotonic()
    assert run.resolve(deadline=before + 0.02) is None
    assert 0.01 <= time.monotonic() - before < 0.15
    run.close()
    gate.set()
