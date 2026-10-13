import traceback
from types import SimpleNamespace

import pytest
import requests

from agent.decision import JevClient, JevError
import agent.decision.jev as jev_module
import models.custom_provider as custom_module


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch, tmp_path):
    for name in (
        "TYPESAFE_API_BASE", "TYPESAFE_API_KEY", "NEWAPI_API_BASE",
        "NEWAPI_BASE_URL", "NEWAPI_API_KEY", "CUSTOM_API_BASE", "CUSTOM_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(jev_module, "expand_path", lambda value: str(tmp_path / "missing.env"))
    monkeypatch.setattr(jev_module, "conf", lambda: {})
    monkeypatch.setattr(custom_module, "conf", lambda: {})


def configure(monkeypatch, config):
    monkeypatch.setattr(jev_module, "conf", lambda: config)
    monkeypatch.setattr(custom_module, "conf", lambda: config)


def questions():
    return {
        "route": {"type": "choice", "instructions": "选择路由", "criteria": {"direct": "直接回答", "tools": "使用工具"}},
        "search": {"type": "noul", "instructions": "是否搜索"},
        "difficulty": {"type": "score", "instructions": "难度", "criteria": ["容易", "困难"]},
    }


def answers():
    return {
        "route": {"type": "choice", "choice": "tools", "probabilities": {"direct": 0.1, "tools": 0.9}, "confidence": 0.8},
        "search": {"type": "noul", "noul": 0.95},
        "difficulty": {"type": "score", "score": 0.8, "probabilities": {"0": 0.2, "1": 0.8}, "confidence": 0.7, "legend": {"0": "容易", "1": "困难"}},
    }


@pytest.mark.parametrize("base, expected", [
    ("https://gateway.invalid", "https://gateway.invalid/typesafe/v1/systemone"),
    ("https://gateway.invalid/v1/", "https://gateway.invalid/typesafe/v1/systemone"),
    ("https://gateway.invalid/proxy/v1", "https://gateway.invalid/proxy/typesafe/v1/systemone"),
    ("https://gateway.invalid/typesafe/v1/systemone/", "https://gateway.invalid/typesafe/v1/systemone"),
    ("https://gateway.invalid/typesafe/v1", "https://gateway.invalid/typesafe/v1/systemone"),
])
def test_gateway_endpoint_normalization(base, expected):
    assert JevClient(base, "test-token").endpoint == expected


@pytest.mark.parametrize("base", [
    "", "gateway.invalid", "file:///private", "https://user:secret@gateway.invalid",
    "https://gateway.invalid?api_key=secret", "https://gateway.invalid#fragment",
])
def test_invalid_endpoint_is_rejected_without_exposing_url(base):
    with pytest.raises(JevError) as caught:
        JevClient(base, "test-token")
    assert str(caught.value) == "Jev configuration"


def test_selected_provider_credentials_are_kept_together(monkeypatch):
    configure(monkeypatch, {
        "bot_type": "custom:selected",
        "custom_api_base": "https://legacy.invalid/v1",
        "custom_api_key": "legacy-token",
        "custom_providers": [
            {"id": "other", "api_base": "https://other.invalid/v1", "api_key": "other-token"},
            {"id": "selected", "api_base": "https://newapi.invalid/v1", "api_key": "selected-token"},
        ],
    })
    client = JevClient.from_config()
    assert client.endpoint == "https://newapi.invalid/typesafe/v1/systemone"
    assert client.api_key == "selected-token"


def test_missing_provider_id_does_not_reuse_legacy_credentials(monkeypatch):
    configure(monkeypatch, {"bot_type": "custom:missing", "custom_providers": [], "custom_api_base": "https://legacy.invalid", "custom_api_key": "legacy-token"})
    assert JevClient.from_config() is None


def test_unrelated_vendor_and_partial_env_do_not_supply_credentials(monkeypatch):
    configure(monkeypatch, {"bot_type": "openai", "open_ai_api_base": "https://openai.invalid/v1", "open_ai_api_key": "other-token"})
    monkeypatch.setenv("NEWAPI_API_BASE", "https://gateway.invalid")
    monkeypatch.setenv("CUSTOM_API_KEY", "custom-token")
    assert JevClient.from_config() is None


def test_newapi_environment_pair_overrides_shared_custom_pair(monkeypatch):
    configure(monkeypatch, {"bot_type": "custom", "custom_api_base": "https://custom.invalid/v1", "custom_api_key": "custom-token"})
    monkeypatch.setenv("NEWAPI_BASE_URL", "https://newapi.invalid/v1")
    monkeypatch.setenv("NEWAPI_API_KEY", "newapi-token")
    client = JevClient.from_config()
    assert client.endpoint == "https://newapi.invalid/typesafe/v1/systemone"
    assert client.api_key == "newapi-token"


def test_typesafe_env_precedes_dedicated_configuration(monkeypatch):
    configure(monkeypatch, {"agent_decision": {"api_base": "https://configured.invalid", "api_key": "configured-token", "model": "custom-jev", "timeout_seconds": 1.25}})
    monkeypatch.setenv("TYPESAFE_API_BASE", "https://dedicated.invalid/v1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "dedicated-token")
    client = JevClient.from_config()
    assert client.endpoint == "https://dedicated.invalid/typesafe/v1/systemone"
    assert client.api_key == "dedicated-token"
    assert client.model == "custom-jev"
    assert client.timeout == 1.25


def test_configured_base_does_not_send_shared_key_to_different_host(monkeypatch):
    configure(monkeypatch, {"bot_type": "custom", "custom_api_base": "https://gateway.invalid/v1", "custom_api_key": "shared-token"})
    assert JevClient.from_config({"api_base": "https://different.invalid"}) is None
    assert JevClient.from_config({"api_base": "https://gateway.invalid"}).api_key == "shared-token"


def test_dedicated_config_and_missing_key_do_not_trigger_requests(monkeypatch):
    def forbidden_request(*args, **kwargs):
        pytest.fail("凭据缺失时不应发出请求")
    monkeypatch.setattr(jev_module.requests, "post", forbidden_request)
    assert JevClient.from_config({"api_base": "https://gateway.invalid"}) is None
    assert JevClient.from_config({"api_base": "https://gateway.invalid", "api_key": "YOUR_API_KEY"}) is None
    client = JevClient.from_config({"api_base": "https://gateway.invalid", "api_key": "test-token", "enabled": False})
    assert client is not None  # enabled 属于主循环控制，不由 HTTP 客户端控制。


def test_canonical_env_loaded_before_credential_resolution(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NEWAPI_API_BASE=https://canonical.invalid/v1\nNEWAPI_API_KEY=canonical-token\n", encoding="utf-8")
    monkeypatch.setenv("NEWAPI_API_BASE", "https://stale.invalid")
    monkeypatch.setenv("NEWAPI_API_KEY", "stale-token")
    monkeypatch.setattr(jev_module, "expand_path", lambda value: str(env_file) if value == "~/.cow/.env" else value)
    client = JevClient.from_config()
    assert client.endpoint == "https://canonical.invalid/typesafe/v1/systemone"
    assert client.api_key == "canonical-token"
    # dotenv 的写入通过 monkeypatch 注册，fixture 结束时恢复环境。


def test_evaluate_sends_single_typed_request_and_returns_answers(monkeypatch):
    calls = []
    expected = answers()
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"answers": expected, "debug": "hidden"})
    monkeypatch.setattr(jev_module.requests, "post", post)
    client = JevClient("https://gateway.invalid/v1", "test-token")
    result = client.evaluate({"user": "搜索最新信息"}, questions(), timeout=0.75)
    assert result == expected
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://gateway.invalid/typesafe/v1/systemone"
    assert kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert kwargs["json"] == {"model": "jev-1.13", "state": {"user": "搜索最新信息"}, "questions": questions()}
    assert kwargs["timeout"] == 0.75
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("mutate", [
    lambda body: body.pop("answers"),
    lambda body: body["answers"].pop("search"),
    lambda body: body["answers"]["search"].update(type="choice"),
    lambda body: body["answers"]["search"].update(noul=True),
    lambda body: body["answers"]["search"].update(noul=float("nan")),
    lambda body: body["answers"]["route"].update(choice="unknown"),
    lambda body: body["answers"]["route"].update(probabilities={"tools": 1.0}),
    lambda body: body["answers"]["route"].update(confidence=1.5),
    lambda body: body["answers"]["difficulty"].update(score=-1),
    lambda body: body["answers"]["difficulty"].update(legend={}),
])
def test_invalid_response_is_rejected(monkeypatch, mutate):
    body = {"answers": answers()}
    mutate(body)
    monkeypatch.setattr(jev_module.requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: body))
    with pytest.raises(JevError, match="^Jev response$"):
        JevClient("https://gateway.invalid", "test-token").evaluate("text", questions())


@pytest.mark.parametrize("error, category", [
    (requests.Timeout("secret-token private-state"), "timeout"),
    (requests.ConnectionError("secret-token private-state"), "transport"),
])
def test_request_failure_is_sanitized_and_not_retried(monkeypatch, caplog, error, category):
    count = 0
    def post(*args, **kwargs):
        nonlocal count
        count += 1
        raise error
    monkeypatch.setattr(jev_module.requests, "post", post)
    client = JevClient("https://gateway.invalid", "secret-token")
    state = "private-state"
    with pytest.raises(JevError) as caught:
        client.evaluate(state, questions())
    assert caught.value.category == category
    assert count == 1
    assert str(caught.value) == f"Jev {category}"
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert "secret-token" not in rendered + repr(client) + caplog.text
    assert "private-state" not in rendered + repr(client) + caplog.text


def test_http_errors_do_not_parse_or_expose_response_body(monkeypatch, caplog):
    def forbidden_json():
        pytest.fail("错误响应不应读取正文")
    monkeypatch.setattr(jev_module.requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=401, text="secret-token private-state", json=forbidden_json))
    with pytest.raises(JevError) as caught:
        JevClient("https://gateway.invalid", "secret-token").evaluate("private-state", questions())
    assert str(caught.value) == "Jev http (401)"
    assert caught.value.status_code == 401
    assert "secret-token" not in caplog.text
    assert "private-state" not in caplog.text


def test_decode_errors_are_sanitized(monkeypatch):
    def bad_json():
        raise ValueError("secret-token server-body")
    monkeypatch.setattr(jev_module.requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=200, json=bad_json))
    with pytest.raises(JevError) as caught:
        JevClient("https://gateway.invalid", "test-token").evaluate("private-state", questions())
    assert str(caught.value) == "Jev response"


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), "invalid"])
def test_invalid_timeout_disables_client(timeout):
    assert JevClient.from_config({"api_base": "https://gateway.invalid", "api_key": "test-token", "timeout_seconds": timeout}) is None
