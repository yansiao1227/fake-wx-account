#!/usr/bin/env python
"""Clear wechat_desktop daily-hot scheduler state so today can fire again.

Not a rolling 24h window: the scheduler only checks whether local calendar
``YYYY-MM-DD`` already equals ``daily_hot_last_date``, and whether now is past
``daily_hot_broadcast_time``. Deleting ``daily_hot_last_date`` re-arms today.

Examples (use the project conda interpreter)::

    D:\\Miniconda\\envs\\cowagent-wechat\\python.exe scripts\\clear_daily_hot_state.py
    D:\\Miniconda\\envs\\cowagent-wechat\\python.exe scripts\\clear_daily_hot_state.py --show
    D:\\Miniconda\\envs\\cowagent-wechat\\python.exe scripts\\clear_daily_hot_state.py --all
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keys written by DailyHotScheduler / channel.
DATE_KEY = "daily_hot_last_date"
STATUS_KEY = "daily_hot_last_status"
ERROR_KEY = "daily_hot_last_error"
ALL_KEYS = (DATE_KEY, STATUS_KEY, ERROR_KEY)


def _workspace_from_config_json() -> Path:
    """Read ``agent_workspace`` from root config.json without full app init."""
    cfg_path = ROOT / "config.json"
    workspace = "~/cow"
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("agent_workspace"):
                workspace = str(data["agent_workspace"])
        except Exception:
            pass
    return Path(workspace).expanduser()


def default_db_path() -> Path:
    """Same default as ``channel.wechat_desktop.service._default_store_path``."""
    return _workspace_from_config_json() / "wechat_desktop.sqlite3"


def resolve_db_path(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    return default_db_path().resolve()


def read_state(db_path: Path, keys: tuple[str, ...] = ALL_KEYS) -> dict[str, object]:
    if not db_path.is_file():
        raise FileNotFoundError(f"state db not found: {db_path}")
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            f"SELECT key, value, updated_at FROM state WHERE key IN ({','.join('?' for _ in keys)})",
            keys,
        ).fetchall()
    finally:
        con.close()
    out: dict[str, object] = {}
    for key, value, updated_at in rows:
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value
        out[key] = {"value": parsed, "updated_at": updated_at}
    return out


def clear_state(db_path: Path, keys: tuple[str, ...]) -> int:
    if not db_path.is_file():
        raise FileNotFoundError(f"state db not found: {db_path}")
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.execute(
            f"DELETE FROM state WHERE key IN ({','.join('?' for _ in keys)})",
            keys,
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clear daily_hot_* state so the scheduler can fire again today."
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to wechat_desktop.sqlite3 (default: <agent_workspace>/wechat_desktop.sqlite3)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Only print current daily_hot_* state; do not delete",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Also clear daily_hot_last_status and daily_hot_last_error",
    )
    parser.add_argument(
        "--date-only",
        action="store_true",
        help="Only clear daily_hot_last_date (default behavior)",
    )
    args = parser.parse_args(argv)

    db_path = resolve_db_path(args.db)
    keys = ALL_KEYS if args.all else (DATE_KEY,)

    print(f"db: {db_path}")
    try:
        before = read_state(db_path, ALL_KEYS)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not before:
        print("state: (no daily_hot_* keys)")
    else:
        print("state before:")
        for key in ALL_KEYS:
            if key in before:
                print(f"  {key} = {before[key]['value']!r}  updated_at={before[key]['updated_at']}")
            else:
                print(f"  {key} = (missing)")

    if args.show:
        return 0

    deleted = clear_state(db_path, keys)
    after = read_state(db_path, ALL_KEYS)
    print(f"deleted rows: {deleted}  keys={list(keys)}")
    if not after:
        print("state after: (no daily_hot_* keys)")
    else:
        print("state after:")
        for key in ALL_KEYS:
            if key in after:
                print(f"  {key} = {after[key]['value']!r}")
            else:
                print(f"  {key} = (missing)")
    print(
        "tip: if the channel is already running and now is past "
        "daily_hot_broadcast_time, the next ~15s tick can prepare again."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
