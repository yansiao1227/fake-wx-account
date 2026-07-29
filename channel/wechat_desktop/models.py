from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class OwnerInfo:
    nick_name: str
    wx_id: str = ""
    source: str = "unknown"


@dataclass(frozen=True)
class ConversationInfo:
    conversation_title: str
    is_do_not_disturb: bool = False
    is_top: bool = False
    not_read_number: int = 0
    mentions_self: Optional[bool] = None
    row_signature: str = ""
    automation_id: str = ""
    runtime_id: str = ""
    row_index: int = -1
    preview_sender: str = ""
    preview_has_sender_prefix: bool = False


@dataclass(frozen=True)
class HeaderInfo:
    title: str
    header_type: str = "unknown"
    chat_number: int = 1


@dataclass(frozen=True)
class UiaChatMessage:
    sender_name: str
    content: str
    message_type: str = "text"
    direction: str = "unknown"
    runtime_id: str = ""
    bounds: Optional[Tuple[int, int, int, int]] = None
    file_path: str = ""


@dataclass
class WechatDesktopEvent:
    kind: str
    conversation_id: str
    conversation_name: str
    sender_id: str
    sender_name: str
    content_type: str
    content: str
    direction: str = "incoming"
    is_group: bool = False
    is_at: bool = False
    source_type: str = "unknown"
    evidence_path: str = ""
    bounds: Optional[Tuple[int, int, int, int]] = None
    history: List[Dict[str, Any]] = field(default_factory=list)
    target_key: str = ""
    observed_at: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def fingerprint(self) -> str:
        fingerprint_content = getattr(self, "_fingerprint_content", self.content)
        if self.content_type == "image" and os.path.isfile(str(self.content or "")):
            digest = hashlib.sha256()
            with open(self.content, "rb") as image_file:
                for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                    digest.update(chunk)
            fingerprint_content = digest.hexdigest()
        stable = {
            "kind": self.kind,
            "conversation_id": self.conversation_id,
            "sender_id": self.sender_id,
            "content_type": self.content_type,
            "content": fingerprint_content,
            "direction": self.direction,
            "source_type": self.source_type,
            "bounds": self.bounds,
        }
        return hashlib.sha256(
            json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReplyTargetValidation:
    valid: bool
    reason: str = ""
    replacement_event: Optional[WechatDesktopEvent] = None

    def __iter__(self):
        # Preserve the existing ``valid, reason = ...`` call pattern.
        yield self.valid
        yield self.reason
