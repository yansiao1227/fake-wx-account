"""Shared daily exhaustion flags and optional request counters for search tools.

Capabilities with free-tier / remote quotas (baidu_ai_search, query_rewrite, qianfan
Baidu web search, tavily) record an exhausted flag for the local calendar day
after a remote quota/billing/rate-limit signal. Subsequent calls skip that
capability without retrying the remote API.

baidu_ai_search also keeps a proactive daily request counter so the agent can degrade
to web_search before burning remote free-tier calls.

SQLite path defaults to ``{data_root}/web_search_usage.db`` and is shared by
web_search and baidu_ai_search.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from common.log import logger
from config import conf, get_data_root


# Capability ids persisted in capability_state / provider_usage.
CAP_BAIDU_AI_SEARCH = "baidu_ai_search"
CAP_QUERY_REWRITE = "query_rewrite"
CAP_QIANFAN = "qianfan"  # Baidu web_search provider
CAP_TAVILY = "tavily"
CAP_DOUBAO = "doubao"  # Volcengine 豆包搜索 / SearchInfinity

_QUOTA_HINTS = (
    "quota",
    "rate limit",
    "rate_limit",
    "too many requests",
    "insufficient",
    "balance",
    "欠费",
    "额度",
    "限流",
    "超限",
    "余额不足",
    "次数不足",
    "免费额度",
    "qps",
)


def looks_like_quota_error(
    status_code: int = 0,
    body: str = "",
    code: Any = None,
) -> bool:
    """True when HTTP/body/code signals free-tier or billing exhaustion."""
    if status_code in (402, 429):
        return True
    text = f"{code or ''} {body or ''}".lower()
    return any(hint in text for hint in _QUOTA_HINTS)


def _tools_web_search_conf() -> dict:
    tools_cfg = conf().get("tools") or {}
    if not isinstance(tools_cfg, dict):
        return {}
    block = tools_cfg.get("web_search") or {}
    return block if isinstance(block, dict) else {}


def _tools_baidu_ai_search_conf() -> dict:
    tools_cfg = conf().get("tools") or {}
    if not isinstance(tools_cfg, dict):
        return {}
    block = tools_cfg.get("baidu_ai_search") or {}
    return block if isinstance(block, dict) else {}


def resolve_usage_store_path(path: Optional[str] = None) -> Path:
    """Resolve the shared SQLite path for search capability state."""
    if path:
        return Path(path)
    for block in (_tools_baidu_ai_search_conf(), _tools_web_search_conf()):
        configured = block.get("usage_store_path")
        if configured:
            return Path(str(configured))
    return Path(get_data_root()) / "web_search_usage.db"


class SearchCapabilityState:
    """Persist per-day exhausted flags and optional request counters."""

    _schema_lock = threading.Lock()

    def __init__(self, path: Optional[str] = None):
        self.path = resolve_usage_store_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self):
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self):
        with self._schema_lock, self._connect() as db:
            # Daily exhaustion flags (primary gate for rewrite / baidu / tavily).
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS capability_state (
                    capability TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    exhausted INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (capability, period_key)
                )
                """
            )
            # Optional request counters (used by baidu_ai_search daily_limit only).
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_usage (
                    provider TEXT NOT NULL,
                    period_key TEXT NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (provider, period_key)
                )
                """
            )

    @staticmethod
    def day_key(now: Optional[datetime] = None) -> str:
        current = now or datetime.now().astimezone()
        return current.strftime("%Y-%m-%d")

    def is_exhausted(
        self, capability: str, now: Optional[datetime] = None
    ) -> bool:
        key = self.day_key(now)
        with self._connect() as db:
            row = db.execute(
                """
                SELECT exhausted FROM capability_state
                WHERE capability=? AND period_key=?
                """,
                (capability, key),
            ).fetchone()
        return bool(row and int(row["exhausted"]))

    def mark_exhausted(
        self,
        capability: str,
        reason: str = "",
        now: Optional[datetime] = None,
    ) -> None:
        key = self.day_key(now)
        updated_at = (now or datetime.now().astimezone()).isoformat()
        reason_text = (reason or "")[:500]
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO capability_state(capability, period_key, exhausted, reason, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(capability, period_key) DO UPDATE SET
                    exhausted=1,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at
                """,
                (capability, key, reason_text, updated_at),
            )
            db.commit()
        logger.info(
            "[SearchQuota] marked exhausted capability=%s day=%s reason=%s",
            capability,
            key,
            reason_text[:120],
        )

    def count(
        self, provider: str, now: Optional[datetime] = None
    ) -> int:
        """Current daily request count for a metered provider (e.g. baidu_ai_search)."""
        key = self.day_key(now)
        with self._connect() as db:
            row = db.execute(
                """
                SELECT request_count FROM provider_usage
                WHERE provider=? AND period_key=?
                """,
                (provider, key),
            ).fetchone()
        return int(row["request_count"]) if row else 0

    def reserve(
        self,
        provider: str,
        limit: Optional[int],
        now: Optional[datetime] = None,
    ) -> Tuple[bool, int]:
        """Increment before the request; return False without increment at quota.

        Used only for proactive local daily_limit on baidu_ai_search.
        """
        key = self.day_key(now)
        updated_at = (now or datetime.now().astimezone()).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT request_count FROM provider_usage
                WHERE provider=? AND period_key=?
                """,
                (provider, key),
            ).fetchone()
            current = int(row["request_count"]) if row else 0
            if limit is not None and current >= max(0, int(limit)):
                db.rollback()
                return False, current
            next_count = current + 1
            db.execute(
                """
                INSERT INTO provider_usage(provider, period_key, request_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider, period_key) DO UPDATE SET
                    request_count=excluded.request_count,
                    updated_at=excluded.updated_at
                """,
                (provider, key, next_count, updated_at),
            )
            db.commit()
            return True, next_count


# Process-wide cache so tools share one connection path per store location.
_STATE_CACHE: dict[str, SearchCapabilityState] = {}
_STATE_LOCK = threading.Lock()


def get_search_capability_state(path: Optional[str] = None) -> SearchCapabilityState:
    resolved = str(resolve_usage_store_path(path))
    with _STATE_LOCK:
        state = _STATE_CACHE.get(resolved)
        if state is None:
            state = SearchCapabilityState(resolved)
            _STATE_CACHE[resolved] = state
        return state
