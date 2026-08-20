"""Read-only tool for the currently open Windows WeChat conversation."""

from __future__ import annotations

import json

from agent.tools.base_tool import BaseTool, ToolResult
from channel.wechat_desktop.service import get_wechat_desktop_service


class WechatHistoryTool(BaseTool):
    """让 Agent 明确地读取当前微信会话的历史消息。"""

    name = "wechat_history"
    description = (
        "Read the recent message history of the currently open Windows WeChat "
        "conversation. Use this tool whenever the user asks to get,查看,读取, "
        "查找 or summarize recent WeChat chat history/messages. It never "
        "switches chats, sends messages, downloads attachments, or writes the "
        "result to the persistent conversation history database. Returns "
        "structured JSON."
    )
    params = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "maximum": 50,
                "description": "Number of recent messages to read, at most 50.",
            }
        },
        "required": [],
    }

    def execute(self, params: dict) -> ToolResult:
        raw_limit = params.get("limit", 20)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return ToolResult.fail("limit must be an integer")
        if limit < 1:
            return ToolResult.fail("limit must be at least 1")

        result = get_wechat_desktop_service().execute_agent_action(
            "read_history", limit=min(limit, 50)
        )
        payload = json.dumps(result, ensure_ascii=False)
        if result.get("status") == "error":
            return ToolResult.fail(payload)
        return ToolResult.success(payload)
