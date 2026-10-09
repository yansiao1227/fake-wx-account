"""微信数据库只读诊断：只输出结构、计数与耗时，不输出密钥或聊天内容。"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from channel.wechat_desktop.config import DEFAULT_CONFIG
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from channel.wechat_desktop.db.discovery import DatabaseCatalog, cache_directory
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.keys import KeyProvider
from channel.wechat_desktop.db.reader import WechatDatabaseReader
from channel.wechat_desktop.db.security import private_directory
from channel.wechat_desktop.storage.store import WechatDesktopStore


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def io_counts():
    if os.name != "nt":
        return {"read_bytes": 0, "write_bytes": 0}
    class Counters(ctypes.Structure):
        _fields_ = [(field, ctypes.c_ulonglong) for field in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters)]
    counters = Counters()
    if not kernel.GetProcessIoCounters(kernel.GetCurrentProcess(), ctypes.byref(counters)):
        return {"read_bytes": 0, "write_bytes": 0}
    return {"read_bytes": counters.read_bytes, "write_bytes": counters.write_bytes}


def benchmark_uia(config):
    from channel.wechat_desktop.uia.client import WechatUiaClient
    client = WechatUiaClient(config)
    started, cpu = time.perf_counter(), time.process_time()
    try:
        # 只读当前可见消息，禁止定位/切换会话，不调用 Driver 的观察或发送。
        messages = client.get_chat_history(limit=20, ensure_conversation=False)
        emit({"stage": "uia_current_history", "healthy": True, "messages": len(messages),
              "seconds": round(time.perf_counter() - started, 3),
              "cpu_seconds": round(time.process_time() - cpu, 3),
              "ocr_enabled": bool(config.get("uia_group_sender_ocr_enabled"))})
    except Exception:
        # UIA 异常文本可能包含当前会话信息，仅输出固定诊断代码。
        emit({"stage": "uia_current_history", "healthy": False, "error_code": "uia_history_unavailable",
              "seconds": round(time.perf_counter() - started, 3)})


def shadow_probe(config, reader, base, seconds, conversation_id):
    store = WechatDesktopStore(str(private_directory(base / "validation") / "shadow-ledger.sqlite3"))
    backend = WechatDatabaseBackend(config, db_reader=reader, store=store)
    try:
        first, _ = backend.observe_events()
        if first.get("error"):
            raise DatabaseReadError(first["db_read_error_code"], "影子观察初始化失败")
        # 未读边界策略在本诊断中禁用；任何已有批次也只落账，不进入回复流水线。
        if first.get("source_batch"):
            store.receive_source_batch(first["source_batch"])
            backend.acknowledge_events([first["source_batch"].batch_id])
        before = store.get_source_checkpoints(reader.account_id)
        contacts = backend.search_contacts("", 50)["contacts"]
        if not conversation_id:
            talker = next((stream.talker for stream in reader._streams.values()
                           if reader.get_highwaters()[stream.stream_id]["cursor"] > 0), "")
            conversation_id = reader.conversation_id(talker) if talker else ""
        history_started = time.perf_counter()
        history = backend.read_chat_history(conversation_id, 20) if conversation_id else None
        emit({"stage": "database_history", "contacts_returned": len(contacts),
              "messages": history.returned_count if history else 0,
              "seconds": round(time.perf_counter() - history_started, 4),
              "conversation_id": conversation_id,
              "cursor_unchanged": before == store.get_source_checkpoints(reader.account_id)})
        initial = {name: cache.status for name, cache in reader.caches.items()}
        started, cpu, disk = time.perf_counter(), time.process_time(), io_counts()
        timings, latencies, accepted, filtered, failures = [], [], 0, 0, 0
        while time.perf_counter() - started < seconds:
            tick = time.perf_counter()
            observation, _ = backend.observe_events()
            if observation.get("error"):
                failures += 1
            elif observation.get("source_batch"):
                batch = observation["source_batch"]
                receipts = store.receive_source_batch(batch)
                accepted += sum(receipt.accepted for receipt in receipts)
                filtered += sum(record.event is None for record in batch.records)
                for record in batch.records:
                    if record.event and record.receipt_phase == "live" and record.event.native_timestamp:
                        latencies.append(max(0, time.time() - record.event.native_timestamp))
                backend.acknowledge_events([batch.batch_id])
            timings.append((time.perf_counter() - tick) * 1000)
            remaining = seconds - (time.perf_counter() - started)
            if remaining > 0:
                time.sleep(min(float(config["db_poll_interval_seconds"]), remaining))
        elapsed, cpu_elapsed, final_disk = time.perf_counter() - started, time.process_time() - cpu, io_counts()
        deltas = {key: sum(getattr(cache.status, key) - getattr(initial.get(name), key, 0)
                           for name, cache in reader.caches.items())
                  for key in ("decrypted_pages", "full_rebuilds", "incremental_refreshes")}
        emit({"stage": "shadow_observation", "seconds": round(elapsed, 3), "polls": len(timings),
              "poll_mean_ms": round(statistics.mean(timings), 3) if timings else 0,
              "poll_max_ms": round(max(timings), 3) if timings else 0,
              "cpu_seconds": round(cpu_elapsed, 3), "cpu_percent_one_core": round(cpu_elapsed / elapsed * 100, 3),
              "io_read_bytes": final_disk["read_bytes"] - disk["read_bytes"],
              "io_write_bytes": final_disk["write_bytes"] - disk["write_bytes"],
              "accepted_messages": accepted, "filtered_rows": filtered, "failed_polls": failures,
              "live_latency_mean_seconds": round(statistics.mean(latencies), 3) if latencies else None,
              "live_latency_max_seconds": round(max(latencies), 3) if latencies else None,
              "uia_initialized": backend.uia_initialized, "replies_sent": 0, **deltas})
        return failures == 0
    finally:
        # 本诊断与正式业务账本隔离。
        store._get_connection().close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--account", default="")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--schema", action="store_true")
    parser.add_argument("--shadow-seconds", type=float, default=0,
                        help="只落账、不执行 Agent/发送的后台观察时长")
    parser.add_argument("--conversation-id", default="", help="历史查询的稳定会话 ID；默认选一条有消息的会话")
    parser.add_argument("--benchmark-uia", action="store_true", help="只读当前可见 UIA 历史性能对比")
    options = parser.parse_args()
    config = dict(DEFAULT_CONFIG, db_data_dir=options.data_dir, db_account=options.account,
                  db_cache_dir=options.cache_dir, shadow_mode=True, process_startup_unread_messages=False,
                  diagnostic_logging=False)
    started = time.perf_counter()
    cpu_started = time.process_time()
    catalog = DatabaseCatalog(config)
    reader = None
    try:
        binding = catalog.resolve()
        if not catalog.validate_binding(binding):
            raise DatabaseReadError("account_binding_lost", "微信账号绑定已失效")
        emit({"stage": "account_authenticated", "account_id": binding.account_id,
              "version": binding.version, "seconds": round(time.perf_counter() - started, 3)})
        base = cache_directory(config) / binding.account_id
        provider = KeyProvider(binding, base / "keys", candidates=catalog.key_candidates)
        reader = WechatDatabaseReader(config, catalog=catalog, key_provider=provider, binding=binding)
        all_healthy = True
        for cache in reader.caches.values():
            source = cache.source
            tick = time.perf_counter()
            status = cache.refresh()
            all_healthy &= status.healthy
            record = {"stage": "database", "name": source.name, "healthy": status.healthy,
                      "error_code": status.error_code, "source_mb": round(source.stat().st_size / 1048576, 2),
                      "first_refresh_seconds": round(time.perf_counter() - tick, 3),
                      "decrypted_pages": status.decrypted_pages}
            if status.healthy:
                tick = time.perf_counter()
                idle = cache.refresh()
                record.update(idle_refresh_ms=round((time.perf_counter() - tick) * 1000, 3),
                              idle_rebuilt=idle.full_rebuilds != status.full_rebuilds,
                              idle_decrypted_pages=idle.decrypted_pages - status.decrypted_pages)
                with cache.read() as connection:
                    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                    record["tables"] = len(tables)
                    if options.schema:
                        schemas = []
                        for table in tables:
                            if table.lower() in {"contact", "name2id", "sendername2id", "sessiontable"} or table.startswith("Msg_"):
                                if table.startswith("Msg_") and any(s["kind"] == "message" for s in schemas):
                                    continue
                                columns = [row[1] for row in connection.execute(
                                    'PRAGMA table_info("' + table.replace('"', '""') + '")')]
                                schemas.append({"kind": "message" if table.startswith("Msg_") else table,
                                                "columns": columns})
                        record["schema"] = schemas
            emit(record)
        if all_healthy and options.shadow_seconds > 0:
            all_healthy &= shadow_probe(config, reader, base, options.shadow_seconds, options.conversation_id)
        if options.benchmark_uia:
            benchmark_uia(config)
        emit({"stage": "finished", "healthy": all_healthy,
              "total_seconds": round(time.perf_counter() - started, 3),
              "cpu_seconds": round(time.process_time() - cpu_started, 3)})
        return 0 if all_healthy else 1
    except DatabaseReadError as exc:
        emit({"stage": "failed", "error_code": exc.code})
        return 1
    finally:
        if reader is not None:
            reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
