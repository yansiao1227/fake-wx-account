import json

import pytest

from models.openai import openai_http_client as http_module
from models.openai.openai_http_client import OpenAIHTTPClient, OpenAIHTTPError


class _FailedResponse:
    status_code = 400
    headers = {"x-request-id": "req-replay-1"}
    text = '{"error":{"message":"invalid request"}}'

    @staticmethod
    def json():
        return {
            "error": {
                "message": "invalid request",
                "code": "invalid_request",
            }
        }


def _read_record(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_failed_sync_request_records_replayable_payload_without_credentials(
    monkeypatch, tmp_path
):
    log_path = tmp_path / "failed.jsonl"
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return _FailedResponse()

    monkeypatch.setattr(http_module, "_FAILED_REQUEST_LOG", log_path)
    monkeypatch.setattr(http_module.requests, "post", fake_post)
    client = OpenAIHTTPClient(
        api_key="secret-key",
        api_base="https://gateway.example/v1",
    )

    with pytest.raises(OpenAIHTTPError):
        client.chat_completions(
            model="test-model",
            messages=[{"role": "user", "content": "hello"}],
        )

    record = _read_record(log_path)
    assert record["url"] == "https://gateway.example/v1/chat/completions"
    assert record["request_body"] == captured["json"]
    assert record["request_body"]["stream"] is False
    assert record["response"]["status_code"] == 400
    assert record["response"]["request_id"] == "req-replay-1"
    assert "secret-key" not in log_path.read_text(encoding="utf-8")


def test_failed_stream_request_preserves_base64_payload(monkeypatch, tmp_path):
    log_path = tmp_path / "failed.jsonl"
    image_url = "data:image/png;base64,ZmFrZS1pbWFnZQ=="

    monkeypatch.setattr(http_module, "_FAILED_REQUEST_LOG", log_path)
    monkeypatch.setattr(
        http_module.requests,
        "post",
        lambda *_args, **_kwargs: _FailedResponse(),
    )
    client = OpenAIHTTPClient(api_base="https://gateway.example/v1")
    stream = client.chat_completions(
        model="test-model",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe"},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
        stream=True,
    )

    chunks = list(stream)

    record = _read_record(log_path)
    assert chunks[0]["status_code"] == 400
    assert record["request_body"]["stream"] is True
    assert (
        record["request_body"]["messages"][0]["content"][1]["image_url"]["url"]
        == image_url
    )
