from __future__ import annotations

from typing import Iterable

from channel.wechat_desktop.models import WechatDesktopEvent


class WechatDesktopPolicy:
    def __init__(self, config: dict, store):
        self.config = config
        self.store = store

    @staticmethod
    def _matches(value: str, allowed: Iterable[str]) -> bool:
        normalized = str(value or "").strip().casefold()
        return any(normalized == str(item).strip().casefold() for item in allowed or [])

    def is_allowlisted(self, target: str, is_group: bool) -> bool:
        allow_all_key = (
            "auto_reply_groups_all" if is_group else "auto_reply_private_all"
        )
        if bool(self.config.get(allow_all_key, False)):
            return True
        key = "auto_reply_groups" if is_group else "auto_reply_contacts"
        return self._matches(target, self.config.get(key, []))

    def is_blocked(self, target: str) -> bool:
        return self._matches(
            target, self.config.get("auto_reply_blacklist", [])
        )

    def group_triggered(self, event: WechatDesktopEvent) -> bool:
        if not event.is_group:
            return True
        mode = self.config.get("group_reply_mode", "at_or_prefix")
        prefixes = self.config.get("group_command_prefixes", ["/cow"])
        prefix_hit = any(event.content.lstrip().startswith(p) for p in prefixes if p)
        if mode == "all":
            return True
        if mode == "at_only":
            return bool(event.is_at)
        if mode == "prefix":
            return prefix_hit
        return bool(event.is_at or prefix_hit)

    def can_auto_send(self, target: str, is_group: bool, content_type: str) -> bool:
        if bool(self.config.get("shadow_mode", True)):
            return False
        if self.is_blocked(target):
            return False
        if not self.is_allowlisted(target, is_group):
            return False
        if content_type != "text":
            return bool(self.config.get("auto_send_images", False))
        return self.store.allow_rate(
            int(self.config.get("max_send_per_minute", 5)),
            int(self.config.get("max_send_per_hour", 60)),
        )
