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
- ``reply``：FIFO 任务准备、分流和收尾
- ``agent_reply``：Agent 上下文、回调通知和超时取消
- ``lifecycle``：独立的耗时诊断记录
- ``send``：最终发送、失败处理、Agent 动作
"""

from __future__ import annotations
import queue
import threading
from bridge.reply import ReplyType
from channel.chat_channel import ChatChannel
from channel.wechat_desktop.config import load_wechat_desktop_config
from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.pipeline.lifecycle import LifecycleRecorder
from channel.wechat_desktop.pipeline.fifo_queue import WechatReplyQueue
from channel.wechat_desktop.pipeline.materialize import WechatDesktopMaterializeMixin
from channel.wechat_desktop.pipeline.policy import WechatDesktopPolicy
from channel.wechat_desktop.pipeline.prompts import _normalize_auto_reply_text
from channel.wechat_desktop.pipeline.reply import WechatDesktopReplyMixin
from channel.wechat_desktop.pipeline.scan import WechatDesktopScanMixin
from channel.wechat_desktop.pipeline.send import WechatDesktopSendMixin
from channel.wechat_desktop.storage.service import get_wechat_desktop_service
from channel.wechat_desktop.backend import create_wechat_desktop_backend
from common.log import file_logger, logger
from common.singleton import singleton


# 通道工厂入口；纯函数直接从 prompts 模块导入。
__all__ = ["WechatDesktopChannel"]


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
        self._runtime_lock = threading.RLock()
        self._runtime_state = "stopped"
        # 最终回复必须全局串行；队列项同时固化入队时的上下文快照。
        self._reply_queue = WechatReplyQueue(
            capacity=self.config["reply_queue_capacity"],
            max_wait_seconds=self.config["reply_queue_max_wait_seconds"],
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
        self._materialize_queue: queue.Queue = queue.Queue(maxsize=self.config["materialize_queue_capacity"])
        self._materialization_active = threading.Event()
        self._queue_thread = None
        self._scan_thread = None
        self._materialize_thread = None

        # 生命周期表只用于诊断耗时，不参与消息业务判断。
        self._lifecycle = LifecycleRecorder()
        self._failure_notice_lock = threading.RLock()
        self._service = get_wechat_desktop_service()
        self._store = self._service.store
        # 来源读取器从已提交账本恢复游标，因此后端必须在账本创建之后注入。
        self._driver = create_wechat_desktop_backend(self.config, store=self._store)
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
        self._lifecycle.start(event)

    def _mark_lifecycle(self, event_ids, stage: str, **metrics):
        self._lifecycle.mark(event_ids, stage, **metrics)

    def _finish_lifecycle(self, event_ids, terminal: str):
        self._lifecycle.finish(event_ids, terminal)

    def _live_workers(self):
        workers = [self._scan_thread, self._materialize_thread, self._queue_thread]
        alive = [worker.name for worker in workers if worker and worker.is_alive()]
        return alive

    def startup(self):
        """旧运行完全结束后才允许创建新一代队列和停止信号。"""
        with self._runtime_lock:
            if self._runtime_state in {"starting", "running"} or self._live_workers():
                raise RuntimeError("wechat_desktop is running or still stopping")
            self._runtime_state = "starting"
            self._stop_event = threading.Event()
            stop_event = self._stop_event
            try:
                self._start_workers()
                self._runtime_state = "running"
            except Exception:
                self.stop()
                raise
        while not stop_event.wait(0.25):
            alive = self._live_workers()
            required = {"cow-wechat-scan", "cow-wechat-materialize", "cow-wechat-reply-fifo"}
            missing = required.difference(alive)
            if missing:
                self._service.update_status(last_error=f"workers stopped: {sorted(missing)}", worker_healthy=False)
                self.stop()
                break

    def _start_workers(self):
        resume = getattr(self._driver, "resume", None)
        if callable(resume):
            resume()
        recovery = self._store.recover_interrupted_events()
        self._service.update_status(event_recovery=recovery)
        self._reply_queue = WechatReplyQueue(
            capacity=self.config["reply_queue_capacity"],
            max_wait_seconds=self.config["reply_queue_max_wait_seconds"],
        )
        self._materialize_queue = queue.Queue(maxsize=self.config["materialize_queue_capacity"])
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
            worker_healthy=True,
            stopping_workers=[],
            paused=bool(self._store.get_state("paused", False)),
            shadow_mode=bool(self.config.get("shadow_mode", True)),
            auto_reply_private_blacklist=list(
                self.config["auto_reply_private_blacklist"]
            ),
            auto_reply_group_blacklist=list(
                self.config["auto_reply_group_blacklist"]
            ),
            diagnostic_logging=bool(
                self.config.get("diagnostic_logging", False)
            ),
            group_reply_mode=str(self.config.get("group_reply_mode", "at_or_prefix")),
            last_error="",
        )
        self._queue_thread.start()
        self._materialize_thread.start()
        self._scan_thread.start()
        self.report_startup_success()
        logger.info(
            "[WechatDesktop] Channel started in %s mode",
            "shadow" if self.config.get("shadow_mode", True) else "active",
        )
        self._trace(
            "00-startup",
            "backend=db_uia poll_seconds=%s group_mode=%s private_blacklist=%s group_blacklist=%s",
            self.config["db_poll_interval_seconds"],
            self.config.get("group_reply_mode"),
            list(self.config["auto_reply_private_blacklist"]),
            list(self.config["auto_reply_group_blacklist"]),
        )

    def stop(self):
        """停止接收任务；超时仍有线程存活时保留 stopping，禁止覆盖运行状态。"""
        with self._runtime_lock:
            self._runtime_state = "stopping"
            self._stop_event.set()
            self._service.set_agent_executor(None)
            queued_ids = self._reply_queue.stop()
            self._best_effort("driver_close", self._driver.close)
            timeout = self.config["worker_join_timeout_seconds"]
            pending_ids = queued_ids + self._clear_pending_private_batches()
            pending_ids.extend(self._clear_pending_materializations())
            for event_id in pending_ids:
                self._best_effort("mark_event_processed", self._store.mark_event_processed, event_id, "stopped", "channel_stopped")
            self._best_effort("finish_lifecycle", self._finish_lifecycle, pending_ids, "stopped")
            # 消费者有短轮询，不依赖往满队列里写入哨兵来唤醒。
            for worker in (self._scan_thread, self._materialize_thread, self._queue_thread):
                if worker and worker.ident is not None and worker is not threading.current_thread():
                    worker.join(timeout=timeout)
            alive = self._live_workers()
            self._runtime_state = "stopping" if alive else "stopped"
            self._service.update_status(running=False, login_status=self._runtime_state,
                                        worker_healthy=not alive, stopping_workers=alive)
