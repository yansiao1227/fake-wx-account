"""Single-consumer in-memory queue for WeChat reply events."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
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
    global_queue_task: bool = False


@dataclass(frozen=True)
class QueueEnqueueResult:
    accepted: bool
    action: str = "duplicate"
    cancel_event_id: str = ""

    def __bool__(self):
        return self.accepted


class WechatReplyQueue:
    """Global FIFO with an in-memory continuation lane for the active chat."""

    _STOP = object()

    def __init__(self, conversation_burst_limit: int = 5):
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._pending_ids: set[str] = set()
        self._pending_by_conversation: dict[str, ReplyQueueItem] = {}
        self._active: Optional[ReplyQueueItem] = None
        self._active_followups: deque[ReplyQueueItem] = deque()
        self._ready_continuations: deque[ReplyQueueItem] = deque()
        self._conversation_burst_limit = max(1, int(conversation_burst_limit))
        self._last_started_conversation = ""
        self._conversation_burst = 0
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
            if self._active and self._active.event.conversation_id == conversation_id:
                return self._enqueue_active_followup(event)
            if any(
                item.event.conversation_id == conversation_id
                for item in self._ready_continuations
            ):
                return self._enqueue_ready_followup(event)
            pending = self._pending_by_conversation.get(conversation_id)
            if pending is not None:
                old_event_id = pending.event.event_id
                pending.superseded_event_ids.append(old_event_id)
                self._pending_ids.discard(old_event_id)
                pending.event = event
                pending.token = uuid.uuid4().hex
                self._pending_ids.add(event.event_id)
                return QueueEnqueueResult(True, "replaced_pending")
            item = ReplyQueueItem(event, global_queue_task=True)
            self._pending_ids.add(event.event_id)
            self._pending_by_conversation[conversation_id] = item
            self._queue.put(item)
            return QueueEnqueueResult(True, "added")

    @staticmethod
    def _merge_private_event(
        previous: WechatDesktopEvent, current: WechatDesktopEvent
    ) -> WechatDesktopEvent:
        """Keep the newest target while retaining messages collected this turn."""
        history = list(current.history)
        previous_content = str(previous.content or "")
        if previous_content and not any(
            str(item.get("content") or "") == previous_content
            or str(item.get("content") or "").endswith(
                f": {previous_content}]"
            )
            for item in history
        ):
            history.append(
                {
                    "sender_name": previous.sender_name,
                    "content": previous_content,
                    "content_type": previous.content_type,
                }
            )
        current.history = history
        return current

    def _replace_private_followup(
        self, items: deque[ReplyQueueItem], event: WechatDesktopEvent
    ) -> QueueEnqueueResult:
        if items:
            item = items[-1]
            old_event = item.event
            item.superseded_event_ids.append(old_event.event_id)
            self._pending_ids.discard(old_event.event_id)
            item.event = self._merge_private_event(old_event, event)
            item.token = uuid.uuid4().hex
            self._pending_ids.add(event.event_id)
            return QueueEnqueueResult(True, "merged_active_private")
        item = ReplyQueueItem(event)
        items.append(item)
        self._pending_ids.add(event.event_id)
        return QueueEnqueueResult(True, "collected_active_private")

    def _enqueue_active_followup(
        self, event: WechatDesktopEvent
    ) -> QueueEnqueueResult:
        if event.is_group:
            self._active_followups.append(ReplyQueueItem(event))
            self._pending_ids.add(event.event_id)
            return QueueEnqueueResult(True, "collected_active_group")

        event = self._merge_private_event(self._active.event, event)
        result = self._replace_private_followup(self._active_followups, event)
        cancel_event_id = self._active.event.event_id
        self._active.expired = True
        self._active.terminal = "superseded"
        self._active.done.set()
        return QueueEnqueueResult(
            True,
            result.action,
            cancel_event_id,
        )

    def _enqueue_ready_followup(
        self, event: WechatDesktopEvent
    ) -> QueueEnqueueResult:
        if event.is_group:
            self._ready_continuations.append(ReplyQueueItem(event))
            self._pending_ids.add(event.event_id)
            return QueueEnqueueResult(True, "collected_active_group")

        matching = deque(
            item
            for item in self._ready_continuations
            if item.event.conversation_id == event.conversation_id
        )
        if matching:
            target = matching[-1]
            old_event = target.event
            target.superseded_event_ids.append(old_event.event_id)
            self._pending_ids.discard(old_event.event_id)
            target.event = self._merge_private_event(old_event, event)
            target.token = uuid.uuid4().hex
            self._pending_ids.add(event.event_id)
            return QueueEnqueueResult(True, "merged_active_private")
        return self._replace_private_followup(self._ready_continuations, event)

    def enqueue_many(self, events: list[WechatDesktopEvent]) -> int:
        added = 0
        for event in sorted(events, key=lambda item: item.observed_at):
            added += int(bool(self.enqueue(event)))
        return added

    def get(self, timeout: float = 0.25) -> Optional[ReplyQueueItem]:
        item = None
        from_global_queue = False
        with self._lock:
            should_yield = bool(
                self._ready_continuations
                and self._conversation_burst >= self._conversation_burst_limit
                and self._ready_continuations[0].event.conversation_id
                == self._last_started_conversation
            )
            if should_yield:
                try:
                    item = self._queue.get_nowait()
                    from_global_queue = True
                except queue.Empty:
                    pass
            if item is None and self._ready_continuations:
                item = self._ready_continuations.popleft()
        if item is None:
            try:
                item = self._queue.get(timeout=max(0.0, timeout))
                from_global_queue = True
            except queue.Empty:
                return None
        if item is self._STOP:
            return None
        with self._lock:
            item.started_at = time.time()
            self._active = item
            conversation_id = str(item.event.conversation_id)
            if from_global_queue and self._pending_by_conversation.get(conversation_id) is item:
                self._pending_by_conversation.pop(conversation_id, None)
            if conversation_id == self._last_started_conversation:
                self._conversation_burst += 1
            else:
                self._last_started_conversation = conversation_id
                self._conversation_burst = 1
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
                self._ready_continuations.extend(self._active_followups)
                self._active_followups.clear()
        if item.global_queue_task:
            self._queue.task_done()

    def status(self) -> dict:
        with self._lock:
            active = self._active
            return {
                "queue_depth": (
                    self._queue.qsize()
                    + len(self._active_followups)
                    + len(self._ready_continuations)
                ),
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
            for item in (*self._active_followups, *self._ready_continuations):
                discarded += 1
                self._pending_ids.discard(item.event.event_id)
            self._active_followups.clear()
            self._ready_continuations.clear()
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
