from types import SimpleNamespace

from agent.prompt.builder import _build_tooling_section
from agent.protocol.agent_stream import _completion_gate_prompt


def test_tooling_prompt_requires_evidence_based_completion():
    prompt = "\n".join(
        _build_tooling_section([SimpleNamespace(name="bash")], "zh")
    )

    assert "工具执行成功不等于任务完成" in prompt
    assert "空结果、歧义结果、部分结果或超出指定范围" in prompt


def test_runtime_completion_gate_is_domain_agnostic():
    prompt = _completion_gate_prompt()

    assert "禁止猜测" in prompt
    assert "中间结果" in prompt
    assert "用户目标已经完成" in prompt
    assert "地点" not in prompt
    assert "路线" not in prompt
    assert "POI" not in prompt
