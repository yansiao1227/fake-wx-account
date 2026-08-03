import json

import pytest

from scripts import replay_openai_failed_request as replay


def test_select_record_supports_latest_positive_and_negative_indices():
    records = [{"id": 1}, {"id": 2}, {"id": 3}]

    assert replay.select_record(records, "latest")["id"] == 3
    assert replay.select_record(records, "1")["id"] == 1
    assert replay.select_record(records, "-1")["id"] == 3
    with pytest.raises(RuntimeError):
        replay.select_record(records, "0")


def test_replay_once_parses_stream_and_uses_logged_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200
        headers = {"x-request-id": "req-1"}

        @staticmethod
        def iter_lines(decode_unicode=False):
            assert decode_unicode is False
            yield b'data: {"choices":[{"delta":{"content":"OK"}}]}'
            yield b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call-1","function":{"name":"vision","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}'
            yield b"data: [DONE]"

        @staticmethod
        def close():
            return None

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(replay.requests, "post", fake_post)
    payload = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }
    record = {
        "url": "https://gateway.example/v1/chat/completions",
        "query": {},
        "request_body": payload,
    }
    config = {
        "custom_api_base": "https://gateway.example/v1",
        "custom_api_key": "secret-key",
    }

    result = replay.replay_once(
        record,
        config,
        timeout=30,
        allow_endpoint_mismatch=False,
        raw=False,
    )

    assert captured["json"] is payload
    assert captured["stream"] is True
    assert captured["headers"]["Authorization"] == "Bearer secret-key"
    assert result["status_code"] == 200
    assert result["request_id"] == "req-1"
    assert result["content"] == "OK"
    assert result["tool_calls"][0]["name"] == "vision"
    assert result["finish_reason"] == "tool_calls"


def test_replay_once_rejects_endpoint_mismatch(monkeypatch):
    monkeypatch.setattr(
        replay.requests,
        "post",
        lambda *_args, **_kwargs: pytest.fail("request must not be sent"),
    )
    with pytest.raises(RuntimeError, match="does not match"):
        replay.replay_once(
            {
                "url": "https://unexpected.example/v1/chat/completions",
                "request_body": {"messages": [], "stream": False},
            },
            {
                "custom_api_base": "https://gateway.example/v1",
                "custom_api_key": "secret-key",
            },
            timeout=30,
            allow_endpoint_mismatch=False,
            raw=False,
        )
