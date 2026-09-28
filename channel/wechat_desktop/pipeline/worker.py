"""可独立测试的单消费者执行器；任务异常不能破坏队列账目。"""

from dataclasses import dataclass
from threading import Event
from typing import Callable

from common.log import logger
from channel.wechat_desktop.pipeline.fifo_queue import ReplyQueueItem, WechatReplyQueue


def best_effort(label, operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except Exception:
        logger.exception("[WechatDesktop] cleanup failed: %s", label)


@dataclass
class ReplyWorker:
    queue: WechatReplyQueue
    stop_event: Event
    process: Callable[[ReplyQueueItem], str]
    on_error: Callable[[ReplyQueueItem, Exception], None]
    on_finish: Callable[[ReplyQueueItem, str], None]

    def run(self):
        while not self.stop_event.is_set():
            item = self.queue.get(timeout=0.25)
            if item is None:
                continue
            terminal = "failed"
            try:
                terminal = (item.terminal or "expired") if item.expired else self.process(item)
            except Exception as exc:
                logger.exception("[WechatDesktop] reply worker recovered from task failure")
                best_effort("worker_error", self.on_error, item, exc)
            finally:
                self.queue.finish(item, terminal)
                best_effort("worker_finish", self.on_finish, item, terminal)
