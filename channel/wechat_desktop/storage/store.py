from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.contracts import EventReceipt, EVENT_TERMINALS, SendResult

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
        self.evidence_dir = Path(path).resolve().parent / "wechat_evidence"
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
                CREATE TABLE IF NOT EXISTS broadcast_jobs (
                    fire_date TEXT NOT NULL,
                    target TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(fire_date, target)
                );
                CREATE TABLE IF NOT EXISTS managed_evidence (
                    path TEXT PRIMARY KEY,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_runs (
                    event_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    event_ids TEXT NOT NULL,
                    target TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
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
            columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
            if "managed_evidence_path" not in columns:
                # 旧 evidence_path 的所有权未知，不能自动当作可删除的通道副本。
                db.execute("ALTER TABLE events ADD COLUMN managed_evidence_path TEXT NOT NULL DEFAULT ''")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_managed_evidence ON events(managed_evidence_path) "
                "WHERE managed_evidence_path != ''"
            )
            delivery_columns = {row["name"] for row in db.execute("PRAGMA table_info(deliveries)")}
            if "dedupe_key" not in delivery_columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN dedupe_key TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_dedupe ON deliveries(dedupe_key) WHERE dedupe_key != ''")

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

    def receive_event(self, event: WechatDesktopEvent) -> EventReceipt:
        """原子登记事件与接收状态；重复快照返回原事件身份，不再次投递 Agent。"""
        with self._lock:
            db = self._connect()
            # record_event 的兼容入口保留独立事务；接收使用 SAVEPOINT 覆盖两个写入。
            db.execute("SAVEPOINT receive_event")
            try:
                fingerprint = event.fingerprint()
                row = db.execute("SELECT event_id FROM events WHERE event_id=? OR fingerprint=?",
                                 (event.event_id, fingerprint)).fetchone()
                if row is not None:
                    run = db.execute("SELECT state FROM event_runs WHERE event_id=?", (row["event_id"],)).fetchone()
                    receipt = EventReceipt(event.event_id, row["event_id"], False, run["state"] if run else "legacy")
                else:
                    db.execute(
                        "INSERT INTO events(event_id,fingerprint,kind,conversation_id,conversation_name,"
                        "sender_id,sender_name,content_type,content,evidence_path,observed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (event.event_id, fingerprint, event.kind, event.conversation_id, event.conversation_name,
                         event.sender_id, event.sender_name, event.content_type, event.content, event.evidence_path, event.observed_at),
                    )
                    db.execute("INSERT INTO event_runs VALUES (?, 'received', '', ?)", (event.event_id, time.time()))
                    receipt = EventReceipt(event.event_id, event.event_id, True, "received")
                db.execute("RELEASE receive_event")
                return receipt
            except Exception:
                db.execute("ROLLBACK TO receive_event")
                db.execute("RELEASE receive_event")
                raise

    def set_event_state(self, event_ids: Iterable[str], state: str, reason: str = ""):
        if state not in EVENT_TERMINALS | {"received", "queued", "running", "sending"}:
            raise ValueError(f"unknown event state: {state}")
        with self._lock, self._connect() as db:
            for event_id in dict.fromkeys(event_ids):
                row = db.execute("SELECT state FROM event_runs WHERE event_id=?", (event_id,)).fetchone()
                if row and row["state"] in EVENT_TERMINALS:
                    continue  # 迟到回调不能把终态改回运行态或改写超时结果。
                ranks = {"received": 0, "queued": 1, "running": 2, "sending": 3}
                if row and state in ranks and ranks.get(row["state"], -1) > ranks[state]:
                    continue
                db.execute(
                    "INSERT INTO event_runs(event_id,state,reason,updated_at) SELECT event_id,?,?,? FROM events WHERE event_id=? "
                    "ON CONFLICT(event_id) DO UPDATE SET state=excluded.state,reason=excluded.reason,updated_at=excluded.updated_at",
                    (state, reason, time.time(), event_id),
                )
                if state in EVENT_TERMINALS:
                    db.execute("UPDATE events SET processed_at=? WHERE event_id=?", (time.time(), event_id))

    def event_state(self, event_id: str) -> dict:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM event_runs WHERE event_id=?", (event_id,)).fetchone()
            return dict(row) if row else {}

    def begin_delivery(self, event_ids: list[str], target: str, content_hash: str) -> str:
        delivery_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO deliveries(delivery_id,event_ids,target,content_hash,status,result,updated_at) VALUES (?,?,?,?, 'sending', '{}', ?)",
                       (delivery_id, json.dumps(event_ids), target, content_hash, time.time()))
        return delivery_id

    def claim_delivery(self, event_ids: list[str], target: str, content_hash: str, *, retry_not_sent: bool = False) -> tuple[str, SendResult | None]:
        """同一入站任务的同一输出只提交一次，日志不完整也不自动重放。"""
        ids = json.dumps(sorted(set(event_ids)))
        key = hashlib.sha256(json.dumps([ids, target, content_hash]).encode()).hexdigest() if event_ids else ""
        delivery_id = uuid.uuid4().hex
        with self._lock, self._connect() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO deliveries(delivery_id,event_ids,target,content_hash,status,result,updated_at,dedupe_key) "
                "VALUES (?,?,?,?, 'sending', '{}', ?, ?)",
                (delivery_id, ids, target, content_hash, time.time(), key),
            )
            if inserted.rowcount:
                return delivery_id, None
            row = db.execute("SELECT delivery_id,result,status FROM deliveries WHERE dedupe_key=?", (key,)).fetchone()
            if retry_not_sent and row["status"] == "not_sent":
                db.execute("UPDATE deliveries SET status='sending',result='{}',updated_at=? WHERE delivery_id=?",
                           (time.time(), row["delivery_id"]))
                return row["delivery_id"], None
            return row["delivery_id"], SendResult.from_backend(json.loads(row["result"]))

    def finish_delivery(self, delivery_id: str, result: SendResult):
        with self._lock, self._connect() as db:
            db.execute("UPDATE deliveries SET status=?,result=?,updated_at=? WHERE delivery_id=?",
                       (result.status.value, json.dumps(result.to_dict(), ensure_ascii=False), time.time(), delivery_id))

    def delivery_outcome(self, event_ids: list[str], fallback: str) -> str:
        """超时/退出时发送可能仍在另一线程，保守地保留不确定性。"""
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT event_ids,status FROM deliveries WHERE status IN ('sending','unverified','partial','uncertain')").fetchall()
        statuses = {row["status"] for row in rows if set(json.loads(row["event_ids"])).intersection(event_ids)}
        if statuses.intersection({"sending", "unverified", "uncertain"}):
            return "uncertain"
        return "partial" if "partial" in statuses else fallback

    def recover_interrupted_events(self) -> dict:
        """只在旧工作线程全部退出后调用；普通消息不自动恢复执行。"""
        counts = {"interrupted": 0, "uncertain": 0}
        with self._lock:
            with self._connect() as db:
                rows = db.execute("SELECT event_id FROM event_runs WHERE state IN ('received','queued','running','sending')").fetchall()
                db.execute("UPDATE deliveries SET status='uncertain',updated_at=? WHERE status='sending'", (time.time(),))
            for row in rows:
                state = self.delivery_outcome([row["event_id"]], "interrupted")
                self.set_event_state([row["event_id"]], state, "restart_no_replay")
                counts[state] = counts.get(state, 0) + 1
        return counts

    def mark_event_processed(self, event_id: str, terminal: str = "skipped", reason: str = ""):
        self.set_event_state([event_id], terminal, reason)
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE events SET processed_at=? WHERE event_id=?",
                (time.time(), event_id),
            )

    def set_event_evidence(self, event_id: str, source: str, managed_path: str):
        """只登记通道目录中的副本；源文件路径仅用于追溯，永不负责删除。"""
        path = Path(managed_path).resolve()
        if path.parent != self.evidence_dir.resolve():
            raise ValueError("managed evidence must be inside the channel evidence directory")
        with self._lock, self._connect() as db:
            updated = db.execute(
                "UPDATE events SET evidence_path=?, managed_evidence_path=? WHERE event_id=?",
                (source, str(path), event_id),
            )
            if updated.rowcount != 1:
                raise ValueError("cannot attach evidence to an unknown event")
            db.execute(
                "INSERT OR IGNORE INTO managed_evidence(path, created_at) VALUES (?, ?)",
                (str(path), time.time()),
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
            # 有稳定事件身份时允许用户重复说同一句话；仅基线导入按内容去重。
            if not source_event_id:
                duplicate = db.execute(
                    """
                    SELECT 1 FROM conversation_history
                     WHERE conversation_id=? AND sender_name=? AND direction=?
                       AND content_type=? AND content=? AND created_at BETWEEN ? AND ?
                     LIMIT 1
                    """,
                    (
                        str(conversation_id),
                        str(sender_name or ""),
                        str(direction or "incoming"),
                        str(content_type),
                        content,
                        timestamp - 300,
                        timestamp + 300,
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
                 WHERE source_event_id = ''
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

    def allow_rate(self, per_minute: int, per_hour: int, kind: str = "send", *, units: int = 1) -> bool:
        if units < 1:
            return False
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
            if minute_count + units > int(per_minute) or hour_count + units > int(per_hour):
                return False
            db.executemany(
                "INSERT INTO rate_events(created_at, kind) VALUES (?, ?)",
                [(now, kind)] * units,
            )
            return True

    def cleanup(
        self,
        retention_days: int = 7,
        history_retention_days: int = 90,
    ):
        cutoff = time.time() - max(1, int(retention_days)) * 86400
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM events WHERE observed_at < ?", (cutoff,))
            history_cutoff = time.time() - max(
                1, int(history_retention_days)
            ) * 86400
            db.execute(
                "DELETE FROM conversation_history WHERE created_at < ?",
                (history_cutoff,),
            )
            # 持有存储锁直到删除完成，避免另一个事件刚登记共享副本就被删除。
            rows = db.execute(
                "SELECT path FROM managed_evidence WHERE path NOT IN "
                "(SELECT managed_evidence_path FROM events WHERE managed_evidence_path != '')"
            ).fetchall()
            for row in rows:
                path = Path(row["path"])
                # 即便旧数据库被错误写入外部路径，也不越过目录边界删除。
                if path.resolve().parent != self.evidence_dir.resolve():
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    # 保留登记，下一轮继续清理，避免永久遗留副本。
                    continue
                db.execute("DELETE FROM managed_evidence WHERE path=?", (row["path"],))


    def ensure_broadcast_job(self, fire_date: str, target: str, message: str) -> dict:
        with self._lock, self._connect() as db:
            db.execute("INSERT OR IGNORE INTO broadcast_jobs(fire_date,target,message,status,updated_at) VALUES (?,?,?,'pending',?)",
                       (fire_date, target, message, time.time()))
            return dict(db.execute("SELECT * FROM broadcast_jobs WHERE fire_date=? AND target=?", (fire_date, target)).fetchone())

    def set_broadcast_status(self, fire_date: str, target: str, status: str, *, expected: tuple[str, ...] = ()) -> bool:
        with self._lock, self._connect() as db:
            sql = "UPDATE broadcast_jobs SET status=?, updated_at=? WHERE fire_date=? AND target=?"
            params = [status, time.time(), fire_date, target]
            if expected:
                sql += " AND status IN (" + ",".join("?" for _ in expected) + ")"
                params.extend(expected)
            return db.execute(sql, params).rowcount == 1

    def has_pending_broadcasts(self, fire_date: str) -> bool:
        with self._lock, self._connect() as db:
            return db.execute("SELECT 1 FROM broadcast_jobs WHERE fire_date=? AND status IN ('pending','queued') LIMIT 1", (fire_date,)).fetchone() is not None

    def broadcast_status_counts(self, fire_date: str) -> dict[str, int]:
        with self._lock, self._connect() as db:
            return {row["status"]: row["count"] for row in db.execute(
                "SELECT status, COUNT(*) AS count FROM broadcast_jobs WHERE fire_date=? GROUP BY status",
                (fire_date,),
            )}
