from types import SimpleNamespace

import pytest

from agent.protocol import agent_stream as agent_stream_module
from agent.protocol.agent_stream import AgentStreamExecutor
from bridge import agent_bridge as agent_bridge_module
from bridge.agent_bridge import AgentBridge
from bridge.reply import ReplyType
from models import model_api_retry as retry_module
from models.model_api_retry import (
    ModelApiRetriesExhausted,
    ModelApiRetryPolicy,
    get_model_api_retry_policy,
    is_retryable_model_api_error,
    model_api_failure_message,
    model_api_retry_delay,
)


def test_model_api_retry_policy_is_configurable_and_bounded(monkeypatch):
    monkeypatch.setattr(
        retry_module,
        "conf",
        lambda: {
            "model_api_max_retries": 99,
            "model_api_retry_base_seconds": -1,
            "model_api_retry_max_seconds": 999,
            "model_api_retry_jitter_seconds": -1,
        },
    )

    policy = get_model_api_retry_policy()

    assert policy == ModelApiRetryPolicy(
        max_retries=10,
        base_seconds=0.0,
        max_seconds=300.0,
        jitter_seconds=0.0,
    )


def test_model_api_retry_classifies_transient_and_permanent_errors():
    assert is_retryable_model_api_error("protocol_mismatch (Status: 400)")
    assert is_retryable_model_api_error(
        "invalid_request: please check request parameters"
    )
    assert is_retryable_model_api_error("rate limit (Status: 429)")
    assert is_retryable_model_api_error("Connection reset by peer")
    assert not is_retryable_model_api_error("invalid API key (Status: 401)")
    assert not is_retryable_model_api_error("permission_denied (Status: 403)")


def test_model_api_retry_delay_uses_bounded_exponential_backoff():
    policy = ModelApiRetryPolicy(
        max_retries=3,
        base_seconds=2.0,
        max_seconds=10.0,
        jitter_seconds=0.0,
    )

    assert model_api_retry_delay(0, "timeout", policy) == 2.0
    assert model_api_retry_delay(1, "timeout", policy) == 4.0
    assert model_api_retry_delay(5, "timeout", policy) == 10.0
    assert model_api_retry_delay(0, "status: 429", policy) == 5.0


def test_model_api_failure_message_uses_configured_fun_copy(monkeypatch):
    monkeypatch.setattr(
        retry_module,
        "conf",
        lambda: {"model_api_failure_messages": ["服务器去喝茶了 ☕ 请再试一次。"]},
    )

    assert model_api_failure_message() == "服务器去喝茶了 ☕ 请再试一次。"


class _StreamingModel:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def call_stream(self, _request):
        self.calls += 1
        if self.calls <= self.failures:
            yield {
                "error": {
                    "message": "protocol_mismatch",
                    "code": "protocol_mismatch",
                    "type": "invalid_request_error",
                },
                "status_code": 400,
            }
            return
        yield {
            "choices": [
                {
                    "delta": {"content": "恢复啦"},
                    "finish_reason": "stop",
                }
            ]
        }


def _stream_executor(monkeypatch, failures, max_retries):
    policy = ModelApiRetryPolicy(
        max_retries=max_retries,
        base_seconds=0.0,
        max_seconds=0.0,
        jitter_seconds=0.0,
    )
    monkeypatch.setattr(
        agent_stream_module,
        "get_model_api_retry_policy",
        lambda: policy,
    )
    monkeypatch.setattr(
        agent_stream_module,
        "model_api_retry_delay",
        lambda *_args: 0,
    )
    model = _StreamingModel(failures)
    executor = AgentStreamExecutor(
        agent=SimpleNamespace(memory_manager=None),
        model=model,
        system_prompt="",
        tools=[],
        messages=[{"role": "user", "content": "hello"}],
    )
    return executor, model


def test_agent_model_api_retries_protocol_error_then_succeeds(monkeypatch):
    executor, model = _stream_executor(monkeypatch, failures=2, max_retries=2)

    content, tool_calls = executor._call_llm_stream()

    assert content == "恢复啦"
    assert tool_calls == []
    assert model.calls == 3


def test_agent_model_api_raises_terminal_error_after_retries(monkeypatch):
    executor, model = _stream_executor(monkeypatch, failures=99, max_retries=2)

    with pytest.raises(ModelApiRetriesExhausted) as caught:
        executor._call_llm_stream()

    assert caught.value.retries == 2
    assert model.calls == 3


def test_agent_bridge_returns_fun_text_for_terminal_model_api_failure(monkeypatch):
    bridge = object.__new__(AgentBridge)

    class _Agent:
        tools = []
        model = SimpleNamespace()

        @staticmethod
        def run_stream(**_kwargs):
            raise ModelApiRetriesExhausted("provider unavailable", retries=3)

    bridge.get_agent = lambda session_id=None: _Agent()
    monkeypatch.setattr(
        agent_bridge_module,
        "model_api_failure_message",
        lambda: "模型去追蝴蝶了 🦋 请再试一次。",
    )

    reply = bridge.agent_reply("hello")

    assert reply.type == ReplyType.TEXT
    assert reply.content == "模型去追蝴蝶了 🦋 请再试一次。"
