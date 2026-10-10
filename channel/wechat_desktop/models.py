from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field, fields
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from bridge.context import ContextType
from channel.chat_message import ChatMessage


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
    browser_content: str = ""
    browser_status: str = ""
    fetched_content: str = ""
    fetch_status: str = ""


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


@dataclass(frozen=True)
class WechatHistoryMessage:
    """微信独立聊天记录窗口中的一条只读消息快照。"""

    sender_name: str
    direction: str
    content_type: str
    content: str
    time_text: str = ""
    timestamp: Optional[str] = None
    stable_id: str = ""
    source: str = "wechat_history_dialog"
    degraded: bool = False
    message_id: str = ""
    source_message_id: str = ""
    native_timestamp: Optional[int] = None


@dataclass(frozen=True)
class WechatHistoryReadResult:
    """一次当前会话聊天记录读取的结构化结果。"""

    conversation_title: str
    conversation_type: str
    messages: List[WechatHistoryMessage]
    requested_limit: int
    returned_count: int
    has_more: Optional[bool] = None
    source: str = "wechat_history_dialog"
    history_window_opened: bool = True
    degraded: bool = False
    warnings: Tuple[str, ...] = ()
    conversation_id: str = ""
    account_id: str = ""


class WechatHistoryReadError(RuntimeError):
    """携带稳定错误码的聊天记录读取异常。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code or "uia_error")


@dataclass
class ReplyTaskMetadata:
    """流水线运行元数据，不序列化到 Agent 消息或事件指纹中。"""

    created_at: float = field(default_factory=time.monotonic)
    source_event_ids: list[str] = field(default_factory=list)
    source_validation_events: list[WechatDesktopEvent] = field(default_factory=list, repr=False)
    source_invalid: bool = False
    batch_id: str = ""
    deferred_materialization_events: list[WechatDesktopEvent] = field(default_factory=list, repr=False)
    cache_only: bool = False
    attachment_reference_required: bool = False
    preflight_attachment_notice_sent: bool = False
    failure_notice_sent: bool = False
    fingerprint_content: str | None = None
    baseline_only: bool = False


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
    attachment_status: str = ""
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
    account_id: str = ""
    source_stream_id: str = ""
    source_message_id: str = ""
    source_local_id: Optional[int] = None
    native_timestamp: Optional[int] = None
    receipt_phase: str = ""

    task: ReplyTaskMetadata = field(default_factory=ReplyTaskMetadata, repr=False, compare=False)

    def fingerprint(self) -> str:
        if self.source_message_id:
            # 原生来源身份包括分片/消息表/主键；昵称、正文和屏幕坐标均可变化。
            return hashlib.sha256(json.dumps(
                [self.account_id, self.source_message_id], ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
        fingerprint_content = self.task.fingerprint_content
        if fingerprint_content is None:
            fingerprint_content = self.content
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
        return {item.name: deepcopy(getattr(self, item.name)) for item in fields(self) if item.name != "task"}


class WechatDesktopMessage(ChatMessage):
    """把统一事件转成 ChatChannel 可消费的消息对象。"""

    def __init__(self, event: WechatDesktopEvent):
        super().__init__(event.to_dict())
        self.event = event
        self.msg_id = event.event_id
        self.create_time = event.native_timestamp if event.native_timestamp is not None else event.observed_at
        if event.content_type == "image":
            self.ctype = ContextType.IMAGE
        elif event.content_type == "file" and os.path.isfile(str(event.content or "")):
            self.ctype = ContextType.FILE
        else:
            self.ctype = ContextType.TEXT
        self.content = event.content
        self.from_user_id = event.sender_id
        self.from_user_nickname = event.sender_name
        self.to_user_id = "wechat_desktop_self"
        self.to_user_nickname = "我"
        self.other_user_id = event.conversation_id
        self.other_user_nickname = event.conversation_name
        self.is_group = event.is_group
        self.is_at = event.is_at
        self.actual_user_id = event.sender_id
        self.actual_user_nickname = event.sender_name
        self.at_list = []
        self.self_display_name = ""
        self.evidence_path = event.evidence_path


@dataclass(frozen=True)
class ReplyTargetValidation:
    """发送前复核结果，可携带替代的新目标事件。"""

    valid: bool
    reason: str = ""
    replacement_event: Optional[WechatDesktopEvent] = None
