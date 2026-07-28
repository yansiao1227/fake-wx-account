"""Single-consumer in-memory queue for WeChat reply events."""

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
    superseded_event_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QueueEnqueueResult:
    accepted: bool
    action: str = "duplicate"
    cancel_event_id: str = ""

    def __bool__(self):
        return self.accepted


class WechatReplyQueue:
    """FIFO across every WeChat conversation; intentionally not persistent."""

    _STOP = object()

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._pending_ids: set[str] = set()
        self._pending_by_conversation: dict[str, ReplyQueueItem] = {}
        self._active: Optional[ReplyQueueItem] = None
        self._stopped = False
        self._counts = {
            "completed": 0,
            "skipped": 0,
            "failed": 0,
            "timeout": 0,
            "superseded": 0,
        }
        self._last_terminal = ""

    def enqueue(self, event: WechatDesktopEvent) -> QueueEnqueueResult:
        with self._lock:
            if self._stopped or event.event_id in self._pending_ids:
                return QueueEnqueueResult(False)
            conversation_id = str(event.conversation_id)
            pending = self._pending_by_conversation.get(conversation_id)
            if pending is not None:
                old_event_id = pending.event.event_id
                pending.superseded_event_ids.append(old_event_id)
                self._pending_ids.discard(old_event_id)
                pending.event = event
                pending.token = uuid.uuid4().hex
                self._pending_ids.add(event.event_id)
                return QueueEnqueueResult(True, "replaced_pending")
            if self._active and self._active.event.conversation_id == conversation_id:
                cancel_event_id = self._active.event.event_id
                self._active.expired = True
                self._active.terminal = "superseded"
                self._active.done.set()
                item = ReplyQueueItem(event)
                self._pending_ids.add(event.event_id)
                self._pending_by_conversation[conversation_id] = item
                self._queue.put(item)
                return QueueEnqueueResult(True, "superseded_active", cancel_event_id)
            item = ReplyQueueItem(event)
            self._pending_ids.add(event.event_id)
            self._pending_by_conversation[conversation_id] = item
            self._queue.put(item)
            return QueueEnqueueResult(True, "added")

    def enqueue_many(self, events: list[WechatDesktopEvent]) -> int:
        added = 0
        for event in sorted(events, key=lambda item: item.observed_at):
            added += int(bool(self.enqueue(event)))
        return added

    def get(self, timeout: float = 0.25) -> Optional[ReplyQueueItem]:
        try:
            item = self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None
        if item is self._STOP:
            return None
        with self._lock:
            item.started_at = time.time()
            self._active = item
            conversation_id = str(item.event.conversation_id)
            if self._pending_by_conversation.get(conversation_id) is item:
                self._pending_by_conversation.pop(conversation_id, None)
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
        terminal = terminal if terminal in self._counts else "completed"
        with self._lock:
            if terminal == "timeout":
                item.expired = True
            item.terminal = terminal
            self._counts[terminal] += 1
            self._last_terminal = terminal
            self._pending_ids.discard(item.event.event_id)
            for event_id in item.superseded_event_ids:
                self._pending_ids.discard(event_id)
            if self._active is item:
                self._active = None
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

    def stop(self) -> int:
        discarded = 0
        with self._lock:
            self._stopped = True
            if self._active:
                self._active.expired = True
                self._active.done.set()
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not self._STOP:
                discarded += 1
                with self._lock:
                    self._pending_ids.discard(item.event.event_id)
                    self._pending_by_conversation.pop(
                        str(item.event.conversation_id), None
                    )
            self._queue.task_done()
        self._queue.put(self._STOP)
        return discarded
