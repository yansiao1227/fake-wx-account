"""事件生命周期诊断；只记录耗时，不决定持久化状态、重试或发送。"""

from __future__ import annotations
import threading
import time
from channel.wechat_desktop.models import WechatDesktopEvent
from common.log import logger


class LifecycleRecorder:
    def __init__(self, *, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._records: dict[str, dict] = {}
        self._scan_count = 0

    def advance_scan(self):
        """扫描计数与事件记录使用同一把锁。"""
        with self._lock:
            self._scan_count += 1

    def snapshot(self, event_id: str) -> dict:
        """返回记录副本，避免诊断读取者修改内部状态。"""
        with self._lock:
            return dict(self._records.get(event_id, {}))

    def start(self, event: WechatDesktopEvent):
        """为新观察到的事件创建端到端耗时记录。"""
        now = self._clock()
        with self._lock:
            self._records.setdefault(
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
                },
            )

    def mark(
        self,
        event_ids,
        stage: str,
        *,
        batch_id: str = "",
        attachment_resolve_count: int = 0,
        send_result: str = "",
    ):
        """记录生命周期阶段的首次到达时间和少量累计指标。"""
        now = self._clock()
        with self._lock:
            for event_id in event_ids:
                lifecycle = self._records.get(str(event_id))
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

    def finish(self, event_ids, terminal: str):
        """结束生命周期并输出从发现到发送完成的阶段耗时。"""
        rows = []
        with self._lock:
            for event_id in event_ids:
                lifecycle = self._records.pop(str(event_id), None)
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
                "scan_count=%s attachment_resolve_count=%s "
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
                lifecycle["send_result"],
            )
