"""数据库只读工具接口测试；不读取真实微信数据或操作桌面。"""

import json
from types import SimpleNamespace

import pytest

from agent.tools.wechat_desktop.wechat_desktop_tool import WechatDesktopTool
from agent.tools.wechat_desktop.wechat_history_tool import WechatHistoryTool
from channel.wechat_desktop.config import load_wechat_desktop_config
from channel.wechat_desktop.models import WechatHistoryMessage, WechatHistoryReadResult
from channel.wechat_desktop.pipeline.send import WechatDesktopSendMixin
from channel.wechat_desktop.storage.service import WechatDesktopService
from channel.wechat_desktop.storage.store import WechatDesktopStore


class ReadBackend:
    """读接口记录调用；任何发送、切窗或游标写入都会导致测试失败。"""

    def __init__(self):
        self.calls = []
        self.cursor = {"message_0:Msg_1": 17}

    def _history(self, limit, conversation_id=""):
        return WechatHistoryReadResult(
            conversation_title="测试联系人", conversation_type="private",
            messages=[WechatHistoryMessage(
                sender_name="测试联系人", direction="incoming", content_type="text",
                content="合成消息", message_id="native:18", source_message_id="18",
                native_timestamp=1791388800, source="wechat_database",
            )], requested_limit=limit, returned_count=1,
            source="wechat_database", history_window_opened=False,
            conversation_id=conversation_id,
        )

    def read_current_chat_history(self, limit):
        self.calls.append(("current", limit))
        return self._history(limit, "account:wxid_synthetic")

    def read_chat_history(self, conversation_id, limit):
        self.calls.append(("history", conversation_id, limit))
        return self._history(limit, conversation_id)

    def search_contacts(self, query, limit):
        self.calls.append(("contacts", query, limit))
        return {
            "contacts": [{"conversation_id": "account:wxid_synthetic", "display_name": "测试联系人"}],
            "source": "wechat_database",
        }

    def send_text(self, *args):
        raise AssertionError("只读工具不能发送消息")

    def resolve_target(self, *args):
        raise AssertionError("只读工具不能切换会话")

    def acknowledge_events(self, *args):
        raise AssertionError("只读工具不能推进实时游标")


def make_reader(backend=None, paused=False):
    channel = WechatDesktopSendMixin()
    channel.config = load_wechat_desktop_config({"desktop_backend": "db_uia"})
    channel._driver = backend or ReadBackend()
    channel._service = SimpleNamespace(status=lambda: {"paused": paused})
    return channel


@pytest.mark.parametrize("dedicated", [True, False])
def test_history_tool_routes_default_and_stable_id_without_side_effects(monkeypatch, dedicated):
    channel = make_reader()
    service = SimpleNamespace(execute_agent_action=channel._execute_agent_action)
    tool_module = "wechat_history_tool" if dedicated else "wechat_desktop_tool"
    monkeypatch.setattr(
        f"agent.tools.wechat_desktop.{tool_module}.get_wechat_desktop_service",
        lambda: service,
    )
    tool = WechatHistoryTool() if dedicated else WechatDesktopTool()
    params = {} if dedicated else {"action": "read_history"}
    current = tool.execute(params)
    requested = tool.execute({**params, "conversation_id": "account:wxid_synthetic", "limit": 99})

    assert current.status == requested.status == "success"
    assert channel._driver.calls == [("current", 20), ("history", "account:wxid_synthetic", 50)]
    payload = json.loads(requested.result)
    assert payload["conversation"]["id"] == payload["conversation_id"] == "account:wxid_synthetic"
    assert payload["messages"][0]["native_timestamp"] == 1791388800
    assert payload["messages"][0]["message_id"] == "native:18"
    assert payload["source"] == "wechat_database"
    assert channel._driver.cursor == {"message_0:Msg_1": 17}


def test_contact_search_returns_stable_id_without_side_effects(monkeypatch):
    channel = make_reader()
    monkeypatch.setattr(
        "agent.tools.wechat_desktop.wechat_desktop_tool.get_wechat_desktop_service",
        lambda: SimpleNamespace(execute_agent_action=channel._execute_agent_action),
    )
    result = WechatDesktopTool().execute({"action": "search_contacts", "query": " 测试 ", "limit": 99})
    assert result.status == "success"
    assert json.loads(result.result)["contacts"][0]["conversation_id"] == "account:wxid_synthetic"
    assert channel._driver.calls == [("contacts", "测试", 50)]
    assert channel._driver.cursor == {"message_0:Msg_1": 17}


@pytest.mark.parametrize("action,params", [
    ("read_history", {"conversation_id": "account:wxid_synthetic"}),
    ("search_contacts", {"query": "测试"}),
])
def test_uia_only_capability_error_does_not_read_or_switch(action, params):
    backend = SimpleNamespace(read_current_chat_history=lambda limit: pytest.fail("不能回退到当前会话"))
    channel = make_reader(backend)
    channel.config["desktop_backend"] = "uia"
    result = channel._execute_agent_action(action, **params)
    assert result["status"] == "error"
    assert result["code"] == "capability_unavailable"


@pytest.mark.parametrize("action", ["read_history", "search_contacts"])
def test_paused_read_preserves_existing_policy(action):
    channel = make_reader(paused=True)
    result = channel._execute_agent_action(action)
    assert result["status"] == "error"
    assert result["message"] == "desktop takeover is paused"
    assert channel._driver.calls == []


@pytest.mark.parametrize("key,value", [
    ("desktop_backend", "unknown"), ("db_data_dir", 10), ("db_account", None),
    ("db_cache_dir", []), ("db_poll_interval_seconds", 0),
    ("db_batch_size", 1.5), ("db_snapshot_retry_attempts", False),
    ("db_key_scan_timeout_seconds", float("nan")),
])
def test_database_config_rejects_invalid_values(key, value):
    with pytest.raises(ValueError):
        load_wechat_desktop_config({key: value})


def test_database_status_defaults_available_before_start(tmp_path):
    status = WechatDesktopService(WechatDesktopStore(str(tmp_path / "state.db"))).status()
    assert status["db_read_healthy"] is False
    assert status["db_read_account_id"] == ""
    assert status["db_read_backlog"] == {}
    assert status["db_read_error_code"] == ""
    assert status["account_binding"] == {}
