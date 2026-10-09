"""微信会话标题的纯函数规则；不依赖数据库或 UI 自动化。"""

from __future__ import annotations

import re

# WeChat group headers often append the member count, e.g. "项目群(9)" / "项目群（9）".
# Session list AutomationId usually keeps the bare name (session_item_项目群).
_MEMBER_COUNT_SUFFIX = re.compile(
    r"^(?P<base>.*?)[\s\u00a0]*[（(](?P<count>\d+)[）)]\s*$"
)


def strip_member_count_suffix(title: str) -> str:
    """Remove a trailing ``(n)`` / ``（n）`` member-count suffix from a chat title."""
    value = str(title or "").strip()
    if not value:
        return ""
    match = _MEMBER_COUNT_SUFFIX.match(value)
    if not match:
        return value
    base = str(match.group("base") or "").strip()
    return base or value


def conversation_titles_match(left: str, right: str) -> bool:
    """Compare chat titles, allowing optional group member-count suffixes.

    Exact match wins. Otherwise compare after stripping a trailing ``(n)`` /
    ``（n）`` from either side (group detail headers often include the count;
    session rows / config usually do not).
    """
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return strip_member_count_suffix(a) == strip_member_count_suffix(b)


__all__ = ["strip_member_count_suffix", "conversation_titles_match"]
