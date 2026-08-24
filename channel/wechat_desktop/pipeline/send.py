"""微信桌面通道的最终发送、失败通知、网络故障收尾和 Agent 动作入口。"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import asdict

from bridge.context import Context
from bridge.reply import Reply, ReplyType
from channel.wechat_desktop.pipeline.prompts import (
    _format_failure_notice,
    _is_network_reply_error,
    _normalize_auto_reply_text,
)
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.models import WechatHistoryReadError
from common.log import file_logger, logger


class WechatDesktopSendMixin:
    """发送路径：安全门、目标复核、审计，以及受限的 Agent 动作入口。"""

    @staticmethod
    def _content_hash(content) -> str:
        """生成审计用摘要，避免在审计记录里重复保存完整正文。"""
        return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()

    def send(self, reply: Reply, context: Context):
        """ChatChannel 发送入口；所有安全判断集中在 ``_send_reply_impl``。"""
        return self._send_reply_impl(reply, context)

    def _send_agent_failure_notice(
        self,
        *,
        context=None,
        event: WechatDesktopEvent | None = None,
        reason: str = "failed",
        require_active: bool = True,
    ) -> bool:
        """幂等发送最终失败提示，避免 Agent 异常或超时后用户空等。"""
        if not bool(self.config.get("agent_failure_notice_enabled", True)):
            return False
        msg = context.get("msg") if context else None
        if event is None:
            event = getattr(msg, "event", None)
        target_name = (
            getattr(event, "conversation_name", "")
            or getattr(msg, "other_user_nickname", "")
            or (context.get("receiver", "") if context else "")
        )
        target_id = (
            getattr(event, "conversation_id", "")
            or getattr(msg, "other_user_id", "")
            or (context.get("receiver", "") if context else "")
        )
        if not target_name and not target_id:
            return False
        queue_token = str(
            context.get("wechat_desktop_queue_token", "") if context else ""
        )
        conversation_id = str(
            getattr(event, "conversation_id", "") or target_id
        )
        if (
            require_active
            and queue_token
            and not self._reply_queue.is_relevant(queue_token, conversation_id)
        ):
            return False

        lock = getattr(self, "_failure_notice_lock", None)
        if lock is None:
            lock = self._failure_notice_lock = threading.RLock()
        with lock:
            if bool(
                context
                and context.get("wechat_desktop_failure_notice_sent", False)
            ) or bool(event and getattr(event, "_failure_notice_sent", False)):
                return True
            is_group = bool(
                getattr(event, "is_group", False)
                if event is not None
                else context.get("isgroup", False)
            )
            if (
                bool(self._service.status().get("paused"))
                or bool(self.config.get("shadow_mode", True))
                or self._policy.is_blocked(target_name)
                or not self._policy.is_allowlisted(target_name, is_group)
            ):
                return False
            notice = _format_failure_notice(
                self.config.get("agent_failure_notice_templates", [])
            )
            send_target = (
                target_id
                if str(target_id).startswith("uia-session:")
                else target_name
            )
            source_event_ids = list(
                context.get("wechat_desktop_source_event_ids", [])
                if context
                else getattr(event, "_source_event_ids", None)
                or ([event.event_id] if event is not None else [])
            )
            self._mark_lifecycle(source_event_ids, "send_started")
            try:
                send_interim = getattr(self._driver, "send_interim_text", None)
                result = (
                    send_interim(send_target, notice)
                    if send_interim
                    else self._driver.send_text(send_target, notice)
                )
                if not result.get("success"):
                    raise RuntimeError(str(result.get("message") or "send failed"))
            except Exception as exc:
                self._mark_lifecycle(
                    source_event_ids,
                    "send_started",
                    send_result="failure_notice_failed",
                )
                self._trace(
                    "12-failure-notice-failed",
                    "target=%s reason=%s error=%s",
                    target_name,
                    reason,
                    exc,
                )
                self._store.audit(
                    "agent_failure_notice",
                    target_name,
                    "failed",
                    self._content_hash(notice),
                    detail=f"reason={reason}; error={exc}",
                )
                return False

            if context is not None:
                context["wechat_desktop_failure_notice_sent"] = True
            if event is not None:
                setattr(event, "_failure_notice_sent", True)
            verified = bool(result.get("verified"))
            self._mark_lifecycle(
                source_event_ids,
                "send_verified" if verified else "send_started",
                send_result=(
                    "failure_notice_verified"
                    if verified
                    else "failure_notice_unverified"
                ),
            )
            self._store.append_conversation_history(
                conversation_id=target_id,
                conversation_name=target_name,
                sender_name=str(self.config.get("self_display_name") or "我"),
                direction="outgoing",
                content_type="text",
                content=notice,
                source_type=str(
                    getattr(event, "source_type", "")
                    or (
                        context.get("wechat_desktop_source_type", "unknown")
                        if context
                        else "unknown"
                    )
                ),
            )
            self._store.audit(
                "agent_failure_notice",
                target_name,
                "success" if verified else "unverified",
                self._content_hash(notice),
                detail=f"reason={reason}",
            )
            self._trace(
                "12-failure-notice-success",
                "target=%s reason=%s verified=%s",
                target_name,
                reason,
                verified,
            )
            return True

    def _finish_reply_cycle(self, context):
        """幂等释放后端回复周期，防止回调和异常路径重复解锁。"""
        if not context or not bool(
            context.get("wechat_desktop_reply_cycle", False)
        ):
            return
        context["wechat_desktop_reply_cycle"] = False
        self._driver.end_reply_cycle()

    def _success_callback(self, session_id, **kwargs):
        """Agent 成功结束时释放后端并唤醒等待中的 FIFO 消费者。"""
        context = kwargs.get("context")
        self._finish_reply_cycle(context)
        if context:
            self._mark_lifecycle(
                context.get("wechat_desktop_source_event_ids", []),
                "agent_done",
            )
            self._reply_queue.signal(
                str(context.get("wechat_desktop_queue_token") or ""),
                str(context.get("wechat_desktop_queue_terminal") or "completed"),
            )
        return super()._success_callback(session_id, **kwargs)

    def _fail_callback(self, session_id, exception, **kwargs):
        """Agent 失败时释放后端，并把当前队列项标记为失败。"""
        context = kwargs.get("context")
        self._finish_reply_cycle(context)
        if context:
            try:
                self._send_agent_failure_notice(
                    context=context,
                    reason="agent_exception",
                )
            except Exception as notice_exc:
                logger.warning(
                    "[WechatDesktop] Agent failure notice crashed: %s",
                    notice_exc,
                )
            self._mark_lifecycle(
                context.get("wechat_desktop_source_event_ids", []),
                "agent_done",
            )
            self._reply_queue.signal(
                str(context.get("wechat_desktop_queue_token") or ""),
                "failed",
            )
        return super()._fail_callback(
            session_id,
            exception,
            **kwargs,
        )

    def _send_reply_impl(self, reply: Reply, context: Context):
        """执行最终发送安全门、目标复核、微信发送和审计。

        队列令牌首先阻止超时后的迟到结果；随后检查来源、暂停状态、回复策略和
        限流。文本发送前还会让后端复核原消息目标，避免 Agent 推理期间会话变化
        导致回错人。发送成功但无法验证气泡时记为 ``unverified``，不会自动重试，
        以避免重复发送。
        """
        queue_token = str(context.get("wechat_desktop_queue_token") or "")
        source_event_ids = list(
            context.get("wechat_desktop_source_event_ids", []) or []
        )
        if queue_token and not self._reply_queue.is_active(queue_token):
            self._store.audit(
                "send_text",
                str(context.get("receiver") or ""),
                "late_result_discarded",
                detail="reply queue token is no longer active",
            )
            return
        msg = context.get("msg")
        target_name = getattr(msg, "other_user_nickname", "") or context.get("receiver", "")
        target_id = getattr(msg, "other_user_id", "") or context.get("receiver", "")
        send_target = (
            target_id
            if str(target_id).startswith("uia-session:")
            else target_name
        )
        is_group = bool(context.get("isgroup", False))
        source_type = str(
            context.get("wechat_desktop_source_type", "unknown")
        ).strip().lower()
        paused = bool(self._service.status().get("paused"))

        if source_type not in {"private", "group"}:
            context["wechat_desktop_queue_terminal"] = "skipped"
            self._trace(
                "10-reply-blocked",
                "target=%s reason=source_type source=%s",
                target_name,
                source_type,
            )
            self._store.audit(
                "send_text",
                target_name,
                "blocked_source_type",
                self._content_hash(reply.content),
                detail=f"source_type={source_type}",
            )
            return

        if reply.type == ReplyType.ERROR:
            context["wechat_desktop_queue_terminal"] = "failed"
            if _is_network_reply_error(reply.content):
                self._handle_network_reply_error(
                    reply,
                    context,
                    target_name,
                )
            else:
                logger.error(
                    "[WechatDesktop] agent reply failed target=%s error=%s",
                    target_name,
                    reply.content,
                )
                self._store.audit(
                    "agent_reply",
                    target_name,
                    "failed",
                    detail=str(reply.content or ""),
                )
                self._send_agent_failure_notice(
                    context=context,
                    reason="agent_reply_error",
                )
            return

        if reply.type == ReplyType.TEXT:
            reply_text = (
                _normalize_auto_reply_text(reply.content)
                if bool(context.get("wechat_desktop_auto_reply", False))
                else str(reply.content or "").strip()
            )
            if not reply_text:
                context["wechat_desktop_queue_terminal"] = "skipped"
                logger.warning("[WechatDesktop] skipped empty normalized reply")
                return
            can_auto = (
                not paused
                and self._policy.can_auto_send(target_name, is_group, "text")
            )
            self._trace(
                "10-reply-ready",
                "target=%s chars=%s paused=%s can_auto=%s group=%s",
                target_name,
                len(reply_text),
                paused,
                can_auto,
                is_group,
            )
            if can_auto:
                try:
                    event = getattr(msg, "event", None)
                    if event is not None:
                        validation = self._driver.validate_reply_target(event)
                        valid, reason = validation
                        if not valid:
                            if validation.replacement_event is not None:
                                self._accept_replacement_event(
                                    validation.replacement_event
                                )
                            context["wechat_desktop_queue_terminal"] = "skipped"
                            self._trace(
                                "11-send-target-invalid",
                                "target=%s reason=%s",
                                target_name,
                                reason,
                            )
                            self._store.audit(
                                "send_text",
                                target_name,
                                "stale_target",
                                self._content_hash(reply_text),
                                detail=reason,
                            )
                            return
                    self._mark_lifecycle(source_event_ids, "send_started")
                    result = self._driver.send_text(send_target, reply_text)
                    if result.get("success"):
                        verified = bool(result.get("verified"))
                        self._mark_lifecycle(
                            source_event_ids,
                            "send_verified" if verified else "send_started",
                            send_result="verified" if verified else "unverified",
                        )
                        self._trace(
                            "12-send-success" if verified else "12-send-unverified",
                            "target=%s chars=%s verified=%s",
                            target_name,
                            len(reply_text),
                            verified,
                        )
                        self._store.audit(
                            "send_text",
                            target_name,
                            "success" if verified else "unverified",
                            self._content_hash(reply_text),
                            detail=str(result.get("message", "")),
                        )
                        self._store.append_conversation_history(
                            conversation_id=target_id,
                            conversation_name=target_name,
                            sender_name=str(
                                self.config.get("self_display_name") or "我"
                            ),
                            direction="outgoing",
                            content_type="text",
                            content=reply_text,
                            source_type=source_type,
                        )
                        context["wechat_desktop_queue_terminal"] = "completed"
                        return
                    raise RuntimeError(str(result.get("message") or "send failed"))
                except Exception as exc:
                    self._mark_lifecycle(
                        source_event_ids,
                        "send_started",
                        send_result="failed",
                    )
                    logger.warning(f"[WechatDesktop] automatic send failed: {exc}")
                    self._trace(
                        "12-send-failed",
                        "target=%s error=%s",
                        target_name,
                        exc,
                    )
            context["wechat_desktop_queue_terminal"] = "failed" if can_auto else "skipped"
            self._store.audit(
                "send_text",
                target_name,
                "failed" if can_auto else "blocked",
                self._content_hash(reply_text),
                detail="direct send failed or was blocked; no approval flow is enabled",
            )
            return

        if reply.type in (ReplyType.IMAGE, ReplyType.IMAGE_URL):
            can_auto = (
                not paused
                and self._policy.can_auto_send(target_name, is_group, "image")
            )
            self._trace(
                "10-image-ready",
                "target=%s path=%s paused=%s can_auto=%s auto_send_images=%s group=%s",
                target_name,
                str(reply.content or "")[:200],
                paused,
                can_auto,
                bool(self.config.get("auto_send_images", False)),
                is_group,
            )
            if can_auto:
                try:
                    self._mark_lifecycle(source_event_ids, "send_started")
                    result = self._driver.send_image(send_target, str(reply.content))
                    if result.get("success"):
                        verified = bool(result.get("verified"))
                        self._mark_lifecycle(
                            source_event_ids,
                            "send_verified" if verified else "send_started",
                            send_result="verified" if verified else "unverified",
                        )
                        self._trace(
                            "12-image-success" if verified else "12-image-unverified",
                            "target=%s verified=%s",
                            target_name,
                            verified,
                        )
                        context["wechat_desktop_queue_terminal"] = "completed"
                        self._store.audit(
                            "send_image",
                            target_name,
                            "success" if result.get("verified") else "unverified",
                            self._content_hash(reply.content),
                            detail=str(result.get("message", "")),
                        )
                        return
                    logger.warning(
                        "[WechatDesktop] automatic image send returned unsuccessful: %s",
                        result,
                    )
                except Exception as exc:
                    logger.warning(
                        "[WechatDesktop] automatic image send failed: %s path=%s",
                        exc,
                        str(reply.content or "")[:200],
                    )
            else:
                logger.warning(
                    "[WechatDesktop] image auto-send blocked target=%s "
                    "paused=%s auto_send_images=%s shadow_mode=%s path=%s",
                    target_name,
                    paused,
                    bool(self.config.get("auto_send_images", False)),
                    bool(self.config.get("shadow_mode", True)),
                    str(reply.content or "")[:200],
                )
            context["wechat_desktop_queue_terminal"] = "failed" if can_auto else "skipped"
            self._store.audit(
                "send_image",
                target_name,
                "failed" if can_auto else "blocked",
                self._content_hash(reply.content),
                detail=(
                    "direct image send failed or was blocked; "
                    f"can_auto={can_auto} auto_send_images="
                    f"{bool(self.config.get('auto_send_images', False))} "
                    f"shadow_mode={bool(self.config.get('shadow_mode', True))}"
                ),
            )
            return
        context["wechat_desktop_queue_terminal"] = "skipped"
        logger.warning(f"[WechatDesktop] unsupported reply type: {reply.type}")

    def _handle_network_reply_error(
        self,
        reply: Reply,
        context: Context,
        target_name: str,
    ):
        """处理 Agent 网络故障：终止当前周期、清空待办并尝试通知当前用户。

        网络状态未知时继续消费会造成大量过期回复，所以这里主动清空三个阶段中
        尚未开始的任务；正在处理的当前任务由外层消费者完成收尾。
        """
        self._finish_reply_cycle(context)
        discarded_event_ids = self._reply_queue.clear_pending()
        discarded_event_ids.extend(self._clear_pending_private_batches())
        discarded_event_ids.extend(self._clear_pending_materializations())
        discarded_event_ids = list(dict.fromkeys(discarded_event_ids))
        for event_id in discarded_event_ids:
            self._store.mark_event_processed(event_id)
        self._finish_lifecycle(discarded_event_ids, "failed")
        error_detail = str(reply.content or "")
        self._trace(
            "10-network-unstable",
            "target=%s discarded=%s error=%s",
            target_name,
            len(discarded_event_ids),
            error_detail,
        )
        logger.warning(
            "[WechatDesktop] network unstable; paused reply monitoring and cleared %s pending events; target=%s error=%s",
            len(discarded_event_ids),
            target_name,
            error_detail,
        )
        self._store.audit(
            "network_unstable",
            target_name,
            "detected",
            detail=(
                f"discarded={len(discarded_event_ids)}; error={error_detail}"
            ),
        )
        self._send_agent_failure_notice(
            context=context,
            reason="network_error",
        )

    def _execute_agent_action(self, action: str, **params) -> dict:
        """供 Agent 读取当前会话历史或主动发送文字的受限入口。

        读取为只读操作，不触发会话切换或持久化；发送路径继续遵守暂停、
        黑名单、白名单和限流规则，并要求后端验证发送结果。
        """
        if action not in {"read_history", "send_text"}:
            return {
                "status": "error",
                "message": f"unsupported wechat_desktop action: {action}",
            }
        if bool(self._service.status().get("paused")):
            return {
                "status": "error",
                "message": "desktop takeover is paused",
            }

        if action == "read_history":
            max_messages = max(
                1,
                min(
                    int(self.config.get("wechat_history_max_messages", 50)),
                    200,
                ),
            )
            try:
                raw_limit = int(params.get("limit", 20))
            except (TypeError, ValueError):
                return {
                    "status": "error",
                    "code": "invalid_limit",
                    "message": "limit must be an integer",
                }
            if raw_limit < 1:
                return {
                    "status": "error",
                    "code": "invalid_limit",
                    "message": "limit must be at least 1",
                }
            try:
                result = self._driver.read_current_chat_history(
                    min(raw_limit, max_messages)
                )
                payload = asdict(result)
                return {
                    "status": "success",
                    "conversation": {
                        "title": result.conversation_title,
                        "type": result.conversation_type,
                    },
                    **{
                        key: value
                        for key, value in payload.items()
                        if key
                        not in {"conversation_title", "conversation_type"}
                    },
                }
            except WechatHistoryReadError as exc:
                return {
                    "status": "error",
                    "code": exc.code,
                    "message": str(exc),
                }
            except Exception as exc:
                file_logger.exception(
                    "[WechatDesktop][history] current conversation read failed"
                )
                return {
                    "status": "error",
                    "code": "uia_error",
                    "message": str(exc),
                }

        conversation = str(params.get("conversation", "") or "").strip()
        text = str(params.get("text", "") or "").strip()
        is_group = bool(params.get("is_group", False))
        if not conversation:
            return {"status": "error", "message": "conversation is required"}
        if not text:
            return {"status": "error", "message": "text is required"}
        if self._policy.is_blocked(conversation):
            return {
                "status": "blocked",
                "message": "conversation is in the WeChat reply blacklist",
            }

        can_auto = (
            self._policy.can_auto_send(
                conversation,
                is_group,
                "text",
            )
        )
        if not can_auto:
            return {
                "status": "blocked",
                "message": "Direct send was blocked by reply policy or rate limits.",
            }

        try:
            result = self._driver.send_text(conversation, text)
            if result.get("success") and result.get("verified"):
                self._store.audit(
                    "send_text",
                    conversation,
                    "success",
                    self._content_hash(text),
                )
                self._store.append_conversation_history(
                    conversation_id=conversation,
                    conversation_name=conversation,
                    sender_name=str(
                        self.config.get("self_display_name") or "我"
                    ),
                    direction="outgoing",
                    content_type="text",
                    content=text,
                    source_type="group" if is_group else "private",
                )
                return {
                    "status": "sent",
                    "verified": True,
                    "conversation": conversation,
                    "message": "text sent and outgoing bubble verified",
                }
            self._store.audit(
                "send_text",
                conversation,
                "unverified",
                self._content_hash(text),
            )
            return {
                "status": "unverified",
                "verified": False,
                "conversation": conversation,
                "message": (
                    "The send action completed, but the outgoing bubble could "
                    "not be verified. Do not retry automatically."
                ),
            }
        except Exception as exc:
            self._store.audit(
                "send_text",
                conversation,
                "failed",
                self._content_hash(text),
                detail=str(exc),
            )
            return {"status": "error", "message": str(exc)}
