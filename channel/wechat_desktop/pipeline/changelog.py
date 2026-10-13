"""北京时间每日变更群通知，复用通道发送门禁与持久化占位。"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from channel.wechat_desktop.contracts import SendResult, TargetStatus
from channel.wechat_desktop.pipeline.delivery import DeliveryBlocked
from common.log import logger


BEIJING_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")


def read_changelog(directory: Path, day: str) -> str:
    """只读当日文件；空文件、纯标题和模板注释不产生通知。"""
    path = directory / f"{day}.md"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return "\n".join(
        line.rstrip() for line in text.splitlines()
        if not re.match(r"^\s*#{1,6}(?:\s|$)", line)
    ).strip()


class DailyChangelogScheduler:
    """通道运行期间轮询；18 点后可补当天，旧日期与已占位群不会重放。"""

    def __init__(self, *, config, store, backend, policy, send, is_paused,
                 stop_event, clock=None, repository_root=None):
        self.config = config
        self.store = store
        self.backend = backend
        self.policy = policy
        self.send = send
        self.is_paused = is_paused
        self.stop_event = stop_event
        self.clock = clock or (lambda: datetime.now(BEIJING_TIME))
        self.repository_root = Path(repository_root) if repository_root else Path(__file__).resolve().parents[3]

    def _now(self):
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("changelog clock must include a timezone")
        return now.astimezone(BEIJING_TIME)

    def _allowed(self):
        return (self.config["changelog_enabled"] and not self.stop_event.is_set()
                and not self.is_paused() and not self.config["shadow_mode"])

    def _check_day(self, day):
        # 同一门禁会由 UIA 在等待期间与每段实际提交前再次执行。
        now = self._now()
        if (not self._allowed() or now.date().isoformat() != day
                or (now.hour, now.minute) < (self.config["changelog_send_hour"], self.config["changelog_send_minute"])):
            raise DeliveryBlocked("changelog day expired or task disabled")

    def tick(self):
        if not self._allowed():
            return
        now = self._now()
        if (now.hour, now.minute) < (self.config["changelog_send_hour"], self.config["changelog_send_minute"]):
            return
        day = now.date().isoformat()
        directory = Path(self.config["changelog_directory"])
        if not directory.is_absolute():
            directory = self.repository_root / directory
        content = read_changelog(directory, day)
        if not content:
            return
        message = self.config["changelog_message_template"].format(date=day, content=content)
        for group in self.backend.list_group_conversations():
            if not self._allowed() or self._now().date().isoformat() != day:
                return
            if group.is_group is not True or not self.policy.allows_send(group.display_name, True, "text"):
                continue
            if self.store.get_changelog_delivery(day, group.conversation_id):
                continue
            # 枚举后群名可能变化；提交前仍按当前数据库身份和名称授权。
            resolution = self.backend.resolve_send_target(group.conversation_id)
            target = resolution.target
            if (resolution.status != TargetStatus.RESOLVED or target is None
                    or target.is_group is not True or target.conversation_id != group.conversation_id
                    or not self.policy.allows_send(target.display_name, True, "text")):
                continue
            if not self.store.claim_changelog(day, target.conversation_id):
                continue
            try:
                result = SendResult.from_backend(self.send(
                    target.conversation_id, message, policy_target=target.display_name,
                    is_group=True, authorized_target=target,
                    cancel_check=lambda: self._check_day(day),
                    # 日级 claim 防重复；尝试 ID 让提交前限流后的下一轮
                    # 不复用 DeliveryService 已记录的 NOT_SENT 回执。
                    source_event_ids=[f"changelog:{day}:{target.conversation_id}",
                                      f"changelog-attempt:{uuid.uuid4().hex}"],
                ))
            except DeliveryBlocked:
                # 明确在提交前被门禁拒绝，可等待下轮；已有发送回执不自动重试。
                self.store.release_changelog(day, target.conversation_id)
                return
            # 未知异常保留 sending 占位，重启后同样不会重放可能提交的消息。
            self.store.finish_changelog(day, target.conversation_id, result)
            self.store.audit("changelog", target.display_name, result.status.value,
                             hashlib.sha256(message.encode("utf-8")).hexdigest(), detail=f"date={day}")
            logger.info("[WechatDesktop] changelog date=%s target=%s status=%s",
                        day, target.display_name, result.status.value)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("[WechatDesktop] changelog task failed")
            self.stop_event.wait(self.config["changelog_check_interval_seconds"])
