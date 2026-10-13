"""Jev 日志的可观测性、隐私边界和普通日志隔离；不访问真实服务。"""

import logging
import os
import re
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from agent.decision import runtime
from agent.decision.jev import JevError
from agent.decision.runtime import DecisionRun
from common import log as common_log


PRIVATE_STATE = "PRIVATE_USER_MESSAGE_禁止记入日志"
PRIVATE_KEY = "PRIVATE_NEWAPI_KEY_禁止记入日志"
PRIVATE_SKILL = "PRIVATE_SKILL_CONTENT_禁止记入日志"


class CapturedLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    @property
    def messages(self):
        return [record.getMessage() for record in list(self.records)]

    @property
    def text(self):
        return "\n".join(self.messages)

    def wait_for(self, *parts, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [message for message in self.messages if all(part in message for part in parts)]
            if found:
                return found[-1]
            time.sleep(0.005)
        pytest.fail("未找到预期的 Jev 日志字段：" + repr(parts) + "；实际：" + self.text)


@pytest.fixture
def logs(monkeypatch):
    monkeypatch.setattr(runtime, "_REQUEST_SLOTS", threading.BoundedSemaphore(4))
    capture = CapturedLogs()
    common_log.jev_logger.addHandler(capture)
    try:
        yield capture
    finally:
        common_log.jev_logger.removeHandler(capture)
        capture.close()


@pytest.fixture
def settings():
    return {"agent_decision": {
        "enabled": True, "request_timeout": 1.0, "route_wait_seconds": 0.1,
        "route_min_probability": 0.85, "skill_min_probability": 0.85,
        "completion_enabled": True, "completion_min_probability": 0.95,
        "completion_wait_seconds": 0.1, "max_skill_chars": 1000,
    }}


def choice(selected):
    return {"choice": selected, "probabilities": {selected: 0.98}}


class MockClient:
    def __init__(self, error=None, gate=None):
        self.error = error
        self.gate = gate
        self.entered = threading.Event()

    def evaluate(self, state, questions, timeout=None):
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(2), "测试没有释放模拟请求"
        if self.error is not None:
            raise self.error
        if "missing_steps" in questions:
            return {"missing_steps": {"noul": 0.01}, "insufficient_evidence": {"noul": 0.02}}
        answers = {"route": choice("tools"), "primary_tool": choice("tool_0")}
        answers.update({key: {"noul": 0.98} for key in questions
                        if key.startswith(("tool_", "skill_"))})
        return answers


def start(settings, client):
    selected_skill = SimpleNamespace(
        name="example_skill", description="测试技能", file_path="D:/skills/example/SKILL.md",
        base_dir="D:/skills/example", content=PRIVATE_SKILL, disable_model_invocation=False,
    )
    manager = SimpleNamespace(build_skill_snapshot=lambda skill_filter=None: SimpleNamespace(
        resolved_skills=[selected_skill],
    ))
    return DecisionRun.start(
        PRIVATE_STATE + PRIVATE_KEY,
        [SimpleNamespace(name="lookup", description="查询证据")], manager, [], settings, client=client,
    )


def assert_safe(logs):
    assert PRIVATE_STATE not in logs.text
    assert PRIVATE_KEY not in logs.text
    assert PRIVATE_SKILL not in logs.text
    assert all(record.exc_info is None for record in logs.records)


def test_success_logs_request_stage_duration_and_safe_summary(settings, logs):
    run = start(settings, MockClient())
    try:
        result = run.resolve(wait_seconds=0.5)
        assert result.route == "tools"
        done = logs.wait_for("stage=route", "status=done", "route=tools")
        assert re.search(r"\[Jev\] run=[0-9a-f]{8}(?:\s|$)", done)
        assert re.search(r"elapsed_ms=\d+(?:\.\d+)?", done)
        assert "tool_count=1" in done
        assert "skill_count=1" in done
        assert any("stage=route" in line and "status=start" in line for line in logs.messages)
        assert_safe(logs)
    finally:
        run.close()


def test_completion_logs_are_distinct_from_route_and_share_run_id(settings, logs):
    run = start(settings, MockClient())
    try:
        assert run.resolve(wait_seconds=0.5).route == "tools"
        assert run.start_completion(PRIVATE_STATE, [{"content": PRIVATE_KEY}])
        assert run.resolve_completion(wait_seconds=0.5).action == "finalize"
        route = logs.wait_for("stage=route", "status=done")
        completion = logs.wait_for("stage=completion", "status=done", "action=finalize")
        assert re.search(r"run=([0-9a-f]{8})", route).group(1) == re.search(
            r"run=([0-9a-f]{8})", completion,
        ).group(1)
        assert "elapsed_ms=" in completion
        assert_safe(logs)
    finally:
        run.close()


@pytest.mark.parametrize("error, safe_detail", [
    (JevError("timeout"), "timeout"),
    (JevError("http", 503), "503"),
    (ValueError(PRIVATE_KEY + PRIVATE_STATE), "ValueError"),
])
def test_failure_reports_only_safe_category_or_exception_type(settings, logs, error, safe_detail):
    run = start(settings, MockClient(error=error))
    try:
        assert run.resolve(wait_seconds=0.5).route == "uncertain"
        fallback = logs.wait_for("stage=route", "status=fallback")
        assert safe_detail in fallback
        assert "elapsed_ms=" in fallback
        assert not any("status=done" in line for line in logs.messages)
        assert_safe(logs)
    finally:
        run.close()


def test_missing_credentials_records_fallback_without_starting_request(settings, logs, monkeypatch):
    monkeypatch.setattr(runtime.JevClient, "from_config", lambda supplied: None)
    assert DecisionRun.start(PRIVATE_STATE, [], None, [], settings) is None
    fallback = logs.wait_for("stage=route", "status=fallback")
    assert "credentials" in fallback
    assert not any("status=start" in line for line in logs.messages)
    assert_safe(logs)


def test_invalid_configuration_records_fallback(settings, logs):
    settings["agent_decision"]["request_timeout"] = -1
    assert start(settings, MockClient()) is None
    assert "invalid_config" in logs.wait_for("stage=route", "status=fallback")
    assert_safe(logs)


def test_disabled_decision_does_not_emit_jev_logs(settings, logs):
    settings["agent_decision"]["enabled"] = False
    assert start(settings, MockClient()) is None
    assert logs.messages == []


@pytest.mark.parametrize("reason", ["no_evidence", "oversize"])
def test_skipped_completion_explains_why_no_request_was_sent(settings, logs, reason):
    run = start(settings, MockClient())
    try:
        assert run.resolve(wait_seconds=0.5).route == "tools"
        evidence = [] if reason == "no_evidence" else [{"content": PRIVATE_KEY + "x" * 50000}]
        assert run.start_completion(PRIVATE_STATE, evidence) is False
        skipped = logs.wait_for("stage=completion", "status=skip")
        assert reason in skipped
        assert not any("stage=completion" in line and "status=start" in line for line in logs.messages)
        assert_safe(logs)
    finally:
        run.close()


def test_saturated_worker_slots_record_busy_without_queueing(settings, logs, monkeypatch):
    monkeypatch.setattr(runtime, "_REQUEST_SLOTS", threading.BoundedSemaphore(0))
    assert start(settings, MockClient()) is None
    assert "busy" in logs.wait_for("stage=route", "status=fallback")
    assert_safe(logs)


@pytest.mark.parametrize("close", [False, True])
def test_expired_or_closed_result_is_logged_as_discarded_not_done(settings, logs, close):
    settings["agent_decision"]["request_timeout"] = 0.03
    gate = threading.Event()
    client = MockClient(gate=gate)
    run = start(settings, client)
    try:
        assert client.entered.wait(0.5)
        if close:
            run.close()
            assert run.resolve() is None
        else:
            assert run.resolve(wait_seconds=1).route == "uncertain"
        gate.set()
        discarded = logs.wait_for("stage=route", "status=discarded")
        if close:
            assert "closed" in discarded or "invalidated" in discarded
        else:
            assert any("timeout" in line or "expired" in line for line in logs.messages)
        assert not any("stage=route" in line and "status=done" in line for line in logs.messages)
        assert not any("status=adopted" in line for line in logs.messages)
        assert_safe(logs)
    finally:
        gate.set()
        run.close()


def test_old_completion_version_is_discarded_and_cannot_report_success(settings, logs):
    old_entered = threading.Event()
    old_gate = threading.Event()

    class VersionedClient(MockClient):
        def __init__(self):
            super().__init__()
            self.completions = 0

        def evaluate(self, state, questions, timeout=None):
            if "missing_steps" not in questions:
                return super().evaluate(state, questions, timeout)
            self.completions += 1
            if self.completions == 1:
                old_entered.set()
                assert old_gate.wait(2)
                return {"missing_steps": {"noul": 0.99}, "insufficient_evidence": {"noul": 0.99}}
            return {"missing_steps": {"noul": 0.01}, "insufficient_evidence": {"noul": 0.01}}

    run = start(settings, VersionedClient())
    try:
        assert run.resolve(wait_seconds=0.5).route == "tools"
        assert run.start_completion(PRIVATE_STATE, [{"content": "旧证据"}])
        assert old_entered.wait(0.5)
        assert run.start_completion(PRIVATE_STATE, [{"content": "新证据"}])
        assert run.resolve_completion(wait_seconds=0.5).action == "finalize"
        old_gate.set()
        logs.wait_for("stage=completion", "status=discarded")
        successful = [line for line in logs.messages if "stage=completion" in line and "status=done" in line]
        assert len(successful) == 1
        assert "action=finalize" in successful[0]
        assert_safe(logs)
    finally:
        old_gate.set()
        run.close()


def test_jev_file_contains_decisions_and_excludes_ordinary_logs(tmp_path):
    # 用独立进程创建真实日志句柄，避免重置正在使用的全局 logger。
    env = os.environ.copy()
    env["COW_DATA_DIR"] = str(tmp_path)
    script = """
import logging
from common.log import logger, file_logger, jev_logger
logger.info('ordinary_console_log_marker')
file_logger.info('ordinary_file_log_marker')
jev_logger.info('[Jev] run=12345678 stage=route status=done route=direct elapsed_ms=12')
for current in (logger, file_logger, jev_logger):
    for handler in current.handlers:
        handler.flush()
logging.shutdown()
"""
    subprocess.run([sys.executable, "-c", script], check=True, env=env, capture_output=True, text=True)
    dedicated = (tmp_path / "jev.log").read_text(encoding="utf-8")
    combined = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert "[Jev]" in dedicated
    assert "ordinary_console_log_marker" not in dedicated
    assert "ordinary_file_log_marker" not in dedicated
    assert "ordinary_console_log_marker" in combined
    assert "ordinary_file_log_marker" in combined
    assert "[Jev]" in combined
