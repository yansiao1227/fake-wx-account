"""所有通道发送共享的门禁与额度预留；不持有 UIA 控件。"""

from dataclasses import dataclass
import hashlib
from typing import Callable

from channel.wechat_desktop.config import DEFAULT_CONFIG
from channel.wechat_desktop.text import split_message_text
from channel.wechat_desktop.send_control import SendCancelled, send_scope
from channel.wechat_desktop.contracts import SendResult, SendStatus


class DeliveryBlocked(SendCancelled):
    """任务失效、通道暂停或额度不足，确认尚未提交气泡。"""


@dataclass
class DeliveryService:
    config: dict
    policy: object
    backend: object
    is_paused: Callable[[], bool]
    is_stopped: Callable[[], bool]
    is_active: Callable[[str], bool]
    store: object | None = None

    def send(self, target: str, content: str, *, policy_target: str,
             is_group: bool = False, content_type: str = "text",
             interim: bool = False, token: str = "",
             source_event_ids: list[str] | None = None) -> SendResult:
        def check_cancelled():
            if self.is_stopped() or self.is_paused() or (token and not self.is_active(token)):
                raise DeliveryBlocked("send cancelled, paused or expired")

        check_cancelled()
        if not self.policy.allows_send(policy_target, is_group, content_type):
            raise DeliveryBlocked("send blocked by policy")
        limit = self.config.get("uia_text_chunk_chars", DEFAULT_CONFIG["uia_text_chunk_chars"])
        units = len(split_message_text(content, limit)) if content_type == "text" else 1
        if not self.policy.reserve_send(units):
            raise DeliveryBlocked("send rate limit exceeded")
        # 额度按气泡数一次性预留；UI 操作开始后不退款，避免不确定发送结果引发重复。
        with send_scope(check_cancelled):
            delivery_id = ""
            if self.store is not None:
                digest = hashlib.sha256(f"{content_type}:{interim}:{content}".encode("utf-8")).hexdigest()
                delivery_id, previous = self.store.claim_delivery(source_event_ids or [], target, digest)
                if previous is not None:
                    return previous
            try:
                if content_type == "image":
                    raw = self.backend.send_image(target, content)
                else:
                    method = getattr(self.backend, "send_interim_text", None) if interim else None
                    raw = (method or self.backend.send_text)(target, content)
                result = SendResult.from_backend(raw)
            except SendCancelled as exc:
                if self.store is not None:
                    self.store.finish_delivery(delivery_id, SendResult(SendStatus.NOT_SENT, str(exc)))
                raise
            except Exception as exc:
                result = SendResult(SendStatus.UNCERTAIN, str(exc))
            if self.store is not None:
                try:
                    self.store.finish_delivery(delivery_id, result)
                except Exception:
                    # 写入发送前日志已成功，保留 sending，后续同输出和重启均不重放。
                    return SendResult(SendStatus.UNCERTAIN, "delivery result could not be persisted; do not replay")
            return result
