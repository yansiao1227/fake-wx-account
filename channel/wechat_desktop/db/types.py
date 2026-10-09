"""数据库读取与业务账本间的批次契约，不依赖 UIA 控件。"""

from __future__ import annotations

from dataclasses import dataclass

from channel.wechat_desktop.models import WechatDesktopEvent


RECEIPT_PHASES = frozenset({"baseline", "offline_backfill", "startup_unread", "live"})


@dataclass(frozen=True)
class SourceRecord:
    """一个已完成解析或明确过滤的原生消息行；过滤也必须推进扫描游标。"""

    stream_id: str
    local_id: int
    source_message_id: str
    event: WechatDesktopEvent | None = None
    filter_reason: str = ""
    receipt_phase: str = "live"


@dataclass(frozen=True)
class SourceCheckpoint:
    """一个来源流的连续查询结果；expected_cursor 防止跳过失败的前序批次。"""

    stream_id: str
    cursor: int
    expected_cursor: int
    baseline_high_water: int = 0
    generation: str = ""


@dataclass(frozen=True)
class SourceBatch:
    """整批提交后才能确认；batch_id 是后端 acknowledge_events 的确认身份。"""

    account_id: str
    batch_id: str
    records: tuple[SourceRecord, ...]
    checkpoints: tuple[SourceCheckpoint, ...]
