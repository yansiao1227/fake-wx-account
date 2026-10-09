"""Agent 回复协调：上下文构造、异步投递、工具事件和进度通知。"""

from __future__ import annotations
import re
import threading
from pathlib import Path
from bridge.context import Context, ContextType
from channel.wechat_desktop.config import DEFAULT_CONFIG
from channel.wechat_desktop.pipeline.prompts import (
    DEFAULT_BOT_MENTION_ALIASES,
    _format_agent_notice,
    _is_user_visible_tool_notice,
    _link_reading_instruction,
    _preflight_tool_notice_data,
    _render_event_context_lines,
    _reply_requirements,
    _strip_group_bot_mentions,
    _tool_notice_subject,
)
from channel.wechat_desktop.pipeline.fifo_queue import ReplyQueueItem
from channel.wechat_desktop.models import WechatDesktopEvent, WechatDesktopMessage
from common.log import logger
from plugins import Event, EventContext, PluginManager


class AgentReplyCoordinator:
    """通过通道接入 Agent 桥与发送门禁；每轮回调的去重状态留在闭包中。"""

    def __init__(self, channel):
        self.channel = channel

    def wait_for_reply(self, item: ReplyQueueItem) -> str:
        """等待回调；超时先失效队列令牌，再取消 Agent 请求，拒绝迟到发送。"""
        channel = self.channel
        timeout = max(
            1.0,
            float(channel.config.get("reply_cycle_timeout_seconds", 180)),
        )
        if item.done.wait(timeout):
            return item.terminal or "completed"
        else:
            channel._reply_queue.expire(item.token)
            try:
                from agent.protocol import get_cancel_registry

                get_cancel_registry().cancel_request(item.event.event_id)
            except Exception as exc:
                logger.warning(
                    "[WechatDesktop] failed to cancel timed-out event %s: %s",
                    item.event.event_id,
                    exc,
                )
        return "timeout"

    def send_attachment_notice(self, item: ReplyQueueItem) -> bool:
        """引用附件解析前尽早发送工具进度通知，降低用户等待的不确定感。"""
        channel = self.channel
        event = item.event
        notice_data = _preflight_tool_notice_data(event)
        if not notice_data or not bool(
            channel.config.get("agent_tool_notice_enabled", True)
        ):
            return False
        context = {
            "msg": WechatDesktopMessage(event),
            "receiver": event.conversation_id or event.conversation_name,
            "isgroup": event.is_group,
            "wechat_desktop_queue_token": item.token,
            "wechat_desktop_source_type": event.source_type,
        }
        try:
            return channel._send_agent_tool_notice(context, notice_data)
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] early attachment notice failed: %s", exc
            )
            return False

    def build_image_prompt(self, event: WechatDesktopEvent) -> str:
        """把本地图片路径转换为明确要求调用 vision 的文字提示。"""
        channel = self.channel
        if not bool(channel.config.get("analyze_incoming_images", True)):
            return "[收到一张图片，当前 AI 仅处理文字，请人工查看]"
        if not event.content or not Path(event.content).is_file():
            return "[收到一张图片，但无法提取图像区域]"
        return (
            f"[需要回复的微信图片已保存到本地: {event.content}]\n"
            "请使用 vision 工具读取这张图片，并先筛选图片附近会话历史中与其高度相关的内容，"
            "再判断发送者的意图并自然回复。"
            "不要只做机械的图片描述；如果发送者是在提问、确认或延续前文，应直接回应其真实意图。"
        )

    def dispatch(self, event: WechatDesktopEvent, queue_token: str = "") -> bool:
        """构造 Agent 上下文并启动一次异步回复周期。

        引用消息只包含被引用内容和当前消息；普通消息携带入队时的候选上文并要求
        Agent 做相关性筛选。图片和文件仍以文本上下文投递，但附加必须调用对应工具
        的指令。返回 ``True`` 表示已成功交给 Agent，最终发送状态由回调完成。
        """
        channel = self.channel
        if (event.attachment_status == "source_invalid"
                or str((event.reference or {}).get("fetch_status") or "").lower() == "source_invalid"):
            # 来源失效的任务不交给 Agent，避免其依据旧 URL 发起工具读取。
            return False
        if event.source_message_id:
            validation = channel._driver.validate_reply_target(event)
            if not validation.valid:
                return False
        invalid_context_ids = set()
        for source in event.task.context_source_events:
            if not channel._driver.validate_reply_target(source).valid:
                invalid_context_ids.update(
                    identity for identity in (source.message_stable_id, source.source_message_id) if identity
                )
        if invalid_context_ids:
            # FIFO 恢复的是入队时历史；实际投递前再剔除等待期间变化的前序来源。
            event.history = [
                item for item in event.history
                if not any(str(item.get(key) or "") in invalid_context_ids
                           for key in ("_message_stable_id", "source_message_id"))
            ]
        msg = WechatDesktopMessage(event)
        ctype = msg.ctype
        content = msg.content
        # 保存原消息供短期会话与重启恢复使用，不重复保存整段注入提示词。
        user_message = str(event.content or "")
        if event.content_type in {"image", "file"}:
            user_message = f"[{'图片' if event.content_type == 'image' else '文件'}: {event.content}]"
        if event.is_group:
            user_message = _strip_group_bot_mentions(
                user_message, [*DEFAULT_BOT_MENTION_ALIASES, channel.config.get("self_display_name", "")]
            )
            user_message = f"{event.sender_name or '群成员'}: {user_message}"
        attachment_instruction = ""
        if ctype == ContextType.IMAGE:
            ctype = ContextType.TEXT
            content = channel._image_agent_prompt(event)
            if (
                bool(channel.config.get("analyze_incoming_images", True))
                and event.content
                and Path(event.content).is_file()
            ):
                attachment_instruction = (
                    "当前消息是一张图片，必须先使用 vision 工具读取图片，"
                    "再结合筛选出的高相关上下文回复。"
                )
        elif ctype == ContextType.FILE:
            ctype = ContextType.TEXT
            content = f"[微信文件已保存到本地: {event.content}]"
            attachment_instruction = (
                "当前消息是一个文件，请读取该文件，并结合筛选出的高相关上下文自然回复发送者。"
            )
        if event.reference:
            reference_type = str(event.reference.get("content_type") or "text")
            reference_path = str(event.reference.get("file_path") or "")
            if reference_type == "image" and reference_path:
                attachment_instruction = (
                    f"被引用的原图片已保存到本地: {reference_path}。"
                    "必须先使用 vision 工具读取这张大图，再回答当前消息。"
                )
            elif reference_type == "file" and reference_path:
                attachment_instruction = (
                    f"被引用的原文件已保存到本地: {reference_path}。"
                    "必须先根据文件扩展名调用适配的工具或技能读取该文件，"
                    "再结合当前消息回答。"
                )
            elif reference_type == "image":
                attachment_instruction = (
                    "被引用的图片未能从微信查看器中安全截取。"
                    "不要猜测图片内容，直接说明目前无法读取该图片。"
                )
            elif reference_type == "file":
                attachment_instruction = (
                    "被引用的文件未能从微信缓存中取得。"
                    "不要猜测文件内容，直接说明目前无法读取该文件。"
                )
        if ctype == ContextType.TEXT:
            history_heading, history_lines = _render_event_context_lines(event, channel.config)
            channel._trace(
                "09-context",
                "id=%s conversation=%s history_available=%s history_used=%s history_chars=%s history_source=%s",
                event.event_id[:10],
                event.conversation_name,
                len(event.history),
                len(history_lines),
                len("\n".join(history_lines)),
                "wechat_database" if event.source_message_id else "visible",
            )
            sections = []
            if history_lines:
                sections.extend(
                    [
                        history_heading,
                        "\n".join(history_lines),
                    ]
                )
            sections.extend(
                [
                    (
                        "[需要回复的引用消息]"
                        if event.reference
                        else "[需要回复的新消息]"
                    ),
                    (
                        f"{event.sender_name or '群成员'}: {content}"
                        if event.is_group
                        else str(content)
                    ),
                    "[回复要求]",
                    _reply_requirements(event),
                ]
            )
            if attachment_instruction:
                sections.append(attachment_instruction)
            link_instruction = _link_reading_instruction(event)
            if link_instruction:
                sections.append(link_instruction)
            content = "\n".join(sections)
            if event.is_group:
                content = _strip_group_bot_mentions(
                    content,
                    [
                        *DEFAULT_BOT_MENTION_ALIASES,
                        channel.config.get("self_display_name", ""),
                    ],
                )
        context = channel._compose_context(
            ctype,
            content,
            isgroup=event.is_group,
            msg=msg,
            no_need_at=True,
            wechat_desktop_evidence=event.evidence_path,
            wechat_desktop_source_type=event.source_type,
            wechat_desktop_auto_reply=True,
            wechat_desktop_user_message=user_message,
            wechat_desktop_is_reference=bool(event.reference),
            wechat_desktop_input_artifact_paths=list(dict.fromkeys(
                str(path) for path in (
                    event.content if event.content_type in {"image", "file"} else "",
                    (event.reference or {}).get("file_path", ""),
                ) if path
            )),
            wechat_desktop_session_max_turns=channel.config.get(
                "reply_session_max_turns", DEFAULT_CONFIG["reply_session_max_turns"]
            ),
            wechat_desktop_session_max_chars=channel.config.get(
                "reply_session_max_chars", DEFAULT_CONFIG["reply_session_max_chars"]
            ),
        )
        if context:
            channel._trace(
                "09-produce",
                "id=%s session=%s context_type=%s",
                event.event_id[:10],
                context.get("session_id"),
                context.type,
            )
            context["wechat_desktop_queue_token"] = queue_token
            context["wechat_desktop_queue_terminal"] = "completed"
            context["wechat_desktop_source_event_ids"] = list(
                event.task.source_event_ids or [event.event_id]
            )
            context["wechat_desktop_batch_id"] = str(
                event.task.batch_id or event.event_id
            )
            context["wechat_desktop_agent_notice_sent"] = bool(
                event.task.preflight_attachment_notice_sent
            )
            # 附件物化阶段的进度通知可以提前发；Agent 侧等到真正开始
            # 调用用户可理解的工具后再发，避免猜错下一步工具。
            if bool(channel.config.get("agent_preflight_notice_enabled", False)):
                notice_data = _preflight_tool_notice_data(event)
                if (
                    notice_data
                    and not context["wechat_desktop_agent_notice_sent"]
                    and bool(channel.config.get("agent_tool_notice_enabled", True))
                    and channel._is_user_visible_tool_notice(notice_data)
                ):
                    try:
                        context["wechat_desktop_agent_notice_sent"] = (
                            channel._send_agent_tool_notice(context, notice_data)
                        )
                    except Exception as exc:
                        logger.warning(
                            "[WechatDesktop] Agent preflight notice failed: %s",
                            exc,
                        )
            context["on_event"] = channel._make_agent_event_callback(context)
            channel.produce(context)
            return True
        channel._trace(
            "09-dropped",
            "id=%s reason=context_filtered_by_plugin_or_empty",
            event.event_id[:10],
        )
        return False

    def make_event_callback(self, context: Context):
        """包装 Agent 流事件，记录首次工具调用并按配置发送一次进度通知。"""
        channel = self.channel
        downstream = context.get("on_event")
        lock = threading.Lock()
        seen_tool_calls: set[str] = set()
        notice_sent = bool(context.get("wechat_desktop_agent_notice_sent", False))

        def on_event(event: dict):
            """处理通道关心的流事件后，保持原回调链继续向下传递。"""
            nonlocal notice_sent
            try:
                if event.get("type") == "tool_execution_start":
                    channel._mark_lifecycle(
                        context.get("wechat_desktop_source_event_ids", []),
                        "first_tool",
                    )
                if (
                    event.get("type") == "tool_execution_start"
                    and bool(channel.config.get("agent_tool_notice_enabled", True))
                ):
                    data = event.get("data", {})
                    if channel._is_user_visible_tool_notice(data):
                        tool_call_id = str(
                            data.get("tool_call_id") or data.get("tool_name") or "tool"
                        )
                        with lock:
                            once = bool(
                                channel.config.get("agent_tool_notice_once_per_reply", True)
                            )
                            if tool_call_id not in seen_tool_calls and not (
                                once and notice_sent
                            ):
                                seen_tool_calls.add(tool_call_id)
                                notice_sent = channel._send_agent_tool_notice(
                                    context, data
                                ) or notice_sent
            except Exception as exc:
                logger.warning(
                    "[WechatDesktop] Agent tool notice failed: %s", exc
                )
            finally:
                if downstream:
                    downstream(event)

        return on_event

    def is_visible_notice(self, data: dict) -> bool:
        """通道配置下，这次工具调用是否应向微信用户发进度通知。"""
        channel = self.channel
        return _is_user_visible_tool_notice(
            data, channel.config.get("agent_tool_notice_silent_tools")
        )

    def send_tool_notice(self, context: Context, data: dict) -> bool:
        """向当前微信会话发送工具/技能进度，并登记为不参与回复识别的临时文本。

        发送前再次检查队列令牌、暂停状态、影子模式、白名单和底层工具过滤，
        防止过期 Agent 周期、只观察模式或 bash/read 一类内部动作产生额外消息。
        """
        channel = self.channel
        msg = context.get("msg")
        target_name = (
            getattr(msg, "other_user_nickname", "")
            or context.get("receiver", "")
        )
        target_id = (
            getattr(msg, "other_user_id", "")
            or context.get("receiver", "")
        )
        event = getattr(msg, "event", None)
        queue_token = str(context.get("wechat_desktop_queue_token") or "")
        if queue_token and not channel._reply_queue.is_active(queue_token):
            return False
        send_target = (target_id or target_name)
        is_group = bool(context.get("isgroup", False))
        if (
            bool(channel._service.status().get("paused"))
            or bool(channel.config.get("shadow_mode", True))
            or channel._policy.is_blocked(target_name)
            or not channel._policy.is_allowlisted(target_name, is_group)
        ):
            return False
        if not channel._is_user_visible_tool_notice(data):
            return False

        kind, name = _tool_notice_subject(data)
        name = re.sub(r"[\r\n`]+", " ", name).strip()[:80] or kind
        template_key = str(data.get("notice_template_key") or "").strip()
        if not template_key:
            template_key = (
                "agent_skill_notice_templates"
                if kind == "skill"
                else "agent_tool_notice_templates"
            )
        templates = [
            str(item)
            for item in channel.config.get(template_key, [])
            if str(item).strip()
        ]
        notice = _format_agent_notice(templates, kind, name)

        result = channel._deliver(send_target, notice, policy_target=target_name,
                               is_group=is_group, interim=True, token=queue_token,
                               source_event_ids=context.get("wechat_desktop_source_event_ids", []))
        if not result.get("success"):
            raise RuntimeError(str(result.get("message") or "send failed"))

        channel._store.append_conversation_history(
            conversation_id=target_id,
            conversation_name=target_name,
            sender_name=str(channel.config.get("self_display_name") or "我"),
            direction="outgoing",
            content_type="text",
            content=notice,
            source_type=str(
                context.get("wechat_desktop_source_type", "unknown")
            ),
        )
        channel._store.audit(
            "agent_tool_notice",
            target_name,
            "success" if result.get("verified") else "unverified",
            channel._content_hash(notice),
            detail=f"{kind}={name}",
        )
        channel._trace(
            "10-tool-notice",
            "target=%s kind=%s name=%s verified=%s",
            target_name,
            kind,
            name,
            bool(result.get("verified")),
        )
        return True

    def compose_context(self, ctype: ContextType, content, **kwargs):
        """创建 CowAgent Context，并允许插件在投递前修改或拦截。

        微信桌面通道已经在自身策略层完成准入判断，因此这里不依赖其他 Channel 的
        全局白名单；图片、语音等最终也会被规范为 Agent 可消费的文本上下文。
        """
        channel = self.channel
        context = Context(ctype, content)
        context.kwargs = kwargs
        context["channel_type"] = "wechat_desktop"
        context["origin_ctype"] = ctype
        msg = context["msg"]
        context["session_id"] = msg.other_user_id
        context["receiver"] = msg.other_user_id

        event_context = PluginManager().emit_event(
            EventContext(
                Event.ON_RECEIVE_MESSAGE,
                {"channel": channel, "context": context},
            )
        )
        context = event_context["context"]
        if event_context.is_pass() or context is None:
            return context

        if ctype == ContextType.TEXT:
            text = str(content or "").strip()
            if kwargs.get("isgroup"):
                prefixes = channel.config.get("group_command_prefixes", ["/cow"])
                for prefix in prefixes:
                    if prefix and text.startswith(prefix):
                        text = text[len(prefix):].lstrip()
                        break
            context.type = ContextType.TEXT
            context.content = text
        return context
