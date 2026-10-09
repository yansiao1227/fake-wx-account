from __future__ import annotations

from typing import Iterable

from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.triggers import group_message_triggered


class WechatDesktopPolicy:
    """集中处理白名单、黑名单、群触发方式和发送频率限制。"""

    def __init__(self, config: dict, store):
        self.config = config
        self.store = store

    @staticmethod
    def _matches(value: str, allowed: Iterable[str]) -> bool:
        normalized = str(value or "").strip().casefold()
        return any(normalized == str(item).strip().casefold() for item in allowed or [])

    def is_allowlisted(self, target: str, is_group: bool) -> bool:
        """检查联系人或群聊是否允许自动回复。"""

        allow_all_key = (
            "auto_reply_groups_all" if is_group else "auto_reply_private_all"
        )
        if bool(self.config.get(allow_all_key, False)):
            return True
        key = "auto_reply_groups" if is_group else "auto_reply_contacts"
        return self._matches(target, self.config.get(key, []))

    def is_blocked(self, target: str) -> bool:
        """黑名单优先级高于全部放行和白名单。"""

        return self._matches(
            target, self.config.get("auto_reply_blacklist", [])
        )

    def group_triggered(self, event: WechatDesktopEvent) -> bool:
        """根据群回复模式判断消息是否触发 Agent。"""

        if not event.is_group:
            return True
        return group_message_triggered(self.config, event.content, event.is_at)

    def allows_send(self, target: str, is_group: bool, content_type: str) -> bool:
        """无副作用的策略检查；发送额度由发送服务在操作前预留。"""
        if bool(self.config.get("shadow_mode", True)) or self.is_blocked(target):
            return False
        if not self.is_allowlisted(target, is_group):
            return False
        return content_type == "text" or (
            content_type == "image" and bool(self.config.get("auto_send_images", False))
        )

    def reserve_send(self, units: int = 1) -> bool:
        from channel.wechat_desktop.config import DEFAULT_CONFIG
        return self.store.allow_rate(
            int(self.config.get("max_send_per_minute", DEFAULT_CONFIG["max_send_per_minute"])),
            int(self.config.get("max_send_per_hour", DEFAULT_CONFIG["max_send_per_hour"])),
            units=units,
        )
