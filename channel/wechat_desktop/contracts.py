"""跨后端的发送、接收确认和目标解析契约。内部状态不依赖 UIA 标识格式。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


class SendStatus(str, Enum):
    SENT = "sent"
    UNVERIFIED = "unverified"
    PARTIAL = "partial"
    UNCERTAIN = "uncertain"
    NOT_SENT = "not_sent"


@dataclass(frozen=True)
class SendResult(Mapping):
    """一次发送的最终观测；所有状态均禁止自动重放原发送。

    sent：全部气泡已验证；unverified：动作已完成但气泡未全部验证；
    partial：部分气泡已验证后中断；uncertain：可能提交但无法判定；
    not_sent：明确未提交。Mapping 仅用于旧调用方和 JSON 边界兼容。
    """

    status: SendStatus
    message: str = ""
    chunks: int = 1
    submitted_chunks: int = 0
    verified_chunks: int = 0
    accepted_by: str = ""
    observation: dict = field(default_factory=dict)
    chunk_results: tuple = ()

    @property
    def terminal(self) -> str:
        return {
            SendStatus.SENT: "completed", SendStatus.UNVERIFIED: "uncertain",
            SendStatus.PARTIAL: "partial", SendStatus.UNCERTAIN: "uncertain",
            SendStatus.NOT_SENT: "failed",
        }[self.status]

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "success": self.status in {SendStatus.SENT, SendStatus.UNVERIFIED},
            "verified": self.status == SendStatus.SENT,
            "retryable": False,
            "message": self.message, "chunks": self.chunks,
            "submitted_chunks": self.submitted_chunks, "verified_chunks": self.verified_chunks,
            "accepted_by": self.accepted_by,
            "verification": "outgoing_uia_bubble" if self.status == SendStatus.SENT else "unverified",
            "observation": dict(self.observation), "chunk_results": list(self.chunk_results),
        }

    def __getitem__(self, key):
        return self.to_dict()[key]

    def __iter__(self):
        return iter(self.to_dict())

    def __len__(self):
        return len(self.to_dict())

    @classmethod
    def from_backend(cls, value) -> SendResult:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls(SendStatus.UNCERTAIN, "invalid backend result; do not replay")
        status = value.get("status")
        if status == "sent" and (value.get("success") is False or value.get("verified") is False):
            return cls(SendStatus.UNCERTAIN, "contradictory backend result; do not replay")
        if status not in {item.value for item in SendStatus}:
            if value.get("success") is True:
                status = "sent" if value.get("verified") is True else "unverified"
            else:
                # 单独 success=False 不能证明没有点击过发送按钮。
                status = "uncertain"
        try:
            chunks = max(1, int(value.get("chunks", 1)))
            submitted = max(0, int(value.get("submitted_chunks", chunks if status in {"sent", "unverified"} else 0)))
            verified = max(0, int(value.get("verified_chunks", chunks if status == "sent" else 0)))
            if verified > submitted or submitted > chunks or (status == "not_sent" and submitted):
                raise ValueError("inconsistent chunk counts")
            if status == "sent" and verified != chunks:
                raise ValueError("sent requires verification of all chunks")
            return cls(SendStatus(status), str(value.get("message", "")), chunks, submitted, verified,
                       str(value.get("accepted_by", "")), dict(value.get("observation") or {}),
                       tuple(value.get("chunk_results") or ()))
        except (ValueError, TypeError):
            return cls(SendStatus.UNCERTAIN, "inconsistent backend result; do not replay")


@dataclass(frozen=True)
class ConversationTarget:
    conversation_id: str
    display_name: str


class TargetStatus(str, Enum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    STALE = "stale"


@dataclass(frozen=True)
class TargetResolution:
    status: TargetStatus
    target: ConversationTarget | None = None
    reason: str = ""


@dataclass(frozen=True)
class EventReceipt:
    observed_event_id: str
    canonical_event_id: str
    accepted: bool
    state: str


EVENT_TERMINALS = frozenset({
    "completed", "skipped", "failed", "partial", "uncertain", "timeout",
    "stopped", "expired", "rejected", "interrupted", "observed", "cached",
})
