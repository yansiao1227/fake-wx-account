"""Daily Baidu-hot broadcast scheduler for wechat_desktop.

At a configurable local time (default 18:00), prepare the top trending item in
a worker thread, then hand the ready text to a channel callback that enqueues
per-group send tasks into the global reply FIFO.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Callable, Optional

from channel.wechat_desktop.baidu_hot import build_daily_hot_message
from common.log import logger

STATE_LAST_DATE = "daily_hot_last_date"
STATE_LAST_ERROR = "daily_hot_last_error"
STATE_LAST_STATUS = "daily_hot_last_status"


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse ``HH:MM`` into hour/minute. Raises ValueError on invalid input."""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid daily hot time: {value!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid daily hot time: {value!r}")
    return hour, minute


def is_due(
    now: datetime,
    schedule_time: str,
    last_date: str,
) -> bool:
    """Return True when local ``now`` is on/after schedule and not yet fired today."""
    try:
        hour, minute = parse_hhmm(schedule_time)
    except ValueError:
        return False
    today = now.strftime("%Y-%m-%d")
    if str(last_date or "") == today:
        return False
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= scheduled


class DailyHotScheduler:
    """Background ticker that prepares and dispatches the daily hot broadcast."""

    def __init__(
        self,
        *,
        config: dict,
        store,
        enqueue_callback: Callable[[str], int],
        is_paused: Optional[Callable[[], bool]] = None,
        tick_seconds: float = 15.0,
        now_factory: Optional[Callable[[], datetime]] = None,
        prepare_factory: Optional[Callable[..., dict]] = None,
    ):
        self.config = config
        self.store = store
        self.enqueue_callback = enqueue_callback
        self.is_paused = is_paused or (lambda: False)
        self.tick_seconds = max(1.0, float(tick_seconds or 15.0))
        self.now_factory = now_factory or datetime.now
        self.prepare_factory = prepare_factory or build_daily_hot_message

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._prepare_thread: Optional[threading.Thread] = None
        self._preparing = False

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="cow-wechat-daily-hot",
                daemon=True,
            )
            self._thread.start()
            logger.info(
                "[WechatDesktop][DailyHot] scheduler started enabled=%s time=%s tab=%s",
                self.enabled,
                self.schedule_time,
                self.tab,
            )

    def stop(self, join_timeout: float = 2.0):
        self._stop_event.set()
        thread = None
        prepare = None
        with self._lock:
            thread = self._thread
            prepare = self._prepare_thread
        if prepare and prepare is not threading.current_thread():
            prepare.join(timeout=join_timeout)
        if thread and thread is not threading.current_thread():
            thread.join(timeout=join_timeout)
        logger.info("[WechatDesktop][DailyHot] scheduler stopped")

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("daily_hot_broadcast_enabled", False))

    @property
    def schedule_time(self) -> str:
        return str(self.config.get("daily_hot_broadcast_time", "18:00") or "18:00")

    @property
    def tab(self) -> str:
        return str(
            self.config.get("daily_hot_broadcast_tab", "livelihood") or "livelihood"
        )

    @property
    def message_prefix(self) -> str:
        return str(
            self.config.get("daily_hot_broadcast_message_prefix", "📰 今日热点")
            or "📰 今日热点"
        )

    def status(self) -> dict:
        with self._lock:
            preparing = self._preparing
        return {
            "daily_hot_enabled": self.enabled,
            "daily_hot_time": self.schedule_time,
            "daily_hot_tab": self.tab,
            "daily_hot_last_date": self.store.get_state(STATE_LAST_DATE, ""),
            "daily_hot_last_error": self.store.get_state(STATE_LAST_ERROR, ""),
            "daily_hot_last_status": self.store.get_state(STATE_LAST_STATUS, ""),
            "daily_hot_preparing": preparing,
        }

    def _run_loop(self):
        while not self._stop_event.wait(self.tick_seconds):
            try:
                self._tick()
            except Exception:
                logger.exception("[WechatDesktop][DailyHot] scheduler tick failed")

    def _tick(self):
        if not self.enabled:
            return
        if bool(self.config.get("shadow_mode", True)):
            return
        if self.is_paused():
            return
        now = self.now_factory()
        last_date = str(self.store.get_state(STATE_LAST_DATE, "") or "")
        if not is_due(now, self.schedule_time, last_date):
            return
        with self._lock:
            if self._preparing:
                return
            self._preparing = True
            self._prepare_thread = threading.Thread(
                target=self._prepare_and_enqueue,
                name="cow-wechat-daily-hot-prepare",
                daemon=True,
                args=(now.strftime("%Y-%m-%d"),),
            )
            self._prepare_thread.start()

    def _prepare_and_enqueue(self, fire_date: str):
        claimed = False
        try:
            if self._stop_event.is_set():
                return
            self.store.set_state(STATE_LAST_STATUS, "preparing")
            self.store.set_state(STATE_LAST_ERROR, "")

            payload = self.prepare_factory(
                tab=self.tab,
                prefix=self.message_prefix,
            )
            if self._stop_event.is_set():
                self.store.set_state(STATE_LAST_STATUS, "stopped")
                return
            if not payload.get("ok"):
                error = str(payload.get("error") or "prepare failed")
                self.store.set_state(STATE_LAST_STATUS, "failed")
                self.store.set_state(STATE_LAST_ERROR, error)
                logger.warning(
                    "[WechatDesktop][DailyHot] prepare failed date=%s error=%s",
                    fire_date,
                    error,
                )
                return

            message = str(payload.get("message") or "").strip()
            if not message:
                self.store.set_state(STATE_LAST_STATUS, "failed")
                self.store.set_state(STATE_LAST_ERROR, "empty message")
                return

            # Claim the day only after content is ready, so transient API failures
            # can retry on the next tick within the same day.
            self.store.set_state(STATE_LAST_DATE, fire_date)
            claimed = True

            if bool(self.config.get("shadow_mode", True)) or self.is_paused():
                self.store.set_state(STATE_LAST_STATUS, "skipped")
                self.store.set_state(
                    STATE_LAST_ERROR,
                    "skipped because shadow_mode or paused after prepare",
                )
                logger.info(
                    "[WechatDesktop][DailyHot] prepared but skipped send date=%s",
                    fire_date,
                )
                return

            queued = int(self.enqueue_callback(message) or 0)
            self.store.set_state(
                STATE_LAST_STATUS,
                "queued" if queued > 0 else "no_targets",
            )
            logger.info(
                "[WechatDesktop][DailyHot] prepared and enqueued date=%s groups=%s",
                fire_date,
                queued,
            )
        except Exception as exc:
            self.store.set_state(STATE_LAST_STATUS, "failed")
            self.store.set_state(STATE_LAST_ERROR, str(exc))
            if not claimed:
                # Leave last_date untouched so the day can retry.
                pass
            logger.exception(
                "[WechatDesktop][DailyHot] prepare worker crashed date=%s",
                fire_date,
            )
        finally:
            with self._lock:
                self._preparing = False

    def wait_prepare(self, timeout: float = 5.0) -> bool:
        """Wait for an in-flight prepare worker (tests / shutdown helpers)."""
        with self._lock:
            prepare = self._prepare_thread
        if prepare is None:
            return True
        prepare.join(timeout=timeout)
        return not prepare.is_alive()
