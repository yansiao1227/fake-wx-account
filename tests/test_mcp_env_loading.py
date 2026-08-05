import json
from types import SimpleNamespace

from agent.tools.tool_manager import ToolManager
import agent.tools.tool_manager as tool_manager_module
import bridge.agent_bridge as agent_bridge_module


def test_mcp_config_loads_canonical_env_before_expanding_headers(
    tmp_path, monkeypatch
):
    env_file = tmp_path / ".cow" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("AGENT_PLAN_API_KEY=ark-test-value\n", encoding="utf-8")

    mcp_file = tmp_path / "cow" / "mcp.json"
    mcp_file.parent.mkdir()
    mcp_file.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "datapro": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {
                            "X-Agent-Plan-Key": "${AGENT_PLAN_API_KEY}"
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("AGENT_PLAN_API_KEY", raising=False)
    monkeypatch.setattr(
        tool_manager_module,
        "expand_path",
        lambda path: str(env_file) if path == "~/.cow/.env" else path,
    )
    fake_manager = SimpleNamespace(_mcp_json_path=lambda: str(mcp_file))

    configs = ToolManager._load_mcp_configs(fake_manager)

    assert configs[0]["headers"]["X-Agent-Plan-Key"] == "ark-test-value"


def test_refresh_all_skills_uses_canonical_env_path(monkeypatch, tmp_path):
    requested_paths = []

    def fake_expand_path(path):
        requested_paths.append(path)
        return str(tmp_path / "missing.env")

    monkeypatch.setattr(agent_bridge_module, "expand_path", fake_expand_path)
    bridge = agent_bridge_module.AgentBridge.__new__(agent_bridge_module.AgentBridge)
    bridge.default_agent = None
    bridge.agents = {}

    assert bridge.refresh_all_skills() == 0
    assert requested_paths == ["~/.cow/.env"]
