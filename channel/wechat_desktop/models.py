from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


UNKNOWN_SENDER_NAME = "unknown"
DEFAULT_SELF_SENDER_NAME = "自己"


@dataclass(frozen=True)
class OwnerInfo:
    """当前登录微信账号的信息。"""

    nick_name: str
    wx_id: str = ""
    source: str = "unknown"


@dataclass(frozen=True)
class ConversationInfo:
    """会话列表中一行可稳定读取的信息，不保存 UIA 控件对象。"""

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
    """当前聊天页头部信息，用于确认会话名称和群聊类型。"""

    title: str
    header_type: str = "unknown"
    chat_number: int = 1


@dataclass(frozen=True)
class UiaReferencedMessage:
    """引用消息的一层快照；引用只解析一层，防止递归读取 UI。"""

    sender_name: str
    content: str
    message_type: str = "text"
    file_path: str = ""
    resolved: bool = False
    degraded: bool = False
    strategy: str = ""
    original_content: str = ""
    url: str = ""
    platform: str = ""
    fetched_content: str = ""
    fetch_status: str = ""
    knowledge_content: str = ""
    knowledge_status: str = ""


@dataclass(frozen=True)
class UiaChatMessage:
    """从微信消息列表读取的、与具体 UIA 控件解耦的消息快照。"""

    sender_name: str
    content: str
    message_type: str = "text"
    direction: str = "unknown"
    runtime_id: str = ""
    bounds: Optional[Tuple[int, int, int, int]] = None
    file_path: str = ""
    stable_id: str = ""
    reference: Optional[UiaReferencedMessage] = None


@dataclass
class WechatDesktopEvent:
    """微信后端交给通道层的统一事件模型。"""

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
    reference: Dict[str, Any] = field(default_factory=dict)
    target_key: str = ""
    message_runtime_id: str = ""
    message_stable_id: str = ""
    content_signature: str = ""
    session_unread_count: int = 0
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
    """发送前复核结果，可携带替代的新目标事件。"""

    valid: bool
    reason: str = ""
    replacement_event: Optional[WechatDesktopEvent] = None

    def __iter__(self):
        # 保留旧的 ``valid, reason = ...`` 解包方式，避免调用方迁移时破坏兼容性。
        yield self.valid
        yield self.reason
