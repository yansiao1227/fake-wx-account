"""Jev 与主 Agent 使用本轮已刷新的技能；不访问真实模型或决策服务。"""

import json

import pytest

import agent.protocol.agent as agent_module
from agent.decision import runtime
from agent.protocol.agent import Agent
from agent.skills.manager import SkillManager
from agent.tools.base_tool import BaseTool


class LocalTool(BaseTool):
    name = "lookup"
    description = "本地测试工具"
    params = {"type": "object", "properties": {}}


@pytest.mark.parametrize("change", ["content", "disabled", "deleted", "invocation_disabled"])
def test_decision_snapshot_refreshes_disk_skills_before_start(tmp_path, monkeypatch, change):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"

    def write_skill(content, extra=""):
        skill_path.write_text(
            "---\nname: demo\ndescription: 本地测试技能\n" + extra + "---\n" + content,
            encoding="utf-8",
        )

    write_skill("OLD_INSTRUCTION")
    manager = SkillManager(custom_dir=str(tmp_path))
    assert "OLD_INSTRUCTION" in manager.build_skill_snapshot().resolved_skills[0].content
    if change == "content":
        write_skill("NEW_INSTRUCTION")
    elif change == "disabled":
        config_path = tmp_path / "skills_config.json"
        saved = json.loads(config_path.read_text(encoding="utf-8"))
        saved["demo"]["enabled"] = False
        config_path.write_text(json.dumps(saved), encoding="utf-8")
    elif change == "deleted":
        skill_path.unlink()
    else:
        write_skill("NEW_INSTRUCTION", "disable-model-invocation: true\n")

    snapshots = []

    def start(cls, message, tools, skill_manager, recent, settings, **kwargs):
        snapshots.append(runtime._snapshot_skills(skill_manager, kwargs.get("skill_filter")))
        return None

    class LocalExecutor:
        def __init__(self, **kwargs):
            self.messages = list(kwargs["messages"])

        def run_stream(self, message):
            return "完成"

    import config

    monkeypatch.setattr(config, "conf", lambda: {"agent_decision": {"enabled": True}})
    monkeypatch.setattr(runtime.DecisionRun, "start", classmethod(start))
    monkeypatch.setattr(agent_module, "AgentStreamExecutor", LocalExecutor)
    instance = Agent("基础提示", model=object(), tools=[LocalTool()], skill_manager=manager)

    assert instance.run_stream("使用 demo 完成任务") == "完成"

    assert len(snapshots) == 1
    if change == "content":
        assert [skill.name for skill in snapshots[0]] == ["demo"]
        assert "NEW_INSTRUCTION" in snapshots[0][0].content
        assert "OLD_INSTRUCTION" not in snapshots[0][0].content
    else:
        assert snapshots[0] == ()


def test_skill_refresh_failure_skips_decision_and_keeps_model_fallback(monkeypatch):
    class BrokenManager:
        def refresh_skills(self):
            raise OSError("本地测试读取失败")

    class LocalExecutor:
        def __init__(self, **kwargs):
            assert kwargs["decision_run"] is None
            self.messages = list(kwargs["messages"])

        def run_stream(self, message):
            return "原流程完成"

    import config

    monkeypatch.setattr(config, "conf", lambda: {"agent_decision": {"enabled": True}})
    monkeypatch.setattr(runtime.DecisionRun, "start", classmethod(
        lambda cls, *args, **kwargs: pytest.fail("技能刷新失败后不能采用旧快照")
    ))
    monkeypatch.setattr(agent_module, "AgentStreamExecutor", LocalExecutor)
    instance = Agent("基础提示", model=object(), skill_manager=BrokenManager())

    assert instance.run_stream("执行任务") == "原流程完成"
