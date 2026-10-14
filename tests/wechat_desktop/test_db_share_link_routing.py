"""分享链接交给 Agent 通用工具，不依赖微信气泡或真实网络。"""

import sqlite3
from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from channel.wechat_desktop.pipeline.materialize import WechatDesktopMaterializeMixin
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from .test_db_reader import table
from .test_db_uia_attachment_bridge import bridge, observe_target, reference_xml


def share_quote(url="https://example.invalid/article?token=synthetic&source=quote", *, subtype=5):
    share = (f"<msg><appmsg><type>{subtype}</type><title>synthetic article</title>"
             f"<url>{escape(url)}</url></appmsg></msg>")
    return reference_xml(49, escape(share))


def forbid_ui(instance, monkeypatch):
    monkeypatch.setattr(instance, "_actions", lambda: pytest.fail("已有原生URL不得访问微信UI"))
    monkeypatch.setattr(instance._binder, "require_attachment_target",
                        lambda *args, **kwargs: pytest.fail("已有原生URL不得绑定微信气泡"))


@pytest.mark.parametrize("bottom", [True, False, None])
@pytest.mark.parametrize("subtype", [4, 5])
def test_native_share_link_routes_to_agent_without_visible_bubble(bridge, monkeypatch, bottom, subtype):
    instance, _, _, client, _, _ = bridge
    event = observe_target(bridge, share_quote(subtype=subtype))
    client.bottom = bottom
    client.messages = []
    forbid_ui(instance, monkeypatch)
    identity = event.fingerprint(), event.content_signature, event.source_message_id
    result, count = instance.materialize_event(event)

    assert count == 0
    assert result.reference["content_type"] == "share_card"
    assert result.reference["native_app_type"] == subtype
    assert result.reference["url"] == "https://example.invalid/article?token=synthetic&source=quote"
    assert result.reference["fetch_status"] == "pending_tool"
    assert result.reference["fetch_source"] == "agent_tools"
    assert result.reference["resolved"] is False
    assert result.attachment_status == "link_available"
    assert not result.reference.get("fetched_content")
    assert not WechatDesktopMaterializeMixin._has_referenced_attachment(result)
    assert client.focus_count == 0 and client.downloads == [] and client.ui_calls == []
    assert identity == (result.fingerprint(), result.content_signature, result.source_message_id)


@pytest.mark.parametrize("subtype", [4, 5])
def test_changed_native_share_payload_cannot_be_read_as_trusted_link(bridge, monkeypatch, subtype):
    instance, reader, _, client, _, talker = bridge
    event = observe_target(bridge, share_quote(subtype=subtype))
    cache = reader.caches["message/message_0.db"]
    with sqlite3.connect(cache.path) as conn:
        conn.execute(f'UPDATE "{table(talker)}" SET message_content=? WHERE local_id=2',
                     (share_quote("https://example.invalid/replaced", subtype=subtype),))
    cache.changed = True
    forbid_ui(instance, monkeypatch)

    result, count = instance.materialize_event(event)

    assert count == 0 and result.reference["fetch_status"] == "source_invalid"
    assert result.reference["resolution_error"] == "source_message_changed"
    assert client.focus_count == 0 and client.downloads == []


@pytest.mark.parametrize("subtype", [4, 5])
def test_account_epoch_change_prevents_link_dispatch(bridge, monkeypatch, subtype):
    instance = bridge[0]
    event = observe_target(bridge, share_quote(subtype=subtype))
    original_validate = instance.source.validate_event

    def validate_and_change_epoch(candidate):
        result = original_validate(candidate)
        instance.source._session_epoch += 1
        return result

    monkeypatch.setattr(instance.source, "validate_event", validate_and_change_epoch)
    forbid_ui(instance, monkeypatch)
    result, count = instance.materialize_event(event)

    assert count == 0 and result.reference["fetch_status"] == "source_invalid"
    assert result.reference["resolution_error"] == "account_binding_changed"


@pytest.mark.parametrize("subtype", [4, 5])
def test_existing_browser_body_does_not_need_second_window_read(bridge, monkeypatch, subtype):
    instance = bridge[0]
    event = observe_target(bridge, share_quote(subtype=subtype))
    event.reference["browser_content"] = "synthetic readable article body"
    forbid_ui(instance, monkeypatch)

    result, count = instance.materialize_event(event)

    assert count == 0 and result.reference["fetch_status"] == "direct_browser"
    assert result.reference["resolved"] and not result.reference["degraded"]
    assert result.reference["browser_content"] == "synthetic readable article body"


@pytest.mark.parametrize("subtype", [4, 5])
def test_missing_share_url_still_requires_safe_uia_materialization(bridge, subtype):
    event = observe_target(bridge, reference_xml(
        49, f"&lt;msg&gt;&lt;appmsg&gt;&lt;type&gt;{subtype}&lt;/type&gt;"
            "&lt;title&gt;synthetic article&lt;/title&gt;&lt;/appmsg&gt;&lt;/msg&gt;"))

    assert WechatDesktopMaterializeMixin._has_referenced_attachment(event)


@pytest.mark.parametrize("subtype", [4, 5])
def test_failed_browser_status_does_not_upgrade_body_to_success(bridge, monkeypatch, subtype):
    instance = bridge[0]
    event = observe_target(bridge, share_quote(subtype=subtype))
    event.reference.update(browser_content="synthetic access failure", browser_status="error")
    forbid_ui(instance, monkeypatch)

    result, count = instance.materialize_event(event)

    assert count == 0 and result.reference["fetch_status"] == "pending_tool"
    assert result.reference["browser_status"] == "error" and not result.reference["resolved"]


@pytest.mark.parametrize("subtype", [4, 5])
def test_source_invalid_task_is_not_dispatched_to_agent(bridge, subtype):
    event = observe_target(bridge, share_quote(subtype=subtype))
    event.reference["fetch_status"] = "source_invalid"
    channel = SimpleNamespace(_compose_context=lambda *args, **kwargs: pytest.fail("来源失效不交Agent"))

    assert AgentReplyCoordinator(channel).dispatch(event) is False
