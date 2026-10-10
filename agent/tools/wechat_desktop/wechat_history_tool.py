"""微信当前会话或数据库稳定会话 ID 的只读历史工具。"""

from __future__ import annotations

import json

from agent.tools.base_tool import BaseTool, ToolResult
from channel.wechat_desktop.storage.service import get_wechat_desktop_service


class WechatHistoryTool(BaseTool):
    """让 Agent 明确地读取微信历史消息，不改变实时接收游标。"""

    name = "wechat_history"
    description = (
        "Read recent Windows WeChat messages. Without conversation_id, read "
        "the currently open conversation. With the db_uia backend, use a stable "
        "conversation_id from wechat_desktop search_contacts to read another "
        "conversation without switching chats. Use whenever the user asks to get,查看,读取, "
        "查找 or summarize recent WeChat chat history/messages. It never "
        "switches chats, sends messages, downloads attachments, or writes the "
        "result to the persistent conversation history database or advances "
        "the live message cursor. UIA-only backends reject conversation_id. Returns "
        "structured JSON."
    )
    params = {
        "type": "object",
        "properties": {
            "conversation_id": {
                "type": "string",
                "default": "",
                "description": "Stable ID from search_contacts; requires db_uia. Empty reads the current chat.",
            },
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

        action_params = {"limit": min(limit, 50)}
        conversation_id = params.get("conversation_id", "")
        if not isinstance(conversation_id, str):
            return ToolResult.fail("conversation_id must be a string")
        if conversation_id.strip():
            action_params["conversation_id"] = conversation_id.strip()
        result = get_wechat_desktop_service().execute_agent_action(
            "read_history", **action_params
        )
        payload = json.dumps(result, ensure_ascii=False)
        if result.get("status") == "error":
            return ToolResult.fail(payload)
        return ToolResult.success(payload)
