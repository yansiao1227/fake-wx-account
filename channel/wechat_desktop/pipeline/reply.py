"""FIFO 任务准备、附件提示和收尾；Agent 细节委托给回复协调器。"""

from __future__ import annotations
from channel.wechat_desktop.pipeline.agent_reply import AgentReplyCoordinator
from bridge.context import Context, ContextType
from channel.wechat_desktop.pipeline.prompts import ATTACHMENT_REFERENCE_REQUIRED_REPLY
from channel.wechat_desktop.pipeline.fifo_queue import ReplyQueueItem
from channel.wechat_desktop.pipeline.worker import ReplyWorker, best_effort
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.contracts import SendResult
from common.log import logger


class WechatDesktopReplyMixin:
    """串行回复流程及通道兼容入口，不持有协调器的业务实现。"""

    def _send_attachment_reference_prompt(self, item: ReplyQueueItem) -> str:
        """拒绝猜测未明确指向的附件，并直接发送“请引用后再问”的提示。"""
        event = item.event
        target_name = event.conversation_name
        target_id = event.conversation_id
        if bool(self._service.status().get("paused")) or not self._policy.allows_send(
            target_name,
            event.is_group,
            "text",
        ):
            return "skipped"
        if not self._validate_source_before_reply(
            event, None, target_name, ATTACHMENT_REFERENCE_REQUIRED_REPLY, "text"
        ):
            return "skipped"
        send_target = (target_id or target_name)
        self._mark_lifecycle(item.source_event_ids, "send_started")
        try:
            result = self._deliver(
                send_target, ATTACHMENT_REFERENCE_REQUIRED_REPLY,
                policy_target=target_name, is_group=event.is_group, token=item.token, source_event_ids=item.source_event_ids,
            )
        except Exception as exc:
            self._mark_lifecycle(
                item.source_event_ids,
                "send_started",
                send_result="failed",
            )
            logger.warning(
                "[WechatDesktop] attachment reference prompt failed: %s",
                exc,
            )
            return "failed"
        if not result.get("success"):
            self._mark_lifecycle(
                item.source_event_ids,
                "send_started",
                send_result="failed",
            )
            return "failed"
        verified = bool(result.get("verified"))
        self._mark_lifecycle(
            item.source_event_ids,
            "send_verified" if verified else "send_started",
            send_result="verified" if verified else "unverified",
        )
        self._store.audit(
            "send_text",
            target_name,
            "success" if verified else "unverified",
            self._content_hash(ATTACHMENT_REFERENCE_REQUIRED_REPLY),
            detail="attachment reference required",
        )
        self._store.append_conversation_history(
            conversation_id=target_id,
            conversation_name=target_name,
            sender_name=str(self.config.get("self_display_name") or "我"),
            direction="outgoing",
            content_type="text",
            content=ATTACHMENT_REFERENCE_REQUIRED_REPLY,
            source_type=event.source_type,
        )
        return SendResult.from_backend(result).terminal

    def _send_deferred_attachment_notice(self, item: ReplyQueueItem) -> bool:
        return AgentReplyCoordinator(self).send_attachment_notice(item)

    _best_effort = staticmethod(best_effort)

    def _consume_reply_queue(self):
        ReplyWorker(
            queue=self._reply_queue,
            stop_event=self._stop_event,
            process=self._process_reply_item,
            on_error=self._on_reply_worker_error,
            on_finish=self._on_reply_worker_finish,
        ).run()

    def _on_reply_worker_error(self, item, exc):
        self._best_effort("worker_error", self._service.update_status, last_error=str(exc))

    def _on_reply_worker_finish(self, item, terminal):
        # 也覆盖尚未进入 Agent 的过期任务和准备阶段异常。
        for event_id in item.source_event_ids:
            self._best_effort("mark_event_processed", self._store.mark_event_processed, event_id, terminal)
        self._best_effort("finish_lifecycle", self._finish_lifecycle, item.source_event_ids, terminal)
        status = self._reply_queue.status()
        self._service.update_status(reply_in_flight=False, reply_conversation="", **status)

    def _process_reply_item(self, item: ReplyQueueItem) -> str:
        self._store.set_event_state(item.source_event_ids, "running")
        self._service.update_status(
            mode="replying",
            reply_in_flight=True,
            reply_conversation=item.event.conversation_name,
            **self._reply_queue.status(),
        )
        terminal = "failed"
        deferred_failed = False
        deferred_events = list(
            item.event.task.deferred_materialization_events
            or []
        )
        if deferred_events and not item.event.task.source_invalid:
            try:
                # 附件组件在实际 UI 操作段取得租约，避免与发送争抢窗口。
                # 分享卡片打开内置浏览器时不发进度提示：发送会和浏览器
                # 小窗抢同一个微信窗口，实际经常发不出去。
                notice_sent = self._send_deferred_attachment_notice(item)

                if not item.event.task.source_invalid:
                    materialized = self._materialize_batch(deferred_events)
                    materialized.task.preflight_attachment_notice_sent = notice_sent
                    item.event = materialized
            except Exception:
                deferred_failed = True
                logger.exception(
                    "[WechatDesktop] deferred attachment materialization failed"
                )
        # 任务在 FIFO 中等待时界面可能已经滑走，始终使用入队时固化的会话上下文回复。
        item.restore_context()
        reference_required = bool(
            item.event.task.attachment_reference_required
        )
        if not reference_required:
            self._mark_lifecycle(
                item.source_event_ids,
                "agent_started",
                batch_id=item.batch_id,
            )
        try:
            if item.event.task.source_invalid:
                terminal = "skipped"
            elif deferred_failed:
                terminal = "failed"
            elif item.expired:
                terminal = item.terminal or "skipped"
            elif reference_required:
                terminal = self._send_attachment_reference_prompt(item)
            elif not self._dispatch_message(item.event, item.token):
                terminal = item.terminal or "skipped"
            else:
                terminal = AgentReplyCoordinator(self).wait_for_reply(item)
        except Exception as exc:
            terminal = "failed"
            logger.error(
                "[WechatDesktop] queued message failed: %s", exc, exc_info=True
            )
        finally:
            if item.event.task.source_invalid:
                terminal = "skipped"
            terminal = self._store.delivery_outcome(item.source_event_ids, terminal)
            if terminal in {"failed", "timeout"}:
                try:
                    self._send_agent_failure_notice(
                        event=item.event,
                        reason=(
                            "reply_timeout"
                            if terminal == "timeout"
                            else "reply_failed"
                        ),
                        require_active=terminal != "timeout",
                    )
                except Exception as notice_exc:
                    logger.warning(
                        "[WechatDesktop] final failure notice crashed: %s",
                        notice_exc,
                    )
                if item.event.task.source_invalid:
                    terminal = "skipped"
            if not reference_required:
                self._mark_lifecycle(item.source_event_ids, "agent_done")
            self._best_effort(
                "reply_audit", self._store.audit, "reply_queue",
                item.event.conversation_name,
                terminal,
                detail=f"event_id={item.event.event_id}",
            )
        return terminal

    def _image_agent_prompt(self, event: WechatDesktopEvent) -> str:
        return AgentReplyCoordinator(self).build_image_prompt(event)

    def _dispatch_message(self, event: WechatDesktopEvent, queue_token: str = "") -> bool:
        return AgentReplyCoordinator(self).dispatch(event, queue_token)

    def _make_agent_event_callback(self, context: Context):
        return AgentReplyCoordinator(self).make_event_callback(context)

    def _is_user_visible_tool_notice(self, data: dict) -> bool:
        return AgentReplyCoordinator(self).is_visible_notice(data)

    def _send_agent_tool_notice(self, context: Context, data: dict) -> bool:
        return AgentReplyCoordinator(self).send_tool_notice(context, data)

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        return AgentReplyCoordinator(self).compose_context(ctype, content, **kwargs)
