"""Strict single-consumer in-memory FIFO for WeChat reply events."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.config import DEFAULT_CONFIG


@dataclass
class ReplyQueueItem:
    event: WechatDesktopEvent
    context_history: list[dict] = field(default_factory=list)
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)
    enqueued_at: float = field(default_factory=time.monotonic)
    terminal: str = ""
    expired: bool = False
    started_at: float = 0.0
    source_event_ids: list[str] = field(default_factory=list)
    batch_id: str = ""

    def restore_context(self):
        """Restore the immutable enqueue-time context onto the consumed event."""
        self.event.history = deepcopy(self.context_history)


@dataclass(frozen=True)
class QueueEnqueueResult:
    accepted: bool
    action: str = "duplicate"

    def __bool__(self):
        return self.accepted


class WechatReplyQueue:
    """只追加的全局 FIFO；已排队和正在处理的任务不会被新消息替换。"""

    def __init__(self, *, capacity=None, max_wait_seconds=None):
        self._capacity = int(capacity if capacity is not None else DEFAULT_CONFIG["reply_queue_capacity"])
        self._max_wait = float(max_wait_seconds if max_wait_seconds is not None else DEFAULT_CONFIG["reply_queue_max_wait_seconds"])
        if self._capacity <= 0 or self._max_wait <= 0:
            raise ValueError("queue capacity and max wait must be positive")
        self._queue: queue.Queue = queue.Queue(maxsize=self._capacity)
        self._lock = threading.RLock()
        self._pending_ids: set[str] = set()
        self._active: Optional[ReplyQueueItem] = None
        self._stopped = False
        self._counts = {
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "partial": 0,
            "uncertain": 0,
            "timeout": 0,
            "stopped": 0,
            "expired": 0,
            "rejected": 0,
        }
        self._last_terminal = ""

    @staticmethod
    def _source_event_ids(event: WechatDesktopEvent) -> list[str]:
        values = event.task.source_event_ids or [event.event_id]
        return list(dict.fromkeys(str(value) for value in values if str(value)))

    def enqueue(self, event: WechatDesktopEvent) -> QueueEnqueueResult:
        source_event_ids = self._source_event_ids(event)
        with self._lock:
            if self._stopped:
                return QueueEnqueueResult(False, "stopped")
            if any(
                event_id in self._pending_ids for event_id in source_event_ids
            ):
                return QueueEnqueueResult(False)
            item = ReplyQueueItem(
                event=event,
                enqueued_at=event.task.created_at,
                context_history=deepcopy(event.history),
                source_event_ids=source_event_ids,
                batch_id=str(event.task.batch_id or event.event_id),
            )
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                self._counts["rejected"] += 1
                return QueueEnqueueResult(False, "full")
            self._pending_ids.update(source_event_ids)
            return QueueEnqueueResult(True, "added")

    def contains(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self._pending_ids

    def source_event_ids(self, token: str) -> list[str]:
        with self._lock:
            if self._active and self._active.token == token:
                return list(self._active.source_event_ids)
            return []

    def get(self, timeout: float = 0.25) -> Optional[ReplyQueueItem]:
        with self._lock:
            if self._stopped:
                return None
        try:
            item = self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        with self._lock:
            item.started_at = time.time()
            if self._stopped:
                item.expired, item.terminal = True, "stopped"
            elif time.monotonic() - item.enqueued_at >= self._max_wait:
                item.expired, item.terminal = True, "expired"
            self._active = item
        return item

    def is_active(self, token: str) -> bool:
        with self._lock:
            return bool(
                self._active
                and self._active.token == str(token or "")
                and not self._active.expired
            )

    def signal(self, token: str, terminal: str):
        with self._lock:
            if not self._active or self._active.token != str(token or ""):
                return
            if self._active.expired:
                return
            self._active.terminal = terminal or "completed"
            self._active.done.set()

    def expire(self, token: str):
        with self._lock:
            if self._active and self._active.token == str(token or ""):
                self._active.expired = True
                self._active.done.set()

    def finish(self, item: ReplyQueueItem, terminal: str):
        terminal = terminal if terminal in self._counts and terminal != "rejected" else "failed"
        with self._lock:
            if terminal == "timeout":
                item.expired = True
            item.terminal = terminal
            self._counts[terminal] += 1
            self._last_terminal = terminal
            for event_id in item.source_event_ids:
                self._pending_ids.discard(event_id)
            if self._active is item:
                self._active = None
        self._queue.task_done()

    def status(self) -> dict:
        with self._lock:
            active = self._active
            return {
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self._capacity,
                "queue_active_event": active.event.event_id if active else "",
                "queue_active_conversation": (
                    active.event.conversation_name if active else ""
                ),
                "queue_active_since": active.started_at if active else 0,
                "queue_last_terminal": self._last_terminal,
                **{f"queue_{key}": value for key, value in self._counts.items()},
            }

    def clear_pending(self) -> list[str]:
        """清空尚未开始的任务，不打断当前正在处理的回复。"""
        discarded_event_ids: list[str] = []
        with self._lock:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                discarded_event_ids.extend(item.source_event_ids)
                for event_id in item.source_event_ids:
                    self._pending_ids.discard(event_id)
                self._queue.task_done()
        return list(dict.fromkeys(discarded_event_ids))

    def stop(self) -> list[str]:
        """使当前令牌失效并返回丢弃的事件；消费者通过短轮询退出。"""
        with self._lock:
            if self._stopped:
                return []
            self._stopped = True
            if self._active:
                self._active.expired = True
                self._active.terminal = "stopped"
                self._active.done.set()
            return self.clear_pending()
