import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def pause_api(monkeypatch):
    from channel.web import web_channel
    from channel.wechat_desktop.storage import service as service_module

    calls = []

    def set_paused(paused):
        calls.append(paused)
        return {"paused": paused}

    fake_service = SimpleNamespace(set_paused=set_paused)
    monkeypatch.setattr(service_module, "get_wechat_desktop_service", lambda: fake_service)
    monkeypatch.setattr(web_channel, "_require_wechat_desktop_security", lambda: None)
    monkeypatch.setattr(web_channel.web, "header", lambda *args, **kwargs: None, raising=False)

    def post(body):
        monkeypatch.setattr(
            web_channel.web, "data", lambda: json.dumps(body).encode(), raising=False
        )
        return json.loads(web_channel.WechatDesktopPauseHandler().POST())

    return SimpleNamespace(post=post, calls=calls, module=web_channel)


@pytest.mark.parametrize("paused", [True, False])
def test_pause_api_accepts_json_boolean_without_coercion(pause_api, paused):
    response = pause_api.post({"paused": paused})

    assert response == {"status": "success", "desktop": {"paused": paused}}
    assert pause_api.calls == [paused]
    assert pause_api.calls[0] is paused


@pytest.mark.parametrize("paused", ["false", "true", "", 0, 1, None, [], {}])
def test_pause_api_rejects_non_boolean_without_changing_service(pause_api, paused):
    response = pause_api.post({"paused": paused})

    assert response == {"status": "error", "message": "paused must be a JSON boolean"}
    assert pause_api.calls == []


@pytest.mark.parametrize("body", [{}, [], ["paused"], None, "paused", 0])
def test_pause_api_requires_paused_in_json_object(pause_api, body):
    response = pause_api.post(body)

    assert response == {"status": "error", "message": "paused is required"}
    assert pause_api.calls == []


def test_pause_api_requires_security_before_changing_service(pause_api, monkeypatch):
    def reject_request():
        raise PermissionError("desktop endpoint denied")

    monkeypatch.setattr(
        pause_api.module, "_require_wechat_desktop_security", reject_request
    )

    with pytest.raises(PermissionError, match="desktop endpoint denied"):
        pause_api.post({"paused": False})

    assert pause_api.calls == []
