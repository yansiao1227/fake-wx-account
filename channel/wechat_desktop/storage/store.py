from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from channel.wechat_desktop.models import WechatDesktopEvent

# Schema version used to gate one-time startup migrations.
# Bump this whenever a new migration is added to _run_startup_migrations().
_SCHEMA_MIGRATION_VERSION = 2


class WechatDesktopStore:
    """Persistent event ledger, audit trail, and rate limiter.

    Connection strategy
    -------------------
    Each thread keeps one long-lived SQLite connection in ``_tls`` (thread-
    local storage).  WAL mode and foreign-key enforcement are set once per
    connection rather than on every operation.  The ``_lock`` (RLock) still
    serialises writes from the caller's perspective so that the single-writer
    WAL constraint is never violated from within this process.

    Startup migrations
    ------------------
    ``normalize_outgoing_history`` and ``deduplicate_conversation_history``
    used to run unconditionally at channel startup, doing full-table scans
    regardless of history size.  They now run **at most once** per database
    file: the ``state`` table records the last completed migration version,
    and ``_run_startup_migrations`` is a no-op when the version is current.
    """

    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _get_connection(self) -> sqlite3.Connection:
        """Return this thread's long-lived connection, creating it if needed."""
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._tls.conn = conn
        return conn

    def _connect(self) -> sqlite3.Connection:
        """Alias kept for internal callers that use ``with self._connect() as db``."""
        return self._get_connection()

    def _init_schema(self):
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    conversation_name TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    evidence_path TEXT NOT NULL DEFAULT '',
                    observed_at REAL NOT NULL,
                    processed_at REAL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    action_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    result TEXT NOT NULL,
                    content_hash TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rate_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_history (
                    history_id TEXT PRIMARY KEY,
                    source_event_id TEXT NOT NULL DEFAULT '',
                    conversation_id TEXT NOT NULL,
                    conversation_name TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'unknown',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_rate_events_created ON rate_events(created_at);
                CREATE INDEX IF NOT EXISTS idx_conversation_history
                    ON conversation_history(conversation_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_history_source_event
                    ON conversation_history(source_event_id)
                    WHERE source_event_id != '';
                """
            )

    # ------------------------------------------------------------------
    # Startup migrations (run at most once per DB file per version)
    # ------------------------------------------------------------------

    def run_startup_migrations(self, normalizer=None) -> dict:
        """Run one-time data migrations guarded by a version stamp in ``state``.

        Returns a dict with counts of changes made (all zeros when already
        up-to-date so the caller can decide whether to log anything).
        """
        result = {"normalized": 0, "deduplicated": 0}
        version_key = "schema_migration_version"
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT value FROM state WHERE key=?", (version_key,)
            ).fetchone()
            current_version = int(json.loads(row["value"])) if row else 0

        if current_version >= _SCHEMA_MIGRATION_VERSION:
            return result

        # Migration 1: normalise outgoing drafting wrappers.
        if current_version < 1 and normalizer is not None:
            result["normalized"] = self.normalize_outgoing_history(normalizer)

        # Migration 2: remove near-duplicate baseline imports.
        if current_version < 2:
            result["deduplicated"] = self.deduplicate_conversation_history()

        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE
                    SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (
                    version_key,
                    json.dumps(_SCHEMA_MIGRATION_VERSION),
                    time.time(),
                ),
            )
        return result

    # ------------------------------------------------------------------
    # Event ledger
    # ------------------------------------------------------------------

    def record_event(self, event: WechatDesktopEvent) -> bool:
        with self._lock, self._connect() as db:
            try:
                db.execute(
                    """
                    INSERT INTO events (
                        event_id, fingerprint, kind, conversation_id,
                        conversation_name, sender_id, sender_name, content_type,
                        content, evidence_path, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.event_id,
                        event.fingerprint(),
                        event.kind,
                        event.conversation_id,
                        event.conversation_name,
                        event.sender_id,
                        event.sender_name,
                        event.content_type,
                        event.content,
                        event.evidence_path,
                        event.observed_at,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def mark_event_processed(self, event_id: str):
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE events SET processed_at=? WHERE event_id=?",
                (time.time(), event_id),
            )

    def append_conversation_history(
        self,
        conversation_id: str,
        conversation_name: str,
        sender_name: str,
        direction: str,
        content_type: str,
        content: str,
        source_type: str = "unknown",
        created_at: Optional[float] = None,
        source_event_id: str = "",
    ) -> bool:
        content = str(content or "").strip()
        if not content:
            return False
        timestamp = float(created_at or time.time())
        with self._lock, self._connect() as db:
            duplicate = db.execute(
                """
                SELECT 1 FROM conversation_history
                 WHERE conversation_id=? AND sender_name=? AND direction=?
                   AND content_type=? AND content=? AND created_at>=?
                 LIMIT 1
                """,
                (
                    str(conversation_id),
                    str(sender_name or ""),
                    str(direction or "incoming"),
                    str(content_type),
                    content,
                    timestamp - 300,
                ),
            ).fetchone()
            if duplicate is not None:
                return False
            try:
                db.execute(
                    """
                    INSERT INTO conversation_history(
                        history_id, source_event_id, conversation_id,
                        conversation_name, sender_name, direction,
                        content_type, content, source_type, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        str(source_event_id or ""),
                        str(conversation_id or conversation_name),
                        str(conversation_name or conversation_id),
                        str(sender_name or ""),
                        str(direction or "incoming"),
                        str(content_type or "text"),
                        content,
                        str(source_type or "unknown"),
                        timestamp,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def append_event_history(self, event: WechatDesktopEvent) -> bool:
        return self.append_conversation_history(
            conversation_id=event.conversation_id,
            conversation_name=event.conversation_name,
            sender_name=event.sender_name,
            direction=event.direction,
            content_type=event.content_type,
            content=event.content,
            source_type=event.source_type,
            created_at=event.observed_at,
            source_event_id=event.event_id,
        )

    def has_conversation_history(self, conversation_id: str) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT 1 FROM conversation_history
                 WHERE conversation_id=?
                 LIMIT 1
                """,
                (str(conversation_id),),
            ).fetchone()
        return row is not None

    def list_conversation_history(
        self,
        conversation_id: str,
        limit: int = 30,
        exclude_source_event_id: str = "",
    ) -> List[dict]:
        with self._lock, self._connect() as db:
            safe_limit = max(1, min(int(limit), 200))
            if exclude_source_event_id:
                rows = db.execute(
                    """
                    SELECT * FROM conversation_history
                     WHERE conversation_id=? AND source_event_id != ?
                     ORDER BY created_at DESC
                     LIMIT ?
                    """,
                    (
                        str(conversation_id),
                        str(exclude_source_event_id),
                        safe_limit,
                    ),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT * FROM conversation_history
                     WHERE conversation_id=?
                     ORDER BY created_at DESC
                     LIMIT ?
                    """,
                    (str(conversation_id), safe_limit),
                ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def list_outgoing_texts_by_name(
        self, conversation_name: str, limit: int = 20
    ) -> List[str]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT content FROM conversation_history
                 WHERE conversation_name=?
                   AND direction='outgoing'
                   AND content_type='text'
                 ORDER BY created_at DESC
                 LIMIT ?
                """,
                (str(conversation_name), max(1, min(int(limit), 100))),
            ).fetchall()
        return [str(row["content"]) for row in rows if row["content"]]

    def normalize_outgoing_history(self, normalizer) -> int:
        """Rewrite previously stored outgoing drafting wrappers in place."""
        changed = 0
        with self._lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT history_id, content FROM conversation_history
                 WHERE direction='outgoing' AND content_type='text'
                """
            ).fetchall()
            for row in rows:
                normalized = str(normalizer(row["content"]) or "").strip()
                if normalized and normalized != row["content"]:
                    db.execute(
                        """
                        UPDATE conversation_history
                           SET content=?
                         WHERE history_id=?
                        """,
                        (normalized, row["history_id"]),
                    )
                    changed += 1
        return changed

    def deduplicate_conversation_history(
        self, window_seconds: int = 300
    ) -> int:
        """Remove repeated visible-baseline imports while preserving order."""
        removed = 0
        recent = {}
        with self._lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT history_id, conversation_id, sender_name, direction,
                       content_type, content, created_at
                  FROM conversation_history
                 ORDER BY created_at, history_id
                """
            ).fetchall()
            for row in rows:
                key = (
                    row["conversation_id"],
                    row["sender_name"],
                    row["direction"],
                    row["content_type"],
                    row["content"],
                )
                previous_at = recent.get(key)
                created_at = float(row["created_at"])
                if (
                    previous_at is not None
                    and created_at - previous_at <= int(window_seconds)
                ):
                    db.execute(
                        "DELETE FROM conversation_history WHERE history_id=?",
                        (row["history_id"],),
                    )
                    removed += 1
                    continue
                recent[key] = created_at
        return removed

    def audit(
        self,
        action_type: str,
        target: str,
        result: str,
        content_hash: str = "",
        detail: str = "",
    ):
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO audit(created_at, action_type, target, result, content_hash, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (time.time(), action_type, target, result, content_hash, detail),
            )

    def set_state(self, key: str, value):
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    def get_state(self, key: str, default=None):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return default

    def allow_rate(self, per_minute: int, per_hour: int, kind: str = "send") -> bool:
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM rate_events WHERE created_at < ?", (now - 3600,))
            minute_count = db.execute(
                "SELECT COUNT(*) AS c FROM rate_events WHERE kind=? AND created_at>=?",
                (kind, now - 60),
            ).fetchone()["c"]
            hour_count = db.execute(
                "SELECT COUNT(*) AS c FROM rate_events WHERE kind=? AND created_at>=?",
                (kind, now - 3600),
            ).fetchone()["c"]
            if minute_count >= int(per_minute) or hour_count >= int(per_hour):
                return False
            db.execute(
                "INSERT INTO rate_events(created_at, kind) VALUES (?, ?)",
                (now, kind),
            )
            return True

    def cleanup(
        self,
        retention_days: int = 7,
        history_retention_days: int = 90,
    ):
        cutoff = time.time() - max(1, int(retention_days)) * 86400
        evidence_paths: List[str] = []
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT evidence_path FROM events WHERE observed_at < ?",
                (cutoff,),
            ).fetchall()
            evidence_paths.extend(row["evidence_path"] for row in rows if row["evidence_path"])
            db.execute("DELETE FROM events WHERE observed_at < ?", (cutoff,))
            history_cutoff = time.time() - max(
                1, int(history_retention_days)
            ) * 86400
            db.execute(
                "DELETE FROM conversation_history WHERE created_at < ?",
                (history_cutoff,),
            )
        for path in evidence_paths:
            try:
                os.remove(path)
            except OSError:
                pass
