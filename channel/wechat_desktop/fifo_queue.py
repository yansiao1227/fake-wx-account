"""Strict single-consumer in-memory FIFO for WeChat reply events."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from channel.wechat_desktop.models import WechatDesktopEvent


@dataclass
class ReplyQueueItem:
    event: WechatDesktopEvent
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)
    terminal: str = ""
    expired: bool = False
    started_at: float = 0.0
    global_queue_task: bool = True
    source_event_ids: list[str] = field(default_factory=list)
    batch_id: str = ""


@dataclass(frozen=True)
class QueueEnqueueResult:
    accepted: bool
    action: str = "duplicate"

    def __bool__(self):
        return self.accepted


class WechatReplyQueue:
    """Append-only global FIFO; queued and active work is never replaced."""

    _STOP = object()

    def __init__(self, conversation_burst_limit: int = 5):
        # ``conversation_burst_limit`` remains accepted for config
        # compatibility. Strict FIFO no longer has a continuation lane.
        del conversation_burst_limit
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._pending_ids: set[str] = set()
        self._active: Optional[ReplyQueueItem] = None
        self._stopped = False
        self._counts = {
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "timeout": 0,
            "stopped": 0,
            # Retained in status payloads for compatibility. New work never
            # produces this terminal state.
            "superseded": 0,
        }
        self._last_terminal = ""

    @staticmethod
    def _source_event_ids(event: WechatDesktopEvent) -> list[str]:
        values = getattr(event, "_source_event_ids", None) or [event.event_id]
        return list(dict.fromkeys(str(value) for value in values if str(value)))

    def enqueue(self, event: WechatDesktopEvent) -> QueueEnqueueResult:
        source_event_ids = self._source_event_ids(event)
        with self._lock:
            if self._stopped or any(
                event_id in self._pending_ids for event_id in source_event_ids
            ):
                return QueueEnqueueResult(False)
            item = ReplyQueueItem(
                event=event,
                source_event_ids=source_event_ids,
                batch_id=str(getattr(event, "_batch_id", "") or event.event_id),
            )
            self._pending_ids.update(source_event_ids)
            self._queue.put(item)
            return QueueEnqueueResult(True, "added")

    def enqueue_many(self, events: list[WechatDesktopEvent]) -> int:
        return sum(
            int(bool(self.enqueue(event)))
            for event in sorted(events, key=lambda item: item.observed_at)
        )

    def get(self, timeout: float = 0.25) -> Optional[ReplyQueueItem]:
        try:
            item = self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if item is self._STOP:
            self._queue.task_done()
            return None
        with self._lock:
            item.started_at = time.time()
            self._active = item
        return item

    def is_active(self, token: str) -> bool:
        with self._lock:
            return bool(
                self._active
                and self._active.token == str(token or "")
                and not self._active.expired
            )

    def is_relevant(self, token: str, conversation_id: str) -> bool:
        del conversation_id
        return self.is_active(token)

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
        terminal = terminal if terminal in self._counts else "completed"
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
        if item.global_queue_task:
            self._queue.task_done()

    def status(self) -> dict:
        with self._lock:
            active = self._active
            return {
                "queue_depth": self._queue.qsize(),
                "queue_active_event": active.event.event_id if active else "",
                "queue_active_conversation": (
                    active.event.conversation_name if active else ""
                ),
                "queue_active_since": active.started_at if active else 0,
                "queue_last_terminal": self._last_terminal,
                **{f"queue_{key}": value for key, value in self._counts.items()},
            }

    def clear_pending(self) -> list[str]:
        """Discard pending work without disturbing the active item."""
        discarded_event_ids: list[str] = []
        with self._lock:
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is self._STOP:
                    self._queue.put(self._STOP)
                    self._queue.task_done()
                    break
                discarded_event_ids.extend(item.source_event_ids)
                for event_id in item.source_event_ids:
                    self._pending_ids.discard(event_id)
                self._queue.task_done()
        return list(dict.fromkeys(discarded_event_ids))

    def stop(self) -> int:
        discarded = 0
        with self._lock:
            self._stopped = True
            if self._active:
                self._active.expired = True
                self._active.terminal = "stopped"
                self._active.done.set()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not self._STOP:
                discarded += 1
                with self._lock:
                    for event_id in item.source_event_ids:
                        self._pending_ids.discard(event_id)
            self._queue.task_done()
        self._queue.put(self._STOP)
        return discarded
