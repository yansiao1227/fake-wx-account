import json

from agent.tools.base_tool import BaseTool, ToolResult
from channel.wechat_desktop.storage.service import get_wechat_desktop_service


class WechatDesktopTool(BaseTool):
    """Policy-protected actions for the normal Windows WeChat client."""

    name = "wechat_desktop"
    description = (
        "Send text through the dedicated wechat_desktop policy executor, or "
        "report desktop WeChat status. With db_uia, search_contacts reads contacts "
        "and returns stable conversation_id values without switching chats or "
        "changing live message cursors. For chat history, use wechat_history."
    )
    params = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "read_history", "search_contacts", "send_text"],
            },
            "conversation": {
                "type": "string",
                "default": "",
                "description": "Exact contact or group name.",
            },
            "conversation_id": {
                "type": "string",
                "default": "",
                "description": "Stable ID from search_contacts when action=read_history; requires db_uia.",
            },
            "query": {
                "type": "string",
                "default": "",
                "description": "Contact/group name, remark, alias or username to find when action=search_contacts.",
            },
            "text": {
                "type": "string",
                "default": "",
                "description": "Text to send when action=send_text.",
            },
            "is_group": {
                "type": "boolean",
                "default": False,
                "description": "Set true only when conversation is a group.",
            },
            "limit": {
                "type": "integer",
                "default": 20,
                "minimum": 1,
                "maximum": 50,
                "description": "Message or contact count when reading history or searching contacts; at most 50.",
            },
        },
        "required": ["action"],
    }

    def execute(self, params: dict) -> ToolResult:
        service = get_wechat_desktop_service()
        action = str(params.get("action", "") or "").strip()
        if action == "status":
            return ToolResult.success(
                json.dumps(service.status(), ensure_ascii=False)
            )
        if action in {"read_history", "search_contacts"}:
            raw_limit = params.get("limit", 20)
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                return ToolResult.fail("limit must be an integer")
            if limit < 1:
                return ToolResult.fail("limit must be at least 1")
            action_params = {"limit": min(limit, 50)}
            if action == "read_history":
                conversation_id = params.get("conversation_id", "")
                if not isinstance(conversation_id, str):
                    return ToolResult.fail("conversation_id must be a string")
                if conversation_id.strip():
                    action_params["conversation_id"] = conversation_id.strip()
            else:
                query = params.get("query", "")
                if not isinstance(query, str):
                    return ToolResult.fail("query must be a string")
                action_params["query"] = query.strip()
            result = service.execute_agent_action(action, **action_params)
            payload = json.dumps(result, ensure_ascii=False)
            if result.get("status") == "error":
                return ToolResult.fail(payload)
            return ToolResult.success(payload)
        if action != "send_text":
            return ToolResult.fail(f"unsupported action: {action}")

        result = service.execute_agent_action(
            "send_text",
            conversation=str(params.get("conversation", "") or ""),
            text=str(params.get("text", "") or ""),
            is_group=bool(params.get("is_group", False)),
        )
        payload = json.dumps(result, ensure_ascii=False)
        if result.get("status") == "error":
            return ToolResult.fail(payload)
        return ToolResult.success(payload)
