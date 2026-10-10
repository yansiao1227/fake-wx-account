"""微信桌面通道的扫描、策略过滤、私聊聚合和控制命令。"""

from __future__ import annotations

import random
import re
import time

from bridge.context import ContextType
from channel.wechat_desktop.models import WechatDesktopEvent, WechatDesktopMessage
from common.log import logger


class WechatDesktopScanMixin:
    """扫描线程：观察事件、去重、策略过滤，并路由到物化或控制命令。"""

    def _scan_loop(self):
        """等待后端变化信号并触发扫描；异常只影响本轮，不结束线程。"""
        first_observation = True
        while not self._stop_event.is_set():
            try:
                reason = "startup" if first_observation else self._driver.wait_for_changes(
                    self._stop_event
                )
                first_observation = False
                if reason != "stopped":
                    self._poll_once()
            except Exception as exc:
                logger.error(f"[WechatDesktop] poll failed: {exc}", exc_info=True)
                self._service.update_status(last_error=str(exc), login_status="error")

    def _poll_once(self):
        """执行一次观察、持久化、策略过滤和路由。

        数据库来源固定启动基线与未读边界。来源记录、事件和游标必须先整批
        提交，再进入后续队列；此处不接受界面快照作为消息来源。
        """
        self._lifecycle.advance_scan()
        observation, events = self._driver.observe_events()
        now = time.time()
        backend_status = {
            key: value for key, value in observation.items()
            if key.startswith("db_read_") or key in {"account_binding", "message_source", "target_identity_verification"}
        }
        if observation.get("error"):
            self.login_status = "unavailable"
            self._service.update_status(
                login_status="unavailable",
                mode=observation.get("mode", "unavailable"),
                last_observation_at=now,
                last_error=observation["error"],
                **backend_status,
            )
            return

        self.login_status = "logged_in"
        self._service.update_status(
            login_status=self.login_status,
            mode=observation.get("mode", "db_uia"),
            uia_available=observation.get("uia_available", False),
            owner_name=observation.get("owner_name", ""),
            owner_source=observation.get("owner_source", "unknown"),
            last_observation_at=now,
            last_error="; ".join(item["error"] for item in observation.get("scan_errors", [])),
            scan_errors=observation.get("scan_errors", []),
            **self._reply_queue.status(),
            **backend_status,
        )

        source_batch = observation.get("source_batch")
        if source_batch is not None:
            try:
                receipts = self._store.receive_source_batch(source_batch)
            except Exception as exc:
                # 来源过滤和游标也是同一事务的一部分，失败后整批重交付。
                self._service.update_status(last_error=f"source batch receipt failed: {exc}")
                logger.exception("[WechatDesktop] source batch receipt failed")
                return
            events_by_id = {
                record.event.event_id: record.event
                for record in source_batch.records if record.event is not None
            }
            for receipt in receipts:
                if receipt.accepted:
                    self._admit_received_event(events_by_id[receipt.observed_event_id])
            self._driver.acknowledge_events([source_batch.batch_id])
            self._cleanup_after_poll(now)
            return

        if events:
            # 接收只接受数据库来源事务，禁止退回单条 UIA 事件登记。
            self._service.update_status(last_error="source_batch_required")
            return
        self._cleanup_after_poll(now)

    def _admit_received_event(self, event):
        self._start_lifecycle(event)
        try:
            self._process_received_event(event)
        except Exception as exc:
            self._store.mark_event_processed(event.event_id, "failed", "admission_failed")
            self._finish_lifecycle([event.event_id], "failed")
            logger.exception("[WechatDesktop] event admission failed")
            self._service.update_status(last_error=str(exc))

    def _cleanup_after_poll(self, now):
        if now - self._last_cleanup_at > 86400:
            self._store.cleanup(
                int(self.config.get("retention_days", 7)),
                int(
                    self.config.get(
                        "conversation_history_retention_days", 90
                    )
                ),
            )
            self._last_cleanup_at = now

    def _process_received_event(self, event):
        """持久化接收后的单事件路由；所有拒绝也必须有可查询终态。"""
        if event.kind != "message":
            self._trace(
                "08-request",
                "id=%s kind=%s ignored_no_approval_flow",
                event.event_id[:10],
                event.kind,
            )
            self._store.mark_event_processed(event.event_id)
            self._finish_lifecycle([event.event_id], "skipped")
            return
        reason = ""
        if not event.source_message_id:
            reason = "native_source_required"
        elif event.direction != "incoming":
            reason = "source_not_incoming"
        elif event.receipt_phase in {"baseline", "offline_backfill"}:
            reason = event.receipt_phase
        elif event.receipt_phase == "startup_unread" and not self.config.get("process_startup_unread_messages", True):
            reason = "startup_unread_disabled"
        if reason:
            self._store.mark_event_processed(event.event_id, "skipped", reason)
            self._finish_lifecycle([event.event_id], "skipped")
            return
        if self._policy.is_blocked(event.conversation_name, event.is_group):
            logger.info(
                "[WechatDesktop] ignored blacklisted conversation: %s (is_group=%s)",
                event.conversation_name,
                event.is_group,
            )
            self._trace(
                "08-skip",
                "id=%s reason=blacklist conversation=%s sender=%s",
                event.event_id[:10],
                event.conversation_name,
                event.sender_name,
            )
            self._store.mark_event_processed(event.event_id, "skipped", "blacklist")
            self._finish_lifecycle([event.event_id], "skipped")
            return
        if event.source_type not in {"private", "group"}:
            logger.info(
                "[WechatDesktop] ignored incoming message with source_type=%s",
                event.source_type,
            )
            self._trace(
                "08-skip",
                "id=%s reason=unsupported_source_type source=%s",
                event.event_id[:10],
                event.source_type,
            )
            self._store.mark_event_processed(event.event_id)
            self._finish_lifecycle([event.event_id], "skipped")
            return
        if event.is_group and not self._policy.group_triggered(event):
            self._trace(
                "08-skip",
                "id=%s reason=group_without_at conversation=%s",
                event.event_id[:10],
                event.conversation_name,
            )
            self._store.mark_event_processed(event.event_id)
            self._finish_lifecycle([event.event_id], "skipped")
            return
        self._trace(
            "09-dispatch",
            "id=%s conversation=%s source=%s",
            event.event_id[:10],
            event.conversation_name,
            event.source_type,
        )
        self._route_reply_event(event)

    def _route_reply_event(self, event: WechatDesktopEvent):
        """按事件形态选择最早可安全执行的下一阶段。

        控制命令直接交给 Agent；独立图片只登记身份，独立文件只做缓存；群聊不
        聚合；普通私聊进入滑动窗口。引用附件必须保留当前消息，稍后再物化。
        """
        if self._is_control_event(event):
            self._dispatch_control_event(event)
            return
        if event.content_type == "image" and not event.reference:
            self._ignore_standalone_attachment(event)
            return
        if event.content_type == "share_card" and not event.reference:
            self._ignore_standalone_attachment(event)
            return
        if event.content_type == "file" and not event.reference:
            self._submit_materialization([event])
            return
        if event.is_group:
            self._submit_materialization([event])
            return
        self._defer_private_event(event)

    def _ignore_standalone_attachment(self, event: WechatDesktopEvent):
        """登记孤立图片但不主动分析，等待后续文字明确引用或提问。"""
        self._store.mark_event_processed(event.event_id, "observed", "standalone_attachment")
        self._trace(
            "09-attachment-observed",
            "id=%s conversation=%s type=%s action=identity_only",
            event.event_id[:10],
            event.conversation_name,
            event.content_type,
        )
        self._finish_lifecycle([event.event_id], "observed")

    @staticmethod
    def _is_control_event(event: WechatDesktopEvent) -> bool:
        """识别应绕过普通回复队列的 Agent 控制命令。"""
        if event.content_type != "text":
            return False
        text = str(event.content or "").strip().lower()
        return text == "/cancel" or re.match(r"^/steer(?:\s|$)", text) is not None

    def _dispatch_control_event(self, event: WechatDesktopEvent):
        """直接投递 /cancel、/steer，不进行私聊聚合和附件物化。"""
        context = self._compose_context(
            ContextType.TEXT,
            str(event.content or ""),
            isgroup=False,
            msg=WechatDesktopMessage(event),
            no_need_at=True,
            wechat_desktop_source_type=event.source_type,
            wechat_desktop_auto_reply=True,
        )
        self._mark_lifecycle([event.event_id], "materialized")
        if context is None:
            terminal = "skipped"
        else:
            context["wechat_desktop_source_event_ids"] = [event.event_id]
            context["wechat_desktop_batch_id"] = event.event_id
            try:
                self.produce(context)
                terminal = "completed"
            except Exception:
                terminal = "failed"
                logger.exception("[WechatDesktop] control command dispatch failed")
        self._store.mark_event_processed(event.event_id, terminal, "control_command")
        self._finish_lifecycle([event.event_id], terminal)

    def _defer_private_event(self, event: WechatDesktopEvent):
        """把同一私聊会话的连续气泡合并到一个滑动时间窗口。

        每条新消息都会取消旧定时器并重新计时；事件对象已经携带观察当时的历史
        快照，因此释放批次时不需要重新打开该会话获取上文。
        """
        wait_seconds = 0.0
        release_now = None
        with self._pending_private_lock:
            previous = self._pending_private_batches.pop(
                event.conversation_id, None
            )
            if previous is None and not self._is_idle_for_private_aggregation():
                release_now = [event]
            events = list(previous[0]) if previous is not None else []
            if previous is not None:
                previous[1].cancel()
            if release_now is None:
                events.append(event)
                now = time.monotonic()
                first_event_at = previous[2] if previous is not None else now
                max_wait_seconds = max(
                    0.05,
                    float(
                        self.config.get(
                            "private_message_aggregation_max_wait_ms", 4000
                        )
                    )
                    / 1000.0,
                )
                remaining_seconds = max_wait_seconds - (now - first_event_at)
                if remaining_seconds <= 0:
                    release_now = events
                else:
                    min_wait_seconds = max(
                        0.05,
                        float(
                            self.config.get(
                                "private_message_aggregation_min_ms", 500
                            )
                        )
                        / 1000.0,
                    )
                    max_wait_window_seconds = max(
                        min_wait_seconds,
                        float(
                            self.config.get(
                                "private_message_aggregation_max_ms", 1200
                            )
                        )
                        / 1000.0,
                    )
                    quiet_seconds = random.uniform(
                        min_wait_seconds, max_wait_window_seconds
                    )
                    wait_seconds = min(quiet_seconds, remaining_seconds)
                    timer = self._private_batch_timer_factory(
                        wait_seconds,
                        self._release_private_batch,
                        args=(event.conversation_id, event.event_id),
                    )
                    timer.daemon = True
                    self._pending_private_batches[event.conversation_id] = (
                        events,
                        timer,
                        first_event_at,
                    )
                    timer.start()
        if release_now is not None:
            self._submit_materialization(release_now)
            wait_seconds = 0.0
        self._trace(
            "09-private-aggregate",
            "id=%s conversation=%s messages=%s milliseconds=%s immediate=%s",
            event.event_id[:10],
            event.conversation_name,
            len(release_now or events),
            int(wait_seconds * 1000),
            release_now is not None,
        )

    def _is_idle_for_private_aggregation(self) -> bool:
        """仅在没有进行中的回复或物化时，才开启新的私聊滑动窗口。"""
        reply_queue = getattr(self, "_reply_queue", None)
        status = reply_queue.status() if reply_queue is not None else {}
        materialize_queue = getattr(self, "_materialize_queue", None)
        materialization_active = getattr(self, "_materialization_active", None)
        return bool(
            not status.get("queue_active_event")
            and int(status.get("queue_depth", 0)) == 0
            and (materialize_queue is None or materialize_queue.empty())
            and (
                materialization_active is None
                or not materialization_active.is_set()
            )
        )

    def _release_private_batch(self, conversation_id: str, last_event_id: str):
        """定时器到期后释放仍为最新版本的私聊批次。"""
        if self._stop_event.is_set():
            return
        with self._pending_private_lock:
            pending = self._pending_private_batches.get(conversation_id)
            if (
                pending is None
                or not pending[0]
                or pending[0][-1].event_id != last_event_id
            ):
                return
            events, _, _ = self._pending_private_batches.pop(conversation_id)
            self._submit_materialization(events)

    def _clear_pending_private_batches(self) -> list[str]:
        """取消所有尚未释放的私聊定时器，返回受影响的事件 ID。"""
        with self._pending_private_lock:
            event_ids = []
            for events, timer, _ in self._pending_private_batches.values():
                timer.cancel()
                event_ids.extend(event.event_id for event in events)
            self._pending_private_batches.clear()
        return event_ids
