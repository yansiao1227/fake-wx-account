"""同步发送调用链的取消检查与提交记录，使用 ContextVar 隔离并发发送者。

DeliveryService 建立作用域，UIA 在等待和不可逆操作前检查同一任务。
作用域不会保存到共享 Client 上，也不会污染扫描线程或下一次发送。
"""

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable


class SendCancelled(RuntimeError):
    """尚未提交的发送被暂停、停止或任务过期取消。"""


class SendNotSubmitted(RuntimeError):
    """执行层明确确认尚未提交任何气泡。"""


_check: ContextVar[Callable[[], None] | None] = ContextVar("wechat_send_check", default=None)


@dataclass
class SendAttempt:
    submitted: bool = False


_attempt: ContextVar[SendAttempt | None] = ContextVar("wechat_send_attempt", default=None)


@contextmanager
def send_scope(check: Callable[[], None]):
    token = _check.set(check)
    try:
        check_send_allowed()
        yield
    finally:
        _check.reset(token)


def check_send_allowed():
    check = _check.get()
    if check is not None:
        check()


@contextmanager
def track_send_attempt():
    attempt = SendAttempt()
    token = _attempt.set(attempt)
    try:
        yield attempt
    finally:
        _attempt.reset(token)


def mark_send_submitted():
    """紧贴点击/Enter 调用；动作抛错时也必须视为结果不确定。"""
    check_send_allowed()
    attempt = _attempt.get()
    if attempt is not None:
        attempt.submitted = True


def wait_send_delay(seconds: float, stop_event=None):
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        check_send_allowed()
        if stop_event is not None and stop_event.is_set():
            raise SendCancelled("WeChat UI Automation is stopping")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        delay = min(remaining, 0.05)
        if stop_event is not None:
            stop_event.wait(delay)
        else:
            time.sleep(delay)


@contextmanager
def send_lock(lock):
    while True:
        check_send_allowed()
        if lock.acquire(timeout=0.05):
            break
    try:
        check_send_allowed()
        yield
    finally:
        lock.release()
