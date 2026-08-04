"""微信桌面通道的业务编排层。

窗口查找、消息解析和实际点击由 ``desktop_backend`` 实现；本模块负责把后端事件
组织成一条可控的自动回复流水线：

1. 扫描线程发现消息并完成去重、策略过滤；
2. 私聊短时间聚合后进入附件物化队列，群聊直接进入该队列；
3. 物化线程只解析真正需要的图片/文件，并将任务写入全局回复 FIFO；
4. 回复线程串行调用 Agent，等待回调，然后在发送前再次校验会话目标。

这三个阶段刻意分离：耗时的附件读取和 Agent 推理不能阻塞微信消息扫描；所有真正
发送到微信的动作仍通过回复 FIFO 串行执行，避免多个线程同时操作同一个客户端窗口。
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import re
import shutil
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel
from channel.wechat_desktop.backend import create_wechat_desktop_backend
from channel.wechat_desktop.config import DEFAULT_CONFIG, load_wechat_desktop_config
from channel.wechat_desktop.daily_hot_scheduler import DailyHotScheduler
from channel.wechat_desktop.fifo_queue import ReplyQueueItem, WechatReplyQueue
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.policy import WechatDesktopPolicy
from channel.wechat_desktop.service import get_wechat_desktop_service
from channel.wechat_desktop.wechat_desktop_message import WechatDesktopMessage
from common.log import logger
from common.singleton import singleton
from common.utils import expand_path
from config import conf
from plugins import Event, EventContext, PluginManager

# Re-export for callers that historically imported defaults from this module.
__all__ = ["WechatDesktopChannel", "DEFAULT_CONFIG"]

ATTACHMENT_REFERENCE_REQUIRED_REPLY = (
    "为了确保我读取的是正确的图片或文件，请在微信中引用对应的图片或文件消息后再提问。"
)
DEFAULT_BOT_MENTION_ALIASES = ("颜料盒bot",)


def _normalize_auto_reply_text(value) -> str:
    """去掉“建议回复：”等代写包装，只保留要发给微信用户的正文。"""
    text = str(value or "").strip()
    if not text:
        return ""
    wrapper = re.compile(
        r"^(?:(?:你)?可以|建议|推荐)?\s*"
        r"(?:这样\s*)?回(?:复)?(?:对方)?(?:内容)?\s*[:：]\s*",
        re.IGNORECASE,
    )
    text = wrapper.sub("", text, count=1).strip()
    text = re.sub(r"^(?:回复内容|回复)\s*[:：]\s*", "", text, count=1).strip()
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE).strip()
    if text.startswith("**") and text.endswith("**") and len(text) > 4:
        text = text[2:-2].strip()
    quote_pairs = [("“", "”"), ('"', '"'), ("「", "」")]
    for left, right in quote_pairs:
        if text.startswith(left) and text.endswith(right) and len(text) > 2:
            text = text[len(left):-len(right)].strip()
            break
    return text


def _strip_group_bot_mentions(value: str, aliases) -> str:
    """从群聊提示词移除机器人的 @，保留对其他成员的显式 @。"""
    text = str(value or "")
    normalized_aliases = list(
        dict.fromkeys(
            str(alias or "").strip().lstrip("@").casefold()
            for alias in aliases or []
            if str(alias or "").strip().lstrip("@")
        )
    )
    for alias in normalized_aliases:
        pattern = re.compile(
            rf"@{re.escape(alias)}(?=$|[\s\u2005\u00a0,，。.!！?？:：])",
            re.IGNORECASE,
        )
        text = pattern.sub("", text)
    # 微信会在 @ 后插入特殊空格；删除名字后顺手收敛横向空白，但不合并换行。
    text = re.sub(r"[\t \u2005\u00a0]{2,}", " ", text)
    text = re.sub(r"^[\t \u2005\u00a0]+", "", text, flags=re.MULTILINE)
    return text.strip()


def _render_event_context_lines(event: WechatDesktopEvent) -> tuple[str, list[str]]:
    """将事件快照渲染成提示词片段，不在这里重新读取微信 UI。

    引用消息只返回被引用的一层内容；普通消息返回候选历史，后续由提示词要求
    Agent 按相关性筛选。这样可以保证排队期间 UI 变化不会改变回复依据。
    """
    if event.reference:
        reference = event.reference
        speaker = str(reference.get("sender_name") or "原消息发送者")
        reference_type = str(reference.get("content_type") or "text").lower()
        reference_path = str(reference.get("file_path") or "").strip()
        reference_content = str(reference.get("content") or "").strip()
        if reference_type == "image" and reference_path:
            reference_content = f"[图片: {reference_path}]"
        elif reference_type == "file" and reference_path:
            reference_content = f"[文件: {reference_path}]"
        elif not reference_content:
            labels = {"image": "图片", "file": "文件"}
            reference_content = f"[{labels.get(reference_type, '原消息')}内容不可用]"
        return "[被引用的内容]", [f"{speaker}: {reference_content}"]

    history_lines = []
    for item in event.history:
        if event.is_group:
            speaker = str(
                item.get("sender_name") or item.get("sender") or "群成员"
            )
        else:
            speaker = "历史消息"
        history_lines.append(
            f"{speaker}: {item.get('content') or '[非文字消息]'}"
        )
    return "[候选会话上下文，需按关联度筛选]", history_lines


def _reply_requirements(event: WechatDesktopEvent) -> str:
    """生成引用消息与普通消息各自的回复约束。"""
    style = (
        "像本人聊天一样直接回复正文，尽量自然、简短、口语化。"
        "不要出现“可以回复”“可以回”“建议回复”“回复如下”等"
        "提示语，也不要用引号、Markdown 加粗或解释你正在代写。"
    )
    group_rule = (
        "群聊中，只有待回复消息明确使用“@成员”时，才认为发送者在询问、"
        "指向或要求该成员回应；没有 @ 时，不要仅因正文出现成员姓名就作此推断。"
        if event.is_group
        else ""
    )
    if event.reference:
        return (
            style
            + "当前是引用消息，只根据“被引用的内容”和“需要回复的引用消息”作答；"
            "不要使用、补充或推断引用之外的会话上下文。"
            + group_rule
        )
    return (
        style
        + group_rule
        + "先判断候选会话上下文与待回复消息的语义关联度，"
        "只保留并使用关联度高、能帮助理解当前意图的内容；"
        "关联度低、已结束或属于其他话题的内容直接忽略。"
        "不要逐条回复上下文，也不要在答复中复述筛选过程。"
        "若候选上下文包含本地文件或图片，只在待回复消息确实要求处理该附件时"
        "调用相应工具读取；否则不要擅自分析附件。"
    )


def _tool_notice_subject(data: dict) -> tuple[str, str]:
    """返回通知类型和展示名，并把读取 SKILL.md 识别为技能调用。"""
    tool_name = str(data.get("tool_name") or "tool").strip() or "tool"
    arguments = data.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            arguments = {}
    if isinstance(arguments, dict):
        for key in ("path", "file_path", "location"):
            value = str(arguments.get(key) or "").replace("\\", "/").rstrip("/")
            match = re.search(r"(?:^|/)skills/([^/]+)/SKILL\.md$", value, re.I)
            if match:
                return "skill", match.group(1)
    return "tool", tool_name


def _format_agent_notice(templates: list[str], kind: str, name: str) -> str:
    """渲染工具/技能通知；旧模板没有占位符时自动补上工具名。"""
    fallback = f"我准备调用 `{name}` {kind}，稍等一下 🛠️"
    if not templates:
        return fallback
    try:
        notice = random.choice(templates).format(
            name=name,
            tool_name=name,
            kind=kind,
        )
    except (KeyError, ValueError):
        return fallback
    # Older custom tool templates may not contain a placeholder. Tool notices
    # must still disclose the concrete tool being executed.
    if kind == "tool" and name.casefold() not in notice.casefold():
        notice = f"{notice.rstrip()} 当前工具：`{name}`。"
    return notice


def _format_failure_notice(templates) -> str:
    """挑选一条不暴露内部异常细节的轻松失败提示。"""
    fallback = "刚才脑内小齿轮打了个滑 😵‍💫 请再戳我一下，我重新来过。"
    candidates = [str(item).strip() for item in templates or [] if str(item).strip()]
    return random.choice(candidates) if candidates else fallback


def _preflight_tool_notice_data(event: WechatDesktopEvent) -> dict | None:
    """根据附件类型预判首个工具，使耗时读取开始前就能发送进度通知。"""
    content_type = str(event.content_type or "").lower()
    path = str(event.content or "")
    if event.reference:
        reference_type = str(event.reference.get("content_type") or "").lower()
        reference_path = str(event.reference.get("file_path") or "")
        if reference_type in {"image", "file"}:
            if not reference_path:
                return None
            content_type, path = reference_type, reference_path
    if content_type == "image":
        return {"tool_name": "vision", "arguments": {"path": path}}
    if content_type != "file":
        return None
    suffix = Path(path).suffix.lower()
    skill_names = {
        ".doc": "docx",
        ".docx": "docx",
        ".pdf": "pdf-reader",
        ".xls": "xlsx",
        ".xlsx": "xlsx",
    }
    skill_name = skill_names.get(suffix)
    if skill_name:
        return {
            "tool_name": "read",
            "arguments": {"path": f"skills/{skill_name}/SKILL.md"},
        }
    return {"tool_name": "file-reader", "arguments": {"path": path}}


def _is_network_reply_error(value) -> bool:
    """判断 Agent 错误是否属于应终止当前排队任务的网络故障。"""
    text = str(value or "").casefold()
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "connection error",
            "connection reset",
            "connection aborted",
            "sslerror",
            "ssl:",
            "unexpected_eof",
            "max retries exceeded",
            "network is unreachable",
        )
    )


@singleton
class WechatDesktopChannel(ChatChannel):
    """连接桌面微信后端、Agent 桥接层和回复策略的单例通道。

    线程模型：

    - ``_scan_thread`` 只发现和路由事件；
    - ``_materialize_thread`` 解析附件并生成稳定的回复任务；
    - ``_queue_thread`` 串行消费回复任务，等待 Agent 和发送结果。

    后端通过工厂注入，因此这里不应出现 UIA 控件选择器或窗口句柄操作。
    """

    NOT_SUPPORT_REPLYTYPE = [ReplyType.VOICE, ReplyType.FILE, ReplyType.VIDEO, ReplyType.VIDEO_URL]

    def __init__(self):
        """加载配置，并创建尚未启动的队列、锁、服务和后端对象。"""
        super().__init__()
        self.config = load_wechat_desktop_config()
        self._stop_event = threading.Event()
        # 后端通过工厂创建，为未来迁移到非 UIA 实现保留稳定替换点。
        self._driver = create_wechat_desktop_backend(self.config)

        # 最终回复必须全局串行；队列项同时固化入队时的上下文快照。
        self._reply_queue = WechatReplyQueue(
            int(self.config.get("active_conversation_burst_limit", 5))
        )

        # 私聊消息先按会话短暂聚合，避免连续气泡触发多次独立回复。值为
        # ``conversation_id -> (尚未释放的事件, 滑动窗口定时器)``。
        self._pending_private_lock = threading.RLock()
        self._pending_private_batches: dict[
            str, tuple[list[WechatDesktopEvent], threading.Timer, float]
        ] = {}
        self._private_batch_timer_factory = threading.Timer

        # 附件物化与消息扫描解耦。提交锁只保护“停止/入队”的原子性，耗时解析
        # 在独立线程执行，不持有该锁。
        self._materialize_submit_lock = threading.RLock()
        self._materialize_queue: queue.Queue = queue.Queue()
        self._materialize_stop = object()
        self._materialization_active = threading.Event()
        self._queue_thread = None
        self._scan_thread = None
        self._materialize_thread = None
        self._daily_hot_scheduler: DailyHotScheduler | None = None

        # 生命周期表只用于诊断耗时，不参与消息业务判断。
        self._lifecycle_lock = threading.RLock()
        self._failure_notice_lock = threading.RLock()
        self._lifecycles: dict[str, dict] = {}
        self._scan_count = 0
        self._service = get_wechat_desktop_service()
        self._store = self._service.store
        self._policy = WechatDesktopPolicy(self.config, self._store)
        normalized_history = self._store.normalize_outgoing_history(
            _normalize_auto_reply_text
        )
        if normalized_history:
            logger.info(
                "[WechatDesktop] normalized %s stored outgoing messages",
                normalized_history,
            )
        deduplicated_history = self._store.deduplicate_conversation_history()
        if deduplicated_history:
            logger.info(
                "[WechatDesktop] removed %s duplicate stored messages",
                deduplicated_history,
            )
        self._bootstrapped = False
        self._last_cleanup_at = 0.0
        self.login_status = "idle"
        self.user_id = "wechat_desktop_self"
        self.name = str(self.config.get("self_display_name", "") or "我")

    def _trace(self, stage: str, message: str, *args):
        """按配置输出细粒度诊断日志；关闭时不承担格式化成本。"""
        if bool(self.config.get("diagnostic_logging", False)):
            logger.info(
                "[WechatDesktop][trace:%s] " + message,
                stage,
                *args,
            )

    def _start_lifecycle(self, event: WechatDesktopEvent):
        """为新观察到的事件创建端到端耗时记录。"""
        now = time.monotonic()
        with self._lifecycle_lock:
            self._lifecycles.setdefault(
                event.event_id,
                {
                    "event_id": event.event_id,
                    "conversation": event.conversation_name,
                    "batch_id": "",
                    "detected": now,
                    "materialized": None,
                    "queued": None,
                    "agent_started": None,
                    "first_tool": None,
                    "agent_done": None,
                    "send_started": None,
                    "send_verified": None,
                    "send_result": "-",
                    "detected_scan": self._scan_count,
                    "attachment_resolve_count": 0,
                    "superseded_by": "",
                },
            )

    def _mark_lifecycle(
        self,
        event_ids,
        stage: str,
        *,
        batch_id: str = "",
        attachment_resolve_count: int = 0,
        send_result: str = "",
    ):
        """记录生命周期阶段的首次到达时间和少量累计指标。"""
        now = time.monotonic()
        with self._lifecycle_lock:
            for event_id in event_ids:
                lifecycle = self._lifecycles.get(str(event_id))
                if lifecycle is None:
                    continue
                if stage in lifecycle and lifecycle[stage] is None:
                    lifecycle[stage] = now
                if batch_id:
                    lifecycle["batch_id"] = batch_id
                if attachment_resolve_count:
                    lifecycle["attachment_resolve_count"] += int(
                        attachment_resolve_count
                    )
                if send_result:
                    lifecycle["send_result"] = send_result

    def _finish_lifecycle(self, event_ids, terminal: str):
        """结束生命周期并输出从发现到发送完成的阶段耗时。"""
        rows = []
        with self._lifecycle_lock:
            for event_id in event_ids:
                lifecycle = self._lifecycles.pop(str(event_id), None)
                if lifecycle is not None:
                    rows.append(lifecycle)
            scan_count = self._scan_count
        for lifecycle in rows:
            detected = lifecycle["detected"]

            def elapsed(field):
                """把阶段时间转换为相对发现时刻的毫秒字符串。"""
                value = lifecycle.get(field)
                return "-" if value is None else str(max(0, int((value - detected) * 1000)))

            logger.info(
                "[WechatDesktop][lifecycle] "
                "event_id=%s batch_id=%s conversation=%s terminal=%s "
                "detected=0 materialized=%s queued=%s agent_started=%s "
                "first_tool=%s agent_done=%s send_started=%s send_verified=%s "
                "scan_count=%s attachment_resolve_count=%s superseded_by=%s "
                "send_result=%s",
                lifecycle["event_id"],
                lifecycle["batch_id"] or "-",
                lifecycle["conversation"],
                terminal,
                elapsed("materialized"),
                elapsed("queued"),
                elapsed("agent_started"),
                elapsed("first_tool"),
                elapsed("agent_done"),
                elapsed("send_started"),
                elapsed("send_verified"),
                max(1, scan_count - int(lifecycle["detected_scan"]) + 1),
                lifecycle["attachment_resolve_count"],
                lifecycle["superseded_by"] or "-",
                lifecycle["send_result"],
            )

    def startup(self):
        """初始化运行状态、启动三个工作线程，并阻塞到通道停止。"""
        self._stop_event.clear()
        try:
            activated = self._driver.ensure_foreground()
            logger.info(
                "[WechatDesktop] WeChat foreground check completed; activated=%s",
                activated,
            )
        except Exception as exc:
            # Keep the channel alive so its regular reconciliation loop can
            # recover if WeChat is launched or becomes available later.
            logger.warning(
                "[WechatDesktop] unable to bring WeChat to the foreground at startup: %s",
                exc,
            )
        self._reply_queue = WechatReplyQueue(
            int(self.config.get("active_conversation_burst_limit", 5))
        )
        self._materialize_queue = queue.Queue()
        self._materialization_active.clear()
        self._queue_thread = threading.Thread(
            target=self._consume_reply_queue,
            name="cow-wechat-reply-fifo",
            daemon=True,
        )
        self._materialize_thread = threading.Thread(
            target=self._consume_materialization_queue,
            name="cow-wechat-materialize",
            daemon=True,
        )
        self._scan_thread = threading.Thread(
            target=self._scan_loop,
            name="cow-wechat-scan",
            daemon=True,
        )
        self._service.set_agent_executor(self._execute_agent_action)
        self._service.update_status(
            running=True,
            paused=bool(self._store.get_state("paused", False)),
            shadow_mode=bool(self.config.get("shadow_mode", True)),
            auto_reply_private_all=bool(
                self.config.get("auto_reply_private_all", False)
            ),
            auto_reply_groups_all=bool(
                self.config.get("auto_reply_groups_all", False)
            ),
            auto_reply_blacklist=list(
                self.config.get("auto_reply_blacklist", [])
            ),
            diagnostic_logging=bool(
                self.config.get("diagnostic_logging", False)
            ),
            auto_reply_contacts=list(self.config.get("auto_reply_contacts", [])),
            auto_reply_groups=list(self.config.get("auto_reply_groups", [])),
            group_reply_mode=str(self.config.get("group_reply_mode", "at_or_prefix")),
            last_error="",
        )
        self._queue_thread.start()
        self._materialize_thread.start()
        self._scan_thread.start()
        self._daily_hot_scheduler = DailyHotScheduler(
            config=self.config,
            store=self._store,
            enqueue_callback=self.enqueue_daily_hot_broadcast,
            is_paused=lambda: bool(self._service.status().get("paused")),
        )
        self._daily_hot_scheduler.start()
        self._service.update_status(**self._daily_hot_scheduler.status())
        self.report_startup_success()
        logger.info(
            "[WechatDesktop] Channel started in %s mode",
            "shadow" if self.config.get("shadow_mode", True) else "active",
        )
        self._trace(
            "00-startup",
            "reconcile_enabled=%s reconcile_seconds=%s auto_reply_private=%s group_mode=%s blacklist=%s daily_hot=%s@%s",
            bool(self.config.get("shell_hook_reconcile_enabled", False)),
            self.config.get("shell_hook_reconcile_seconds"),
            bool(self.config.get("auto_reply_private_all")),
            self.config.get("group_reply_mode"),
            list(self.config.get("auto_reply_blacklist", [])),
            bool(self.config.get("daily_hot_broadcast_enabled", False)),
            self.config.get("daily_hot_broadcast_time", "18:00"),
        )
        while not self._stop_event.wait(0.25):
            pass

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

    def stop(self):
        """停止接收新任务，清空各阶段待处理项，并有限等待工作线程退出。"""
        self._stop_event.set()
        scheduler = self._daily_hot_scheduler
        if scheduler is not None:
            scheduler.stop()
        pending_ids = self._clear_pending_private_batches()
        for event_id in pending_ids:
            self._store.mark_event_processed(event_id)
            self._finish_lifecycle([event_id], "stopped")
        queued_ids = self._reply_queue.clear_pending()
        for event_id in queued_ids:
            self._store.mark_event_processed(event_id)
        self._finish_lifecycle(queued_ids, "stopped")
        materializing_ids = self._clear_pending_materializations()
        for event_id in materializing_ids:
            self._store.mark_event_processed(event_id)
        self._finish_lifecycle(materializing_ids, "stopped")
        self._materialize_queue.put(self._materialize_stop)
        discarded = self._reply_queue.stop()
        close = getattr(self._driver, "close", None)
        if close:
            close()
        self._service.set_agent_executor(None)
        self._service.update_status(running=False, login_status="stopped")
        for worker in (
            self._scan_thread,
            self._materialize_thread,
            self._queue_thread,
        ):
            if worker and worker is not threading.current_thread():
                worker.join(timeout=2.0)
        if discarded:
            logger.info("[WechatDesktop] discarded %s queued messages on stop", discarded)

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
        """Only open a new debounce batch when no reply work is in flight."""
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

    def _submit_materialization(self, events: list[WechatDesktopEvent]):
        """原子地提交一个消息批次到附件物化线程。"""
        if not events:
            return False
        with self._materialize_submit_lock:
            if self._stop_event.is_set():
                accepted = False
            else:
                self._materialize_queue.put(events)
                accepted = True
        if not accepted:
            event_ids = [event.event_id for event in events]
            for event_id in event_ids:
                self._store.mark_event_processed(event_id)
            self._finish_lifecycle(event_ids, "stopped")
        return accepted

    def _clear_pending_materializations(self) -> list[str]:
        """清空尚未开始的物化批次，供停止和网络故障收尾使用。"""
        event_ids = []
        with self._materialize_submit_lock:
            while True:
                try:
                    events = self._materialize_queue.get_nowait()
                except queue.Empty:
                    break
                if events is not self._materialize_stop:
                    event_ids.extend(event.event_id for event in events)
                self._materialize_queue.task_done()
        return list(dict.fromkeys(event_ids))

    @staticmethod
    def _attachment_marker(event: WechatDesktopEvent) -> str:
        """将已落盘附件表示成可放入 Agent 上下文的稳定文本标记。"""
        if event.content_type == "file":
            return f"[文件: {event.content}]"
        if event.content_type == "image":
            return f"[图片: {event.content}]"
        return str(event.content or "")

    @staticmethod
    def _requires_attachment_reference(event: WechatDesktopEvent) -> bool:
        """判断用户是否在未引用具体附件的情况下要求读取“这张图/文件”。

        这种请求无法可靠确定目标，不能猜测最近附件；消费者会直接提示用户使用
        微信引用功能后重试。生成图片之类的创作意图不属于此情况。
        """
        if event.content_type != "text":
            return False
        # Any explicit quote already gives the Agent an unambiguous target.
        # In particular, a quoted text message may itself discuss a picture or
        # file; treating its wording as an unreferenced attachment request would
        # incorrectly replace the Agent reply with the attachment prompt.
        if event.reference:
            return False
        text = unicodedata.normalize("NFKC", str(event.content or "")).strip()
        if not text or re.search(r"(?:生成|创建|制作|画一张|做一张)", text):
            return False
        attachment = re.search(
            r"(?:图(?:片|像)?|照片|截图|文件|文档|附件|表格|PDF)",
            text,
            re.IGNORECASE,
        )
        question = re.search(
            r"(?:是什么|是啥|什么内容|讲了?什么|写了?什么|说了?什么|"
            r"内容是|分析|总结|概括|解读|看一下|看看|读一下|读取)",
            text,
            re.IGNORECASE,
        )
        return bool(attachment and question)

    def _materialize_batch(
        self, events: list[WechatDesktopEvent]
    ) -> WechatDesktopEvent:
        """解析批次附件，并把多条消息折叠为一个可回复事件。

        最后一条消息是回复目标，前面的同批消息并入它的历史。只有显式引用的附件
        才会继续出现在普通文本上下文中，防止 Agent 把缓存中的旧图片误当成目标。
        返回事件上的 ``_source_event_ids`` 和 ``_batch_id`` 用于生命周期追踪。
        """
        resolved_events = []
        for event in events:
            event, resolve_count = self._driver.materialize_event(event)
            self._preserve_event_evidence(event)
            resolved_events.append(event)
            self._mark_lifecycle(
                [event.event_id],
                "materialized",
                attachment_resolve_count=resolve_count,
            )

        target = resolved_events[-1]
        batch_id = str(
            getattr(events[-1], "_batch_id", "") or uuid.uuid4().hex
        )
        source_event_ids = [event.event_id for event in resolved_events]
        history = list(target.history)
        existing = {
            (str(item.get("content") or ""), str(item.get("content_type") or ""))
            for item in history
        }
        for event in resolved_events[:-1]:
            content = self._attachment_marker(event)
            key = (content, event.content_type)
            replaced_history_item = False
            if event.message_stable_id:
                for item in history:
                    if (
                        str(item.get("_message_stable_id") or "")
                        == event.message_stable_id
                    ):
                        item["content"] = content
                        item["content_type"] = event.content_type
                        replaced_history_item = True
                        existing.add(key)
                        break
            if replaced_history_item:
                continue
            if key not in existing:
                history.append(
                    {
                        "sender_name": event.sender_name,
                        "content": content,
                        "content_type": event.content_type,
                    }
                )
                existing.add(key)
        reference_type = str(target.reference.get("content_type") or "").lower()
        if (
            target.content_type == "text"
            and reference_type not in {"file", "image"}
        ):
            # Cached attachments are not implicit context. A text message must
            # explicitly quote the intended attachment before the Agent sees it.
            history = [
                item
                for item in history
                if str(item.get("content_type") or "") not in {"file", "image"}
            ]
        target.history = history
        setattr(target, "_source_event_ids", source_event_ids)
        setattr(target, "_batch_id", batch_id)
        setattr(
            target,
            "_cache_only",
            bool(resolved_events)
            and all(
                event.content_type == "file" and not event.reference
                for event in resolved_events
            ),
        )
        setattr(
            target,
            "_attachment_reference_required",
            self._requires_attachment_reference(target),
        )
        self._mark_lifecycle(
            source_event_ids,
            "materialized",
            batch_id=batch_id,
        )
        return target

    @staticmethod
    def _has_referenced_attachment(event: WechatDesktopEvent) -> bool:
        """引用图片/文件需要在真正消费回复任务时占用微信窗口解析。"""
        return str(event.reference.get("content_type") or "").lower() in {
            "file",
            "image",
        }

    def _prepare_deferred_materialization(
        self, events: list[WechatDesktopEvent]
    ) -> WechatDesktopEvent:
        """为引用附件创建轻量队列项，把实际解析推迟到回复 FIFO 队首。

        图片查看器和原文件定位会操作当前微信窗口；若在物化线程提前执行，可能与
        正在发送的上一条回复抢占窗口，因此这里只保存原始事件列表。
        """
        target = events[-1]
        source_event_ids = [event.event_id for event in events]
        batch_id = uuid.uuid4().hex
        setattr(target, "_source_event_ids", source_event_ids)
        setattr(target, "_batch_id", batch_id)
        setattr(target, "_deferred_materialization_events", list(events))
        setattr(target, "_attachment_reference_required", False)
        return target

    def _consume_materialization_queue(self):
        """后台解析附件，并把结果转交回复 FIFO 或仅作为文件缓存结束。"""
        while not self._stop_event.is_set():
            try:
                events = self._materialize_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if events is self._materialize_stop:
                self._materialize_queue.task_done()
                return
            event_ids = [event.event_id for event in events]
            materialization_active = getattr(
                self, "_materialization_active", None
            )
            if materialization_active is not None:
                materialization_active.set()
            try:
                if self._has_referenced_attachment(events[-1]):
                    materialized = self._prepare_deferred_materialization(events)
                else:
                    materialized = self._materialize_batch(events)
                if self._stop_event.is_set():
                    for event_id in event_ids:
                        self._store.mark_event_processed(event_id)
                    self._finish_lifecycle(event_ids, "stopped")
                elif bool(getattr(materialized, "_cache_only", False)):
                    for event_id in event_ids:
                        self._store.mark_event_processed(event_id)
                    self._trace(
                        "09-file-cached",
                        "conversation=%s messages=%s",
                        materialized.conversation_name,
                        len(event_ids),
                    )
                    self._finish_lifecycle(event_ids, "cached")
                else:
                    self._enqueue_reply_event(materialized)
            except Exception:
                logger.exception("[WechatDesktop] message materialization failed")
                for event_id in event_ids:
                    self._store.mark_event_processed(event_id)
                self._finish_lifecycle(event_ids, "failed")
            finally:
                if materialization_active is not None:
                    materialization_active.clear()
                self._materialize_queue.task_done()

    def _enqueue_reply_event(self, event: WechatDesktopEvent):
        """把稳定事件写入全局回复 FIFO，并标记其生命周期进入排队阶段。"""
        enqueue_result = self._reply_queue.enqueue(event)
        source_event_ids = list(
            getattr(event, "_source_event_ids", None) or [event.event_id]
        )
        if not enqueue_result:
            for event_id in source_event_ids:
                self._store.mark_event_processed(event_id)
            self._finish_lifecycle(source_event_ids, "duplicate")
            return
        self._mark_lifecycle(
            source_event_ids,
            "queued",
            batch_id=str(getattr(event, "_batch_id", "") or event.event_id),
        )
        self._trace(
            "09-queued",
            "id=%s conversation=%s action=%s queue_depth=%s",
            event.event_id[:10],
            event.conversation_name,
            enqueue_result.action,
            self._reply_queue.status()["queue_depth"],
        )
        self._service.update_status(**self._reply_queue.status())

    def _resolve_daily_hot_target(self, group_name: str) -> tuple[str, str]:
        """把 ``auto_reply_groups`` 中的群显示名解析为发送用会话 ID。

        若扫描缓存里能唯一匹配到该标题，优先返回 ``uia-session:...``，便于发送时
        带上 runtime_id / row_index，降低点错会话的概率；否则退回显示名本身。
        """
        name = str(group_name or "").strip()
        if not name:
            return name, name
        resolver = getattr(self._driver, "_resolve_selector", None)
        if not callable(resolver):
            return name, name
        try:
            selector = resolver(name)
        except Exception:
            return name, name
        title = str(getattr(selector, "title", "") or name).strip() or name
        runtime_id = str(getattr(selector, "runtime_id", "") or "").strip()
        if runtime_id:
            conversation_id = f"uia-session:{runtime_id}"
            logger.info(
                "[WechatDesktop][DailyHot] resolved target title=%s -> %s",
                title,
                conversation_id,
            )
            return conversation_id, title
        return name, title

    def enqueue_daily_hot_broadcast(self, message: str) -> int:
        """把已准备好的每日热点文案按白名单群拆成预写发送任务并入全局 FIFO。

        目标仅来自 ``auto_reply_groups`` 白名单（不是当前打开的会话，也不走
        ``auto_reply_groups_all``）。返回成功入队的群数量。
        """
        text = str(message or "").strip()
        if not text:
            return 0
        if bool(self.config.get("shadow_mode", True)):
            logger.info(
                "[WechatDesktop][DailyHot] skip enqueue because shadow_mode is on"
            )
            return 0
        if bool(self._service.status().get("paused")):
            logger.info("[WechatDesktop][DailyHot] skip enqueue because paused")
            return 0

        # 仅向 auto_reply_groups 白名单群广播，不使用 auto_reply_groups_all。
        groups = [
            str(name).strip()
            for name in self.config.get("auto_reply_groups", []) or []
            if str(name).strip()
        ]
        if not groups:
            logger.warning(
                "[WechatDesktop][DailyHot] no targets: auto_reply_groups is empty"
            )
        else:
            logger.info(
                "[WechatDesktop][DailyHot] enqueue targets from auto_reply_groups=%s",
                groups,
            )
        queued = 0
        for group_name in groups:
            if self._policy.is_blocked(group_name):
                self._store.audit(
                    "daily_hot_broadcast",
                    group_name,
                    "blocked",
                    self._content_hash(text),
                    detail="blacklist",
                )
                continue
            if not self._policy.is_allowlisted(group_name, True):
                continue
            conversation_id, conversation_name = self._resolve_daily_hot_target(
                group_name
            )
            event = WechatDesktopEvent(
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
            setattr(event, "_proactive_send", True)
            setattr(event, "_precomposed_reply_text", text)
            setattr(event, "_source_event_ids", [event.event_id])
            setattr(event, "_batch_id", event.event_id)
            self._start_lifecycle(event)
            self._store.record_event(event)
            self._enqueue_reply_event(event)
            queued += 1
            self._store.audit(
                "daily_hot_broadcast",
                group_name,
                "queued",
                self._content_hash(text),
                detail=f"event_id={event.event_id}",
            )
        if queued == 0:
            self._store.audit(
                "daily_hot_broadcast",
                "",
                "no_targets",
                self._content_hash(text),
                detail="no allowlisted groups",
            )
        scheduler = getattr(self, "_daily_hot_scheduler", None)
        if scheduler is not None:
            self._service.update_status(**scheduler.status())
        return queued

    def _send_precomposed_reply(self, item: ReplyQueueItem) -> str:
        """发送入队前已准备好的文案（如每日热点），不调用 Agent。"""
        event = item.event
        text = str(
            getattr(event, "_precomposed_reply_text", None) or event.content or ""
        ).strip()
        target_name = event.conversation_name
        target_id = event.conversation_id
        if not text:
            return "skipped"
        if bool(self._service.status().get("paused")) or not self._policy.can_auto_send(
            target_name,
            event.is_group,
            "text",
        ):
            self._store.audit(
                "daily_hot_broadcast",
                target_name,
                "skipped",
                self._content_hash(text),
                detail="paused or policy blocked",
            )
            return "skipped"

        # Prefer a still-valid uia-session key (exact runtime_id). If the row
        # left the scan cache, fall back to the display name so locate never
        # treats "uia-session:..." as a session title.
        send_target = target_name
        if str(target_id).startswith("uia-session:"):
            resolver = getattr(self._driver, "_resolve_selector", None)
            if callable(resolver):
                try:
                    selector = resolver(target_id)
                    if str(getattr(selector, "runtime_id", "") or "").strip():
                        send_target = target_id
                except Exception:
                    pass
        logger.info(
            "[WechatDesktop][DailyHot] precomposed send target_name=%s "
            "conversation_id=%s send_target=%s",
            target_name,
            target_id,
            send_target,
        )
        self._driver.begin_reply_cycle(target_name, target_id)
        self._mark_lifecycle(item.source_event_ids, "send_started")
        try:
            result = self._driver.send_text(send_target, text)
        except Exception as exc:
            self._mark_lifecycle(
                item.source_event_ids,
                "send_started",
                send_result="failed",
            )
            logger.warning(
                "[WechatDesktop] precomposed send failed target=%s: %s",
                target_name,
                exc,
            )
            self._store.audit(
                "send_text",
                target_name,
                "failed",
                self._content_hash(text),
                detail=f"precomposed:{exc}",
            )
            return "failed"
        if not result.get("success"):
            self._mark_lifecycle(
                item.source_event_ids,
                "send_started",
                send_result="failed",
            )
            self._store.audit(
                "send_text",
                target_name,
                "failed",
                self._content_hash(text),
                detail="precomposed send unsuccessful",
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
            self._content_hash(text),
            detail="daily_hot_broadcast",
        )
        self._store.append_conversation_history(
            conversation_id=target_id,
            conversation_name=target_name,
            sender_name=str(self.config.get("self_display_name") or "我"),
            direction="outgoing",
            content_type="text",
            content=text,
            source_type=event.source_type or "group",
        )
        self._trace(
            "12-precomposed-send",
            "target=%s chars=%s verified=%s",
            target_name,
            len(text),
            verified,
        )
        return "completed"

    def _send_attachment_reference_prompt(self, item: ReplyQueueItem) -> str:
        """拒绝猜测未明确指向的附件，并直接发送“请引用后再问”的提示。"""
        event = item.event
        target_name = event.conversation_name
        target_id = event.conversation_id
        if bool(self._service.status().get("paused")) or not self._policy.can_auto_send(
            target_name,
            event.is_group,
            "text",
        ):
            return "skipped"
        validation = self._driver.validate_reply_target(event)
        if not validation.valid:
            if validation.replacement_event is not None:
                self._accept_replacement_event(validation.replacement_event)
            self._store.audit(
                "send_text",
                target_name,
                "stale_target",
                self._content_hash(ATTACHMENT_REFERENCE_REQUIRED_REPLY),
                detail=validation.reason,
            )
            return "skipped"
        send_target = (
            target_id
            if str(target_id).startswith("uia-session:")
            else target_name
        )
        self._mark_lifecycle(item.source_event_ids, "send_started")
        try:
            result = self._driver.send_text(
                send_target,
                ATTACHMENT_REFERENCE_REQUIRED_REPLY,
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
        return "completed"

    def _send_deferred_attachment_notice(self, item: ReplyQueueItem) -> bool:
        """引用附件解析前尽早发送工具进度通知，降低用户等待的不确定感。"""
        event = item.event
        notice_data = _preflight_tool_notice_data(event)
        if not notice_data or not bool(
            self.config.get("agent_tool_notice_enabled", True)
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
            return self._send_agent_tool_notice(context, notice_data)
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] early attachment notice failed: %s", exc
            )
            return False

    def _consume_reply_queue(self):
        """串行消费最终回复任务，并等待每个 Agent 周期到达终态。

        引用附件先在队首完成延迟物化；之后恢复入队时固化的上文，避免等待期间
        微信 UI 变化导致上下文漂移。普通任务由 Agent 回调设置 ``item.done``；超时
        时令牌失效，迟到结果会在发送路径被丢弃。
        """
        while not self._stop_event.is_set():
            item = self._reply_queue.get(timeout=0.25)
            if item is None:
                continue
            self._service.update_status(
                mode="replying",
                reply_in_flight=True,
                reply_conversation=item.event.conversation_name,
                **self._reply_queue.status(),
            )
            terminal = "failed"
            deferred_failed = False
            deferred_events = list(
                getattr(
                    item.event, "_deferred_materialization_events", None
                )
                or []
            )
            if deferred_events:
                try:
                    # 延迟物化会打开图片查看器或定位文件，必须纳入独占回复周期。
                    self._driver.begin_reply_cycle(
                        item.event.conversation_name,
                        item.event.conversation_id,
                    )
                    notice_sent = self._send_deferred_attachment_notice(item)
                    materialized = self._materialize_batch(deferred_events)
                    setattr(
                        materialized,
                        "_preflight_attachment_notice_sent",
                        notice_sent,
                    )
                    item.event = materialized
                except Exception:
                    deferred_failed = True
                    logger.exception(
                        "[WechatDesktop] deferred attachment materialization failed"
                    )
            # The UI may have moved on while this task waited in the FIFO.
            # Always reply with the conversation context captured at enqueue.
            item.restore_context()
            reference_required = bool(
                getattr(item.event, "_attachment_reference_required", False)
            )
            proactive_send = bool(
                getattr(item.event, "_proactive_send", False)
                or getattr(item.event, "_precomposed_reply_text", None)
            )
            if not reference_required and not proactive_send:
                self._mark_lifecycle(
                    item.source_event_ids,
                    "agent_started",
                    batch_id=item.batch_id,
                )
            try:
                if deferred_failed:
                    dispatched = False
                elif item.expired:
                    terminal = item.terminal or "skipped"
                    dispatched = False
                elif reference_required:
                    terminal = self._send_attachment_reference_prompt(item)
                    dispatched = False
                elif proactive_send:
                    terminal = self._send_precomposed_reply(item)
                    dispatched = False
                else:
                    dispatched = self._dispatch_message(item.event, item.token)
                if reference_required or proactive_send:
                    pass
                elif deferred_failed:
                    terminal = "failed"
                elif not dispatched:
                    terminal = item.terminal or "skipped"
                else:
                    timeout = max(
                        1.0,
                        float(self.config.get("reply_cycle_timeout_seconds", 180)),
                    )
                    if item.done.wait(timeout):
                        terminal = item.terminal or "completed"
                    else:
                        terminal = "timeout"
                        self._reply_queue.expire(item.token)
                        try:
                            from agent.protocol import get_cancel_registry

                            get_cancel_registry().cancel_request(item.event.event_id)
                        except Exception as exc:
                            logger.warning(
                                "[WechatDesktop] failed to cancel timed-out event %s: %s",
                                item.event.event_id,
                                exc,
                            )
            except Exception as exc:
                terminal = "failed"
                logger.error(
                    "[WechatDesktop] queued message failed: %s", exc, exc_info=True
                )
            finally:
                if terminal in {"failed", "timeout"} and not proactive_send:
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
                if not reference_required:
                    self._driver.end_reply_cycle()
                    if not proactive_send:
                        self._mark_lifecycle(item.source_event_ids, "agent_done")
                for event_id in item.source_event_ids:
                    self._store.mark_event_processed(event_id)
                self._store.audit(
                    "reply_queue",
                    item.event.conversation_name,
                    terminal,
                    detail=f"event_id={item.event.event_id}",
                )
                self._reply_queue.finish(item, terminal)
                self._finish_lifecycle(item.source_event_ids, terminal)
                status_updates = {
                    "reply_in_flight": False,
                    "reply_conversation": "",
                    **self._reply_queue.status(),
                }
                scheduler = getattr(self, "_daily_hot_scheduler", None)
                if scheduler is not None:
                    status_updates.update(scheduler.status())
                self._service.update_status(**status_updates)

    def _preserve_event_evidence(self, event: WechatDesktopEvent):
        """把临时截图/附件复制到 Agent 工作区，避免微信缓存清理后路径失效。"""
        source = str(event.evidence_path or "")
        if not source or not os.path.isfile(source):
            return
        workspace = Path(expand_path(conf().get("agent_workspace", "~/cow")))
        evidence_dir = workspace / "wechat_evidence"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(source).suffix or ".png"
        target = evidence_dir / f"{event.event_id}{suffix}"
        try:
            shutil.copy2(source, target)
            old_source = event.evidence_path
            event.evidence_path = str(target)
            if event.content_type == "image" and event.content == old_source:
                event.content = str(target)
            if event.reference.get("file_path") == old_source:
                event.reference["file_path"] = str(target)
        except OSError as exc:
            logger.warning(f"[WechatDesktop] failed to preserve evidence: {exc}")

    def _image_agent_prompt(self, event: WechatDesktopEvent) -> str:
        """把本地图片路径转换为明确要求调用 vision 的文字提示。"""
        if not bool(self.config.get("analyze_incoming_images", True)):
            return "[收到一张图片，当前 AI 仅处理文字，请人工查看]"
        if not event.content or not Path(event.content).is_file():
            return "[收到一张图片，但无法提取图像区域]"
        return (
            f"[需要回复的微信图片已保存到本地: {event.content}]\n"
            "请使用 vision 工具读取这张图片，并先筛选图片附近会话历史中与其高度相关的内容，"
            "再判断发送者的意图并自然回复。"
            "不要只做机械的图片描述；如果发送者是在提问、确认或延续前文，应直接回应其真实意图。"
        )

    def _dispatch_message(self, event: WechatDesktopEvent, queue_token: str = "") -> bool:
        """构造 Agent 上下文并启动一次异步回复周期。

        引用消息只包含被引用内容和当前消息；普通消息携带入队时的候选上文并要求
        Agent 做相关性筛选。图片和文件仍以文本上下文投递，但附加必须调用对应工具
        的指令。返回 ``True`` 表示已成功交给 Agent，最终发送状态由回调完成。
        """
        msg = WechatDesktopMessage(event)
        ctype = msg.ctype
        content = msg.content
        attachment_instruction = ""
        if ctype == ContextType.IMAGE:
            ctype = ContextType.TEXT
            content = self._image_agent_prompt(event)
            if (
                bool(self.config.get("analyze_incoming_images", True))
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
            history_items = list(event.history)
            self._trace(
                "09-context",
                "id=%s conversation=%s history_items=%s history_source=%s",
                event.event_id[:10],
                event.conversation_name,
                len(history_items),
                "visible",
            )
            history_heading, history_lines = _render_event_context_lines(event)
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
            content = "\n".join(sections)
            if event.is_group:
                content = _strip_group_bot_mentions(
                    content,
                    [
                        *DEFAULT_BOT_MENTION_ALIASES,
                        self.config.get("self_display_name", ""),
                    ],
                )
        context = self._compose_context(
            ctype,
            content,
            isgroup=event.is_group,
            msg=msg,
            no_need_at=True,
            wechat_desktop_evidence=event.evidence_path,
            wechat_desktop_source_type=event.source_type,
            wechat_desktop_auto_reply=True,
        )
        if context:
            self._trace(
                "09-produce",
                "id=%s session=%s context_type=%s",
                event.event_id[:10],
                context.get("session_id"),
                context.type,
            )
            context["wechat_desktop_reply_cycle"] = True
            context["wechat_desktop_queue_token"] = queue_token
            context["wechat_desktop_queue_terminal"] = "completed"
            context["wechat_desktop_source_event_ids"] = list(
                getattr(event, "_source_event_ids", None) or [event.event_id]
            )
            context["wechat_desktop_batch_id"] = str(
                getattr(event, "_batch_id", "") or event.event_id
            )
            context["wechat_desktop_agent_notice_sent"] = bool(
                getattr(event, "_preflight_attachment_notice_sent", False)
            )
            self._driver.begin_reply_cycle(
                event.conversation_name, event.conversation_id
            )
            try:
                notice_data = None
                if bool(self.config.get("agent_preflight_notice_enabled", True)):
                    notice_data = _preflight_tool_notice_data(event)
                if (
                    notice_data
                    and not context["wechat_desktop_agent_notice_sent"]
                    and bool(
                    self.config.get("agent_tool_notice_enabled", True)
                    )
                ):
                    try:
                        context["wechat_desktop_agent_notice_sent"] = (
                            self._send_agent_tool_notice(context, notice_data)
                        )
                    except Exception as exc:
                        logger.warning(
                            "[WechatDesktop] Agent preflight notice failed: %s", exc
                        )
                context["on_event"] = self._make_agent_event_callback(context)
                self.produce(context)
            except Exception:
                context["wechat_desktop_reply_cycle"] = False
                self._driver.end_reply_cycle()
                raise
            return True
        self._trace(
            "09-dropped",
            "id=%s reason=context_filtered_by_plugin_or_empty",
            event.event_id[:10],
        )
        return False

    def _make_agent_event_callback(self, context: Context):
        """包装 Agent 流事件，记录首次工具调用并按配置发送一次进度通知。"""
        downstream = context.get("on_event")
        lock = threading.Lock()
        seen_tool_calls: set[str] = set()
        notice_sent = bool(context.get("wechat_desktop_agent_notice_sent", False))

        def on_event(event: dict):
            """处理通道关心的流事件后，保持原回调链继续向下传递。"""
            nonlocal notice_sent
            try:
                if event.get("type") == "tool_execution_start":
                    self._mark_lifecycle(
                        context.get("wechat_desktop_source_event_ids", []),
                        "first_tool",
                    )
                if (
                    event.get("type") == "tool_execution_start"
                    and bool(self.config.get("agent_tool_notice_enabled", True))
                ):
                    data = event.get("data", {})
                    tool_call_id = str(
                        data.get("tool_call_id") or data.get("tool_name") or "tool"
                    )
                    with lock:
                        once = bool(
                            self.config.get("agent_tool_notice_once_per_reply", True)
                        )
                        if tool_call_id not in seen_tool_calls and not (
                            once and notice_sent
                        ):
                            seen_tool_calls.add(tool_call_id)
                            notice_sent = self._send_agent_tool_notice(
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

    def _send_agent_tool_notice(self, context: Context, data: dict) -> bool:
        """向当前微信会话发送工具/技能进度，并登记为不参与回复识别的临时文本。

        发送前再次检查队列令牌、暂停状态、影子模式和白名单，防止过期 Agent 周期
        或只观察模式产生额外消息。
        """
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
        conversation_id = str(
            getattr(event, "conversation_id", "") or target_id
        )
        queue_token = str(context.get("wechat_desktop_queue_token") or "")
        if queue_token and not self._reply_queue.is_relevant(
            queue_token, conversation_id
        ):
            return False
        send_target = (
            target_id
            if str(target_id).startswith("uia-session:")
            else target_name
        )
        is_group = bool(context.get("isgroup", False))
        if (
            bool(self._service.status().get("paused"))
            or bool(self.config.get("shadow_mode", True))
            or self._policy.is_blocked(target_name)
            or not self._policy.is_allowlisted(target_name, is_group)
        ):
            return False

        kind, name = _tool_notice_subject(data)
        name = re.sub(r"[\r\n`]+", " ", name).strip()[:80] or kind
        template_key = (
            "agent_skill_notice_templates"
            if kind == "skill"
            else "agent_tool_notice_templates"
        )
        templates = [
            str(item)
            for item in self.config.get(template_key, [])
            if str(item).strip()
        ]
        notice = _format_agent_notice(templates, kind, name)

        self._driver.register_interim_text(conversation_id, notice)
        try:
            send_interim = getattr(self._driver, "send_interim_text", None)
            result = (
                send_interim(send_target, notice)
                if send_interim
                else self._driver.send_text(send_target, notice)
            )
            if not result.get("success"):
                raise RuntimeError(str(result.get("message") or "send failed"))
        except Exception:
            self._driver.forget_interim_text(conversation_id, notice)
            raise

        self._store.append_conversation_history(
            conversation_id=target_id,
            conversation_name=target_name,
            sender_name=str(self.config.get("self_display_name") or "我"),
            direction="outgoing",
            content_type="text",
            content=notice,
            source_type=str(
                context.get("wechat_desktop_source_type", "unknown")
            ),
        )
        self._store.audit(
            "agent_tool_notice",
            target_name,
            "success" if result.get("verified") else "unverified",
            self._content_hash(notice),
            detail=f"{kind}={name}",
        )
        self._trace(
            "10-tool-notice",
            "target=%s kind=%s name=%s verified=%s",
            target_name,
            kind,
            name,
            bool(result.get("verified")),
        )
        return True

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

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        """创建 CowAgent Context，并允许插件在投递前修改或拦截。

        微信桌面通道已经在自身策略层完成准入判断，因此这里不依赖其他 Channel 的
        全局白名单；图片、语音等最终也会被规范为 Agent 可消费的文本上下文。
        """
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
                {"channel": self, "context": context},
            )
        )
        context = event_context["context"]
        if event_context.is_pass() or context is None:
            return context

        if ctype == ContextType.TEXT:
            text = str(content or "").strip()
            if kwargs.get("isgroup"):
                prefixes = self.config.get("group_command_prefixes", ["/cow"])
                for prefix in prefixes:
                    if prefix and text.startswith(prefix):
                        text = text[len(prefix):].lstrip()
                        break
            context.type = ContextType.TEXT
            context.content = text
        return context

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
                        context["wechat_desktop_queue_terminal"] = "completed"
                        self._store.audit(
                            "send_image",
                            target_name,
                            "success" if result.get("verified") else "unverified",
                            self._content_hash(reply.content),
                            detail=str(result.get("message", "")),
                        )
                        return
                except Exception as exc:
                    logger.warning(f"[WechatDesktop] automatic image send failed: {exc}")
            context["wechat_desktop_queue_terminal"] = "failed" if can_auto else "skipped"
            self._store.audit(
                "send_image",
                target_name,
                "failed" if can_auto else "blocked",
                self._content_hash(reply.content),
                detail="direct image send failed or was blocked; no approval flow is enabled",
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
        """供 Agent 主动发送微信文字的受限入口。

        当前仅支持 ``send_text``。该路径同样遵守暂停、黑名单、白名单和限流规则，
        并要求后端验证发送结果；它不绕过桌面通道的安全策略。
        """
        if action != "send_text":
            return {
                "status": "error",
                "message": f"unsupported wechat_desktop action: {action}",
            }
        if bool(self._service.status().get("paused")):
            return {
                "status": "error",
                "message": "desktop takeover is paused",
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
