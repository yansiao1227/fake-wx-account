"""扫描层与策略层共用的群消息触发规则。"""

from channel.wechat_desktop.config import DEFAULT_CONFIG


def group_requires_mention(config: dict) -> bool:
    return config.get("group_reply_mode", DEFAULT_CONFIG["group_reply_mode"]) == "at_only"


def group_message_triggered(config: dict, content: str, is_at: bool) -> bool:
    mode = config.get("group_reply_mode", DEFAULT_CONFIG["group_reply_mode"])
    prefixes = config.get("group_command_prefixes", DEFAULT_CONFIG["group_command_prefixes"])
    prefix_hit = any(str(content or "").lstrip().startswith(p) for p in prefixes if p)
    if mode == "all":
        return True
    if mode == "at_only":
        return bool(is_at)
    if mode == "prefix":
        return prefix_hit
    return bool(is_at or prefix_hit)
