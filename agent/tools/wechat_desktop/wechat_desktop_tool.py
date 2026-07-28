import json

from agent.tools.base_tool import BaseTool, ToolResult
from channel.wechat_desktop.service import get_wechat_desktop_service


class WechatDesktopTool(BaseTool):
    """Policy-protected actions for the normal Windows WeChat client."""

    name = "wechat_desktop"
    description = (
        "Send a message through the normal Windows WeChat desktop client using "
        "the dedicated wechat_desktop policy executor. Use this tool instead "
        "of generic computer clicks for outbound WeChat messages. Allowlisted "
        "text may send immediately; other actions are queued "
        "for approval in the Web console."
    )
    params = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "send_text"],
            },
            "conversation": {
                "type": "string",
                "default": "",
                "description": "Exact contact or group name.",
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
