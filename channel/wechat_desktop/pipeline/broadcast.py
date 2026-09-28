"""广播编排：目标展开、持久化任务认领、预写文案发送与结果收尾。"""

from __future__ import annotations
import hashlib
from datetime import datetime
from channel.wechat_desktop.pipeline.fifo_queue import ReplyQueueItem
from channel.wechat_desktop.pipeline.delivery import DeliveryBlocked
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.contracts import SendResult
from common.log import logger


class BroadcastCoordinator:
    """复用注入通道的队列、策略和 DeliveryService，不另建发送通路。"""

    def __init__(self, channel):
        self.channel = channel

    def resolve_target(self, group_name: str) -> tuple[str, str]:
        """仅使用公开目标解析契约；歧义和失效身份由后端明确报告。"""
        channel = self.channel
        resolution = channel._driver.resolve_target(str(group_name or "").strip())
        if resolution.target is None:
            raise DeliveryBlocked(resolution.reason)
        return resolution.target.conversation_id, resolution.target.display_name

    def enqueue(self, message: str, fire_date: str = "") -> int:
        """把已准备好的每日热点文案按目标群拆成预写发送任务并入全局 FIFO。

        目标仅来自 ``daily_hot_broadcast_groups``（不是当前打开的会话，也不走
        ``auto_reply_groups`` / ``auto_reply_groups_all``）。返回成功入队的群数量。
        """
        channel = self.channel
        text = str(message or "").strip()
        if not text:
            return 0
        if bool(channel.config.get("shadow_mode", True)):
            logger.info(
                "[WechatDesktop][DailyHot] skip enqueue because shadow_mode is on"
            )
            return 0
        if bool(channel._service.status().get("paused")):
            logger.info("[WechatDesktop][DailyHot] skip enqueue because paused")
            return 0

        # 仅向 daily_hot_broadcast_groups 广播，不使用自动回复白名单。
        groups = [
            str(name).strip()
            for name in channel.config.get("daily_hot_broadcast_groups", []) or []
            if str(name).strip()
        ]
        if not groups:
            logger.warning(
                "[WechatDesktop][DailyHot] no targets: daily_hot_broadcast_groups is empty"
            )
        else:
            logger.info(
                "[WechatDesktop][DailyHot] enqueue targets from daily_hot_broadcast_groups=%s",
                groups,
            )
        fire_date = fire_date or datetime.now().strftime("%Y-%m-%d")
        queued = 0
        rejected = False
        for group_name in dict.fromkeys(groups):
            if channel._policy.is_blocked(group_name):
                channel._store.audit(
                    "daily_hot_broadcast",
                    group_name,
                    "blocked",
                    channel._content_hash(text),
                    detail="blacklist",
                )
                continue
            if not channel._policy.is_daily_hot_target(group_name):
                continue
            conversation_id, conversation_name = channel._resolve_daily_hot_target(
                group_name
            )
            job = channel._store.ensure_broadcast_job(fire_date, group_name, text)
            if job["status"] not in {"pending", "queued"}:
                continue
            event_id = hashlib.sha256(f"daily_hot:{fire_date}:{group_name}".encode()).hexdigest()
            if channel._reply_queue.contains(event_id):
                queued += 1
                continue
            event = WechatDesktopEvent(
                event_id=event_id,
                kind="proactive_send",
                conversation_id=conversation_id,
                conversation_name=conversation_name,
                sender_id="system",
                sender_name="daily_hot_broadcast",
                content_type="text",
                content=text,
                direction="outgoing",
                is_group=True,
                source_type="group",
            )
            event.task.broadcast_date = fire_date
            event.task.broadcast_target = group_name
            event.task.fingerprint_content = f"{fire_date}:{group_name}:{job['message']}"
            event.content = job["message"]
            event.task.proactive_send = True
            event.task.precomposed_reply_text = job['message']
            event.task.source_event_ids = [event.event_id]
            event.task.batch_id = event.event_id
            channel._start_lifecycle(event)
            channel._store.record_event(event)
            if not channel._store.set_broadcast_status(
                fire_date, group_name, "queued", expected=("pending", "queued")
            ):
                channel._finish_lifecycle([event_id], "duplicate")
                continue
            if not channel._enqueue_reply_event(event):
                channel._store.set_broadcast_status(fire_date, group_name, "pending", expected=("queued",))
                rejected = True
                continue
            queued += 1
            channel._store.audit(
                "daily_hot_broadcast",
                group_name,
                "queued",
                channel._content_hash(text),
                detail=f"event_id={event.event_id}",
            )
        if rejected:
            raise RuntimeError("daily hot queue full or stopped; retry remaining targets")
        if queued == 0:
            channel._store.audit(
                "daily_hot_broadcast",
                "",
                "no_targets",
                channel._content_hash(text),
                detail="no allowlisted groups",
            )
        scheduler = getattr(channel, "_daily_hot_scheduler", None)
        if scheduler is not None:
            channel._service.update_status(**scheduler.status())
        return queued

    def send(self, item: ReplyQueueItem) -> str:
        """发送入队前已准备好的文案（如每日热点），不调用 Agent。"""
        channel = self.channel
        event = item.event
        text = str(
            event.task.precomposed_reply_text or event.content or ""
        ).strip()
        target_name = event.conversation_name
        target_id = event.conversation_id
        if not text:
            return "skipped"
        if bool(channel._service.status().get("paused")) or not channel._policy.allows_send(
            target_name, True, "text", daily_hot=True
        ):
            channel._store.audit(
                "daily_hot_broadcast",
                target_name,
                "skipped",
                channel._content_hash(text),
                detail="paused or policy blocked",
            )
            return "skipped"

        send_target = target_id or target_name
        logger.info(
            "[WechatDesktop][DailyHot] precomposed send target_name=%s "
            "conversation_id=%s send_target=%s",
            target_name,
            target_id,
            send_target,
        )
        channel._driver.begin_reply_cycle(target_name, target_id)
        channel._mark_lifecycle(item.source_event_ids, "send_started")
        claimed = False

        def claim_send():
            nonlocal claimed
            channel._claim_broadcast_send(event)
            claimed = True

        try:
            result = channel._deliver(send_target, text, policy_target=target_name,
                                   is_group=True, daily_hot=True, token=item.token,
                                   source_event_ids=item.source_event_ids,
                                   before_send=claim_send)
        except DeliveryBlocked:
            if claimed:
                # 尚未提交任何气泡，可安全交回调度器；部分发送返回结构化结果。
                channel._set_broadcast_status(event, "pending")
            return "skipped"
        except Exception as exc:
            channel._set_broadcast_status(event, "uncertain")
            channel._mark_lifecycle(
                item.source_event_ids,
                "send_started",
                send_result="failed",
            )
            logger.warning(
                "[WechatDesktop] precomposed send failed target=%s: %s",
                target_name,
                exc,
            )
            channel._store.audit(
                "send_text",
                target_name,
                "failed",
                channel._content_hash(text),
                detail=f"precomposed:{exc}",
            )
            return "failed"
        if not result.get("success"):
            outcome = SendResult.from_backend(result).terminal
            channel._set_broadcast_status(event, outcome)
            channel._mark_lifecycle(
                item.source_event_ids,
                "send_started",
                send_result="failed",
            )
            channel._store.audit(
                "send_text",
                target_name,
                "failed",
                channel._content_hash(text),
                detail="precomposed send unsuccessful",
            )
            return outcome

        verified = bool(result.get("verified"))
        channel._set_broadcast_status(event, "completed" if verified else "unverified")
        channel._mark_lifecycle(
            item.source_event_ids,
            "send_verified" if verified else "send_started",
            send_result="verified" if verified else "unverified",
        )
        channel._store.audit(
            "send_text",
            target_name,
            "success" if verified else "unverified",
            channel._content_hash(text),
            detail="daily_hot_broadcast",
        )
        channel._store.append_conversation_history(
            conversation_id=target_id,
            conversation_name=target_name,
            sender_name=str(channel.config.get("self_display_name") or "我"),
            direction="outgoing",
            content_type="text",
            content=text,
            source_type=event.source_type or "group",
        )
        channel._trace(
            "12-precomposed-send",
            "target=%s chars=%s verified=%s",
            target_name,
            len(text),
            verified,
        )
        return SendResult.from_backend(result).terminal

    def claim(self, event):
        channel = self.channel
        if event.task.broadcast_date and not channel._store.set_broadcast_status(
            event.task.broadcast_date, event.task.broadcast_target, "sending",
            expected=("pending", "queued"),
        ):
            raise DeliveryBlocked("broadcast already started or completed")

    def set_status(self, event, status):
        channel = self.channel
        if event.task.broadcast_date:
            channel._store.set_broadcast_status(event.task.broadcast_date, event.task.broadcast_target, status)
