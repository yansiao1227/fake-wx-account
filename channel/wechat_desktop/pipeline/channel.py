"""微信桌面通道的业务编排层。

窗口查找、消息解析和实际点击由 ``desktop_backend`` 实现；本模块负责把后端事件
组织成一条可控的自动回复流水线：

1. 扫描线程发现消息并完成去重、策略过滤；
2. 私聊短时间聚合后进入附件物化队列，群聊直接进入该队列；
3. 物化线程只解析真正需要的图片/文件，并将任务写入全局回复 FIFO；
4. 回复线程串行调用 Agent，等待回调，然后在发送前再次校验会话目标。

这三个阶段刻意分离：耗时的附件读取和 Agent 推理不能阻塞微信消息扫描；所有真正
发送到微信的动作仍通过回复 FIFO 串行执行，避免多个线程同时操作同一个客户端窗口。

实现按流水线阶段拆到 ``pipeline/`` 各模块，本文件只负责组合、启停和对外导出：

- ``prompts``：提示词、通知文案和纯函数
- ``scan``：扫描、策略过滤、私聊聚合
- ``materialize``：附件物化与入队
- ``reply``：FIFO 消费、Agent 投递、每日热点
- ``send``：最终发送、失败处理、Agent 动作
"""

from __future__ import annotations

import queue
import threading
import time

from bridge.reply import ReplyType
from channel.chat_channel import ChatChannel
from channel.wechat_desktop.config import DEFAULT_CONFIG, load_wechat_desktop_config
from channel.wechat_desktop.daily_hot.scheduler import DailyHotScheduler
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.materialize import WechatDesktopMaterializeMixin
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.pipeline.prompts import (
    ATTACHMENT_REFERENCE_REQUIRED_REPLY,
    DEFAULT_BOT_MENTION_ALIASES,
    _format_agent_notice,
    _format_failure_notice,
    _is_network_reply_error,
    _normalize_auto_reply_text,
    _preflight_tool_notice_data,
    _render_event_context_lines,
    _reply_requirements,
    _strip_group_bot_mentions,
    _tool_notice_subject,
)
from channel.wechat_desktop.pipeline.reply import WechatDesktopReplyMixin
from channel.wechat_desktop.pipeline.scan import WechatDesktopScanMixin
from channel.wechat_desktop.pipeline.send import WechatDesktopSendMixin
from channel.wechat_desktop.storage.service import get_wechat_desktop_service
from channel.wechat_desktop.uia.backend import create_wechat_desktop_backend
from common.log import file_logger, logger
from common.singleton import singleton

# 测试与工厂仍可从本模块导入默认配置和纯函数。
__all__ = ["WechatDesktopChannel", "DEFAULT_CONFIG"]


@singleton
class WechatDesktopChannel(
    WechatDesktopScanMixin,
    WechatDesktopMaterializeMixin,
    WechatDesktopReplyMixin,
    WechatDesktopSendMixin,
    ChatChannel,
):
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
        migration_result = self._store.run_startup_migrations(
            normalizer=_normalize_auto_reply_text
        )
        if migration_result.get("normalized"):
            logger.info(
                "[WechatDesktop] normalized %s stored outgoing messages",
                migration_result["normalized"],
            )
        if migration_result.get("deduplicated"):
            logger.info(
                "[WechatDesktop] removed %s duplicate stored messages",
                migration_result["deduplicated"],
            )
        self._bootstrapped = False
        self._last_cleanup_at = 0.0
        self.login_status = "idle"
        self.user_id = "wechat_desktop_self"
        self.name = str(self.config.get("self_display_name", "") or "我")

    def _trace(self, stage: str, message: str, *args):
        """细粒度扫描/会话诊断日志：只写 run.log，不打印到控制台。

        关闭 ``diagnostic_logging`` 时不承担格式化成本。
        """
        if bool(self.config.get("diagnostic_logging", False)):
            file_logger.info(
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

    def _warmup_group_sender_ocr(self) -> None:
        """后台预加载 RapidOCR，避免首次群聊扫描被模型加载拖慢。"""
        try:
            preload = getattr(self._driver, "preload_group_sender_ocr", None)
            if not callable(preload):
                return
            ready = bool(preload())
            if ready:
                logger.info("[WechatDesktop] RapidOCR preloaded at channel startup")
            elif bool(self.config.get("uia_group_sender_ocr_enabled", True)):
                logger.warning(
                    "[WechatDesktop] RapidOCR preload finished without a ready engine"
                )
        except Exception as exc:
            logger.warning(
                "[WechatDesktop] RapidOCR preload failed (non-fatal): %s",
                exc,
            )

    def startup(self):
        """初始化运行状态、启动三个工作线程，并阻塞到通道停止。"""
        self._stop_event.clear()
        # 尽早加载 RapidOCR 模型，避免首次识别群聊发送者时付出数秒加载延迟。
        threading.Thread(
            target=self._warmup_group_sender_ocr,
            name="cow-wechat-rapidocr-warmup",
            daemon=True,
        ).start()
        try:
            activated = self._driver.ensure_foreground()
            logger.info(
                "[WechatDesktop] WeChat foreground check completed; activated=%s",
                activated,
            )
        except Exception as exc:
            # 通道继续运行，后续常规校准循环可在微信稍后启动或恢复时自行接上。
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
            daily_hot_broadcast_groups=list(
                self.config.get("daily_hot_broadcast_groups", [])
            ),
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
