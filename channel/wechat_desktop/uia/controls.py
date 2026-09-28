"""微信 UIA 控件标识及无状态值读取辅助函数。"""
from __future__ import annotations
import re
import struct
from typing import Iterable, Optional

SESSION_PREFIX = "session_item_"
MESSAGE_LIST_ID = "chat_message_list"
INPUT_ID = "chat_input_field"
HISTORY_LIST_ID = "chat_log_message_list"
HISTORY_WINDOW_CLASS = "mmui::SearchMsgUniqueChatWindow"
MENTION_MARKERS = ("[有人@我]", "有人@我", "[Someone mentioned me]")
HISTORY_TIME_RE = re.compile(
    r"(?P<time>(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日\s*"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}))\s*$"
)
FILE_SIZE_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(B|K|KB|M|MB|G|GB)",
    re.IGNORECASE,
)
FILE_SIZE_UNITS = {
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024 ** 2,
    "MB": 1024 ** 2,
    "G": 1024 ** 3,
    "GB": 1024 ** 3,
}
FILE_DUPLICATE_SUFFIX_RE = re.compile(r"(?:\s*\(\d+\))+$")


def _text(value) -> str:
    return str(value or "").strip()


def _encode_cf_hdrop(files: Iterable[str]) -> bytes:
    """Encode file paths as the Windows DROPFILES payload used by CF_HDROP."""

    paths = [str(path) for path in files]
    if not paths:
        raise ValueError("CF_HDROP requires at least one file path")
    if any("\0" in path for path in paths):
        raise ValueError("CF_HDROP file paths cannot contain NUL characters")

    # DROPFILES is 20 bytes: pFiles, POINT(x, y), fNC, fWide. File names
    # follow as a double-NUL-terminated UTF-16LE list when fWide is true.
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    file_list = ("\0".join(paths) + "\0\0").encode("utf-16le")
    return header + file_list


def _bounds(control) -> Optional[tuple[int, int, int, int]]:
    try:
        rect = control.BoundingRectangle
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    except Exception:
        return None


def _runtime_id(control) -> str:
    try:
        value = control.GetRuntimeId()
        if value:
            return ".".join(str(item) for item in value)
    except Exception:
        pass
    try:
        value = control.GetPropertyValue(30000)
        if value:
            return ".".join(str(item) for item in value)
    except Exception:
        pass
    return ""


