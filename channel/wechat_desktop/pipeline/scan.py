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

        首次扫描默认建立基线而不回复旧消息，但会按配置处理会话列表明确标记的
        未读消息。通过全部安全门的事件才会进入后续队列。
        """
        with self._lifecycle_lock:
            self._scan_count += 1
        observation, events = self._driver.observe_events()
        now = time.time()
        if observation.get("error"):
            self.login_status = "unavailable"
            self._service.update_status(
                login_status="unavailable",
                mode="unavailable",
                last_observation_at=now,
                last_error=observation["error"],
            )
            return

        self.login_status = "logged_in"
        self._service.update_status(
            login_status=self.login_status,
            mode="uia",
            uia_available=observation.get("uia_available", False),
            shell_hook_active=observation.get("shell_hook_active", False),
            owner_name=observation.get("owner_name", ""),
            owner_source=observation.get("owner_source", "unknown"),
            last_observation_at=now,
            last_error="",
            **self._reply_queue.status(),
        )

        first_poll = not self._bootstrapped
        baseline_history_exists = {}
        if first_poll:
            baseline_history_exists = {
                event.conversation_id: self._store.has_conversation_history(
                    event.conversation_id
                )
                for event in events
                if event.kind == "message"
            }
        for event in events:
            self._start_lifecycle(event)
            recorded = self._store.record_event(event)
            self._trace(
                "07-event",
                "id=%s recorded=%s kind=%s conversation=%s sender=%s source=%s group=%s at=%s type=%s chars=%s",
                event.event_id[:10],
                recorded,
                event.kind,
                event.conversation_name,
                event.sender_name,
                event.source_type,
                event.is_group,
                event.is_at,
                event.content_type,
                len(str(event.content or "")),
            )
            if not recorded:
                # 事件账本指纹是最终去重门。UIA 快照可能在私聊回复尚未完成时再次
                # 发出；绝不能把重复事件当成新入站消息，否则会触发自我回复循环。
                # 私聊和群聊同样适用。
                self._store.mark_event_processed(event.event_id)
                self._finish_lifecycle([event.event_id], "duplicate")
                self._trace(
                    "08-skip",
                    "id=%s reason=duplicate_event_fingerprint",
                    event.event_id[:10],
                )
                continue
            if event.kind == "message" and not (
                first_poll
                and baseline_history_exists.get(event.conversation_id, False)
            ):
                self._store.append_event_history(event)
            if event.kind != "message":
                self._trace(
                    "08-request",
                    "id=%s kind=%s ignored_no_approval_flow",
                    event.event_id[:10],
                    event.kind,
                )
                self._store.mark_event_processed(event.event_id)
                self._finish_lifecycle([event.event_id], "skipped")
                continue
            process_startup_unread = bool(
                self.config.get("process_startup_unread_messages", True)
            )
            startup_unread_event = bool(
                process_startup_unread and event.session_unread_count > 0
            )
            if (
                first_poll
                and not bool(
                    self.config.get("bootstrap_existing_messages", False)
                )
                and not startup_unread_event
            ):
                self._trace(
                    "08-skip",
                    "id=%s reason=initial_baseline",
                    event.event_id[:10],
                )
                self._store.mark_event_processed(event.event_id)
                self._finish_lifecycle([event.event_id], "skipped")
                continue
            if (
                self._policy.is_blocked(event.conversation_name)
                or self._policy.is_blocked(event.sender_name)
            ):
                logger.info(
                    "[WechatDesktop] ignored blacklisted sender/conversation: %s / %s",
                    event.sender_name,
                    event.conversation_name,
                )
                self._trace(
                    "08-skip",
                    "id=%s reason=blacklist conversation=%s sender=%s",
                    event.event_id[:10],
                    event.conversation_name,
                    event.sender_name,
                )
                self._store.mark_event_processed(event.event_id)
                self._finish_lifecycle([event.event_id], "skipped")
                continue
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
                continue
            if event.is_group and not self._policy.group_triggered(event):
                self._trace(
                    "08-skip",
                    "id=%s reason=group_without_at conversation=%s",
                    event.event_id[:10],
                    event.conversation_name,
                )
                self._store.mark_event_processed(event.event_id)
                self._finish_lifecycle([event.event_id], "skipped")
                continue
            self._trace(
                "09-dispatch",
                "id=%s conversation=%s source=%s",
                event.event_id[:10],
                event.conversation_name,
                event.source_type,
            )
            self._route_reply_event(event)
        self._bootstrapped = True

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
        self._store.mark_event_processed(event.event_id)
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
        self._store.mark_event_processed(event.event_id)
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

    def _accept_replacement_event(self, event: WechatDesktopEvent):
        """发送前发现目标已更新时，用相同安全门重新接纳替代事件。"""
        self._start_lifecycle(event)
        recorded = self._store.record_event(event)
        if recorded:
            self._store.append_event_history(event)
        if event.kind != "message" or event.source_type not in {"private", "group"}:
            self._store.mark_event_processed(event.event_id)
            return
        if (
            self._policy.is_blocked(event.conversation_name)
            or self._policy.is_blocked(event.sender_name)
            or (event.is_group and not self._policy.group_triggered(event))
        ):
            self._store.mark_event_processed(event.event_id)
            return
        self._route_reply_event(event)
