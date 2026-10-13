"""微信写锁冲突仅在下一次有界尝试前等待，陈旧快照仍不可读取。"""

from contextlib import contextmanager

import pytest

from channel.wechat_desktop.db import cache as cache_module
from channel.wechat_desktop.db.errors import DatabaseReadError
from .test_db_crypto import create_cache, version


def test_busy_source_releases_lock_then_waits_once_and_publishes_second_attempt(tmp_path, monkeypatch):
    cache = create_cache(tmp_path)
    original_lock = cache_module.source_snapshot_lock
    attempts, delays = [], []
    lock_state = {"held": False}

    @contextmanager
    def briefly_busy(source):
        attempts.append(source)
        if len(attempts) == 1:
            raise DatabaseReadError("source_busy", "synthetic writer transaction")
        with original_lock(source) as validate_source:
            lock_state["held"] = True
            try:
                yield validate_source
            finally:
                lock_state["held"] = False

    def wait(seconds):
        assert not lock_state["held"], "等待必须发生在源锁释放后"
        delays.append(seconds)

    monkeypatch.setattr(cache_module, "source_snapshot_lock", briefly_busy)
    monkeypatch.setattr(cache_module.time, "sleep", wait)
    try:
        result = cache.refresh()
        assert result.healthy and not result.stale and result.error_code == ""
        assert len(attempts) == 2 and delays == [0.05]
        assert version(cache) == 0
    finally:
        cache.close()


@pytest.mark.parametrize("budget", [1, 3])
def test_exhausted_busy_attempts_keep_old_bytes_but_reject_stale_read(tmp_path, monkeypatch, budget):
    cache = create_cache(tmp_path)
    first = cache.refresh()
    assert first.healthy
    previous = cache.path.read_bytes()
    cache.retry_attempts = budget
    attempts, delays = [], []

    @contextmanager
    def always_busy(source):
        attempts.append(source)
        raise DatabaseReadError("source_busy", "synthetic writer transaction")
        yield  # 使错误发生在上下文进入阶段，而不是构造阶段。

    monkeypatch.setattr(cache_module, "source_snapshot_lock", always_busy)
    monkeypatch.setattr(cache_module.time, "sleep", delays.append)
    try:
        result = cache.refresh()
        assert len(attempts) == budget
        assert delays == [0.05] * (budget - 1)
        assert result.stale and not result.healthy and not result.changed
        assert result.error_code == "source_busy"
        assert result.last_success_at == first.last_success_at
        assert result.generation == first.generation
        assert cache.path.read_bytes() == previous
        with pytest.raises(DatabaseReadError, match="数据库快照不可用") as error:
            with cache.read():
                pytest.fail("锁冲突耗尽后不能读取旧副本")
        assert error.value.code == "source_busy"
    finally:
        cache.close()


@pytest.mark.parametrize("failure,expected_code", [
    (DatabaseReadError("source_changed", "synthetic changed transaction"), "source_changed"),
    (DatabaseReadError("wal_checksum_failed", "synthetic integrity failure"), "wal_checksum_failed"),
    (OSError("synthetic read failure"), "snapshot_io_failed"),
])
def test_non_busy_failures_keep_existing_immediate_retry_budget(tmp_path, monkeypatch, failure, expected_code):
    cache = create_cache(tmp_path)
    attempts = []

    @contextmanager
    def failed_lock(source):
        attempts.append(source)
        raise failure
        yield

    monkeypatch.setattr(cache_module, "source_snapshot_lock", failed_lock)
    monkeypatch.setattr(cache_module.time, "sleep",
                        lambda seconds: pytest.fail("非 source_busy 错误不得等待"))
    try:
        result = cache.refresh()
        assert len(attempts) == cache.retry_attempts
        assert result.error_code == expected_code and result.stale and not result.healthy
        assert not cache.path.exists()
    finally:
        cache.close()
