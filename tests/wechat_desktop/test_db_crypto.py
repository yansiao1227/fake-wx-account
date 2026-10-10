"""合成 SQLCipher/WAL 夹具；不读取真实微信，不包含生产密钥。"""

import hashlib
import hmac
import os
import sqlite3
import struct

import pytest
from Crypto.Cipher import AES

from channel.wechat_desktop.db.cache import EncryptedDatabaseCache
from channel.wechat_desktop.db.crypto import (
    PAGE_SIZE, RESERVE_SIZE, SQLITE_HEADER, decrypt_page, hmac_key, verify_key, verify_page, wal_checksum,
)
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.security import private_directory, protect, unprotect

FAKE_KEY = bytes(range(32))
FAKE_SALT = bytes(range(32, 48))


def plain_page(user_version=0):
    page = bytearray(PAGE_SIZE)
    page[:16] = SQLITE_HEADER
    struct.pack_into(">H", page, 16, PAGE_SIZE)
    page[18:24] = bytes((1, 1, RESERVE_SIZE, 64, 32, 32))
    for offset, value in ((24, 1), (28, 1), (40, 1), (44, 4), (56, 1), (60, user_version), (92, 1), (96, 3040000)):
        struct.pack_into(">I", page, offset, value)
    page[100] = 13  # 空 sqlite_master 叶子页。
    struct.pack_into(">H", page, 105, PAGE_SIZE - RESERVE_SIZE)
    return bytes(page)


def encrypt_page(plain, page_number=1, key=FAKE_KEY, salt=FAKE_SALT, *, explicit_salt=False):
    start = 16 if page_number == 1 else 0
    iv = hashlib.sha256(struct.pack("<I", page_number) + plain).digest()[:16]
    payload = AES.new(key, AES.MODE_CBC, iv).encrypt(plain[start:PAGE_SIZE - RESERVE_SIZE])
    prefix = (SQLITE_HEADER if explicit_salt else salt) if page_number == 1 else b""
    page = prefix + payload + iv
    checksum = hmac.new(hmac_key(key, salt), page[start:] + struct.pack("<I", page_number), hashlib.sha512).digest()
    return page + checksum


def wal_bytes(frames, salt=b"SYNTHWAL", *, magic=0x377F0682):
    header = struct.pack(">IIII", magic, 3007000, PAGE_SIZE, 0) + salt
    byteorder = "<" if magic == 0x377F0682 else ">"
    checksum = wal_checksum(header, byteorder=byteorder)
    output = header + struct.pack(">II", *checksum)
    for page, committed_pages in frames:
        metadata = struct.pack(">II", 1, committed_pages)
        checksum = wal_checksum(metadata + page, checksum, byteorder=byteorder)
        output += metadata + salt + struct.pack(">II", *checksum) + page
    return output


def create_cache(tmp_path, wal=None):
    source = tmp_path / "encrypted.db"
    source.write_bytes(encrypt_page(plain_page()))
    if wal is not None:
        (tmp_path / "encrypted.db-wal").write_bytes(wal)
    return EncryptedDatabaseCache(source, FAKE_KEY, tmp_path / "private" / "copy.db")


def version(cache):
    with cache.read() as connection:
        return connection.execute("PRAGMA user_version").fetchone()[0]


def test_page_authentication_and_decryption():
    plain = plain_page(7)
    page = encrypt_page(plain)
    assert verify_key(FAKE_KEY, page)
    assert decrypt_page(page, 1, FAKE_KEY, hmac_key(FAKE_KEY, FAKE_SALT)) == plain
    assert not verify_key(bytes(reversed(FAKE_KEY)), page)
    assert not verify_page(page, 2, hmac_key(FAKE_KEY, FAKE_SALT))


@pytest.mark.parametrize("explicit_salt", [False, True])
def test_explicit_salt_key_supports_plaintext_and_encrypted_header(explicit_salt):
    plain = plain_page(4)
    page = encrypt_page(plain, explicit_salt=explicit_salt)
    key = FAKE_KEY + FAKE_SALT
    assert verify_key(key, page)
    assert decrypt_page(page, 1, key, hmac_key(FAKE_KEY, FAKE_SALT)) == plain


@pytest.mark.parametrize("offset", [16, 100, 4016, 4095])
def test_page_tampering_fails_authentication(offset):
    page = bytearray(encrypt_page(plain_page()))
    page[offset] ^= 1
    assert not verify_key(FAKE_KEY, bytes(page))
    with pytest.raises(DatabaseReadError, match="校验失败"):
        decrypt_page(bytes(page), 1, FAKE_KEY, hmac_key(FAKE_KEY, FAKE_SALT))


def test_full_cache_and_idle_refresh(tmp_path):
    cache = create_cache(tmp_path)
    first = cache.refresh()
    assert first.healthy and first.full_rebuilds == 1
    assert first.changed_pages is None
    assert version(cache) == 0
    idle = cache.refresh()
    assert not idle.changed and idle.decrypted_pages == first.decrypted_pages
    assert idle.changed_pages == ()


@pytest.mark.parametrize("magic", [0x377F0682, 0x377F0683])
def test_committed_wal_is_visible(tmp_path, magic):
    cache = create_cache(tmp_path, wal_bytes([(encrypt_page(plain_page(9)), 1)], magic=magic))
    assert cache.refresh().healthy
    assert version(cache) == 9


def test_uncommitted_wal_is_not_visible_then_publishes_incrementally(tmp_path):
    frames = [(encrypt_page(plain_page(12)), 0)]
    cache = create_cache(tmp_path, wal_bytes(frames))
    initial = cache.refresh()
    assert initial.healthy and version(cache) == 0
    frames.append((encrypt_page(plain_page(13)), 1))
    cache.source.with_name("encrypted.db-wal").write_bytes(wal_bytes(frames))
    refreshed = cache.refresh()
    assert refreshed.healthy and version(cache) == 13
    assert refreshed.full_rebuilds == 1
    assert refreshed.incremental_refreshes == 1
    assert refreshed.decrypted_pages - initial.decrypted_pages == 1
    assert refreshed.changed_pages == (1,)


def test_incremental_commit_does_not_run_full_database_check(tmp_path, monkeypatch):
    frames = [(encrypt_page(plain_page(10)), 1)]
    cache = create_cache(tmp_path, wal_bytes(frames))
    assert cache.refresh().healthy

    def unexpected_full_check(path):
        pytest.fail("WAL 增量提交不得扫描整库")

    monkeypatch.setattr(cache, "_check", unexpected_full_check)
    for value in (20, 30):
        frames.append((encrypt_page(plain_page(value)), 1))
        cache.source.with_name("encrypted.db-wal").write_bytes(wal_bytes(frames))
        refreshed = cache.refresh()
        assert refreshed.healthy and version(cache) == value
        assert refreshed.full_rebuilds == 1
    assert refreshed.incremental_refreshes == 2


def test_full_rebuild_still_checks_database_structure(tmp_path, monkeypatch):
    cache = create_cache(tmp_path)
    check = cache._check
    checks = []

    def checked(path):
        checks.append(path)
        check(path)

    monkeypatch.setattr(cache, "_check", checked)
    assert cache.refresh().healthy
    cache.source.write_bytes(encrypt_page(plain_page(20)))
    assert cache.refresh().healthy and version(cache) == 20
    assert len(checks) == 2


def test_only_frames_before_last_commit_are_published(tmp_path):
    cache = create_cache(tmp_path, wal_bytes([
        (encrypt_page(plain_page(15)), 1), (encrypt_page(plain_page(99)), 0)]))
    assert cache.refresh().healthy and version(cache) == 15


@pytest.mark.parametrize("discarded_tail", [1, 3])
def test_rolled_back_tail_is_reused_with_same_salt_size_and_mtime(tmp_path, discarded_tail):
    initial_frames = [(encrypt_page(plain_page(10)), 1)] + [
        (encrypt_page(plain_page(90 + index)), 0) for index in range(discarded_tail)]
    cache = create_cache(tmp_path, wal_bytes(initial_frames))
    first = cache.refresh()
    assert first.healthy and version(cache) == 10
    assert cache._wal_state.committed_frames == 1
    wal = cache.source.with_name("encrypted.db-wal")
    stamp = wal.stat()
    # 第一帧改为提交，后续帧仍待提交；不能按旧 frames 跳过新提交。
    replacement = wal_bytes([(encrypt_page(plain_page(10)), 1), (encrypt_page(plain_page(20)), 1)] + [
        (encrypt_page(plain_page(90 + index)), 0) for index in range(1, discarded_tail)])
    wal.write_bytes(replacement)
    os.utime(wal, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    refreshed = cache.refresh()
    assert refreshed.healthy and version(cache) == 20
    assert refreshed.full_rebuilds == first.full_rebuilds


def synthetic_wal_index(wal, frames):
    checksum = struct.unpack(">II", wal[32 + (frames - 1) * (PAGE_SIZE + 24) + 16:32 + (frames - 1) * (PAGE_SIZE + 24) + 24])
    header = struct.pack("<IIIBBHII", 3007000, 0, 1, 1, 0, PAGE_SIZE, frames, 1)
    header += struct.pack("<II", *checksum) + wal[16:24]
    header += struct.pack("<II", *wal_checksum(header))
    return header * 2 + bytes(40)


def test_committed_wal_index_excludes_old_same_salt_rollback_tail(tmp_path):
    initial_wal = wal_bytes([(encrypt_page(plain_page(10)), 1),
                             (encrypt_page(plain_page(90)), 0), (encrypt_page(plain_page(99)), 0)])
    cache = create_cache(tmp_path, initial_wal)
    shm = cache.source.with_name("encrypted.db-shm")
    shm.write_bytes(synthetic_wal_index(initial_wal, 1))
    first = cache.refresh()
    assert first.healthy and version(cache) == 10 and cache._wal_state.frames == 1
    assert not cache.refresh().changed
    wal = cache.source.with_name("encrypted.db-wal")
    stamp = wal.stat()
    new_transaction = wal_bytes([(encrypt_page(plain_page(10)), 1), (encrypt_page(plain_page(20)), 1)])
    new_wal = new_transaction + initial_wal[len(new_transaction):]
    wal.write_bytes(new_wal)
    os.utime(wal, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    shm.write_bytes(synthetic_wal_index(new_wal, 2))
    refreshed = cache.refresh()
    assert refreshed.healthy and version(cache) == 20
    assert refreshed.decrypted_pages - first.decrypted_pages == 1
    assert refreshed.full_rebuilds == first.full_rebuilds
    assert not cache.refresh().changed


def test_wal_index_cannot_publish_an_uncommitted_boundary(tmp_path):
    wal = wal_bytes([(encrypt_page(plain_page(20)), 0)])
    cache = create_cache(tmp_path, wal)
    cache.source.with_name("encrypted.db-shm").write_bytes(synthetic_wal_index(wal, 1))
    failed = cache.refresh()
    assert failed.stale and failed.error_code == "wal_commit_boundary_invalid"
    assert not cache.path.exists()


def test_source_reopened_during_rebuild_preserves_previous_copy(tmp_path, monkeypatch):
    cache = create_cache(tmp_path)
    first = cache.refresh()
    previous = cache.path.read_bytes()
    cache.retry_attempts = 1
    cache.source.write_bytes(encrypt_page(plain_page(20)))
    check = cache._check
    def initialize_shm_after_check(path):
        check(path)
        cache.source.with_name("encrypted.db-shm").write_bytes(bytes(136))
    monkeypatch.setattr(cache, "_check", initialize_shm_after_check)
    failed = cache.refresh()
    assert failed.stale and failed.error_code == "source_changed"
    assert failed.last_success_at == first.last_success_at
    assert cache.path.read_bytes() == previous


def test_bad_checksum_keeps_previous_copy_and_marks_stale(tmp_path):
    frames = [(encrypt_page(plain_page(10)), 1)]
    cache = create_cache(tmp_path, wal_bytes(frames))
    previous = cache.refresh()
    frames.append((encrypt_page(plain_page(20)), 1))
    invalid = bytearray(wal_bytes(frames))
    invalid[-1] ^= 1
    cache.source.with_name("encrypted.db-wal").write_bytes(invalid)
    failed = cache.refresh()
    assert failed.stale and failed.error_code == "wal_checksum_failed"
    assert failed.last_success_at == previous.last_success_at
    with pytest.raises(DatabaseReadError):
        version(cache)
    connection = sqlite3.connect(cache.path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
    finally:
        connection.close()


def test_authenticated_wal_checksum_with_bad_page_hmac_is_rejected(tmp_path):
    page = bytearray(encrypt_page(plain_page(20)))
    page[100] ^= 1
    cache = create_cache(tmp_path, wal_bytes([(bytes(page), 1)]))
    assert cache.refresh().error_code == "page_hmac_failed"
    assert not cache.path.exists()


def test_wal_generation_reset_rebuilds(tmp_path):
    cache = create_cache(tmp_path, wal_bytes([(encrypt_page(plain_page(6)), 1)]))
    first = cache.refresh()
    cache.source.with_name("encrypted.db-wal").write_bytes(
        wal_bytes([(encrypt_page(plain_page(8)), 1)], salt=b"NEXT-WAL"))
    second = cache.refresh()
    assert second.healthy and version(cache) == 8
    assert second.full_rebuilds == first.full_rebuilds + 1
    assert second.generation == first.generation


def test_same_size_wal_reset_with_unchanged_mtime_rebuilds(tmp_path):
    cache = create_cache(tmp_path, wal_bytes([(encrypt_page(plain_page(6)), 1)]))
    first = cache.refresh()
    wal = cache.source.with_name("encrypted.db-wal")
    previous_stat = wal.stat()
    wal.write_bytes(wal_bytes([(encrypt_page(plain_page(8)), 1)], salt=b"NEXT-WAL"))
    os.utime(wal, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
    assert wal.stat().st_size == previous_stat.st_size
    assert wal.stat().st_mtime_ns == previous_stat.st_mtime_ns

    refreshed = cache.refresh()

    assert refreshed.healthy and version(cache) == 8
    assert refreshed.full_rebuilds == first.full_rebuilds + 1


def test_preallocated_frame_overwrite_with_unchanged_mtime_is_read(tmp_path):
    active_frames = [(encrypt_page(plain_page(5)), 1)]
    old_tail = wal_bytes([(encrypt_page(plain_page(90)), 1)], salt=b"OLD--WAL")[32:]
    cache = create_cache(tmp_path, wal_bytes(active_frames) + old_tail)
    first = cache.refresh()
    assert first.healthy and version(cache) == 5
    wal = cache.source.with_name("encrypted.db-wal")
    previous_stat = wal.stat()
    active_frames.append((encrypt_page(plain_page(7)), 1))
    wal.write_bytes(wal_bytes(active_frames))
    os.utime(wal, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
    assert wal.stat().st_size == previous_stat.st_size
    assert wal.stat().st_mtime_ns == previous_stat.st_mtime_ns

    refreshed = cache.refresh()

    assert refreshed.healthy and version(cache) == 7
    assert refreshed.full_rebuilds == first.full_rebuilds
    assert refreshed.incremental_refreshes == first.incremental_refreshes + 1
    assert refreshed.decrypted_pages == first.decrypted_pages + 1


@pytest.mark.parametrize("corruption", ["page_type", "page_size", "reserve", "cell_count", "content_offset", "child_pointer"])
def test_authenticated_structural_corruption_keeps_verified_snapshot(tmp_path, corruption):
    frames = [(encrypt_page(plain_page(10)), 1)]
    cache = create_cache(tmp_path, wal_bytes(frames))
    previous = cache.refresh()
    verified_bytes = cache.path.read_bytes()
    consumed_frames = cache._wal_state.frames
    corrupt = bytearray(plain_page(20))
    # 结构非法，但密钥、页 HMAC 和 WAL 校验和都有效。
    if corruption == "page_type":
        corrupt[100] = 0
    elif corruption == "page_size":
        struct.pack_into(">H", corrupt, 16, PAGE_SIZE * 2)
    elif corruption == "reserve":
        corrupt[20] = 0
    elif corruption == "cell_count":
        struct.pack_into(">H", corrupt, 103, PAGE_SIZE)
    elif corruption == "content_offset":
        struct.pack_into(">H", corrupt, 105, 100)
    else:
        corrupt[100] = 5
        struct.pack_into(">I", corrupt, 108, 2)
    encrypted = encrypt_page(bytes(corrupt))
    assert verify_key(FAKE_KEY, encrypted)
    frames.append((encrypted, 1))
    cache.source.with_name("encrypted.db-wal").write_bytes(wal_bytes(frames))

    failed = cache.refresh()

    assert not failed.healthy and failed.stale
    assert failed.last_success_at == previous.last_success_at
    assert failed.full_rebuilds == previous.full_rebuilds
    assert failed.incremental_refreshes == previous.incremental_refreshes
    assert failed.decrypted_pages == previous.decrypted_pages
    assert cache._wal_state.frames == consumed_frames
    assert cache.path.read_bytes() == verified_bytes
    with pytest.raises(DatabaseReadError):
        version(cache)
    with sqlite3.connect(cache.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_idle_after_complete_uncommitted_frame_does_not_decrypt(tmp_path, monkeypatch):
    cache = create_cache(tmp_path, wal_bytes([(encrypt_page(plain_page(12)), 0)]))
    first = cache.refresh()
    assert first.healthy and version(cache) == 0
    assert cache._wal_state.frames == 1

    def unexpected_decrypt(*args, **kwargs):
        pytest.fail("未变化的 WAL 不应再次解密")

    monkeypatch.setattr("channel.wechat_desktop.db.cache.decrypt_page", unexpected_decrypt)
    idle = cache.refresh()
    assert idle.healthy and not idle.changed
    assert idle.decrypted_pages == first.decrypted_pages
    assert idle.full_rebuilds == first.full_rebuilds
    assert idle.incremental_refreshes == first.incremental_refreshes
    assert version(cache) == 0


def test_frame_arriving_between_collection_and_probe_is_not_missed(tmp_path, monkeypatch):
    active_frames = [(encrypt_page(plain_page(5)), 1)]
    old_tail = wal_bytes([(encrypt_page(plain_page(90)), 1)], salt=b"OLD--WAL")[32:]
    cache = create_cache(tmp_path, wal_bytes(active_frames) + old_tail)
    assert cache.refresh().healthy and version(cache) == 5
    wal = cache.source.with_name("encrypted.db-wal")
    previous_stat = wal.stat()
    # 先触发一次刷新；新增有效帧恰好出现在本轮收集结束之后。
    os.utime(wal, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns + 1_000_000))
    before_arrival = wal.stat()
    original_collect = cache._collect_wal
    appended = False

    def collect_then_append(*args, **kwargs):
        nonlocal appended
        result = original_collect(*args, **kwargs)
        if not appended:
            appended = True
            wal.write_bytes(wal_bytes(active_frames + [(encrypt_page(plain_page(7)), 1)]))
            os.utime(wal, ns=(before_arrival.st_atime_ns, before_arrival.st_mtime_ns))
        return result

    monkeypatch.setattr(cache, "_collect_wal", collect_then_append)
    cache.refresh()
    refreshed = cache.refresh()
    assert appended
    assert refreshed.healthy and version(cache) == 7
    assert cache._wal_state.frames == 2


@pytest.mark.skipif(os.name != "nt", reason="验证 Windows SQLite WAL 字节范围锁")
def test_sqlite_wal_writer_lock_defers_refresh_and_recovers(tmp_path):
    cache = create_cache(tmp_path)
    initial = cache.refresh()
    assert initial.healthy
    verified_bytes = cache.path.read_bytes()
    # 硬链接使两个路径共享同一个 shm 文件和字节范围锁；真实 SQLite 库只含合成表。
    live_source = tmp_path / "writer.db"
    with sqlite3.connect(live_source) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE synthetic (id INTEGER)")
        writer.commit()
        os.link(str(live_source) + "-shm", str(cache.source) + "-shm")
        writer.execute("BEGIN IMMEDIATE")

        deferred = cache.refresh()

        assert not deferred.healthy and deferred.stale
        assert deferred.last_success_at == initial.last_success_at
        assert cache.path.read_bytes() == verified_bytes
        writer.rollback()
        recovered = cache.refresh()
        assert recovered.healthy and not recovered.stale
        assert version(cache) == 0


def test_checkpoint_rebuilds_and_rejects_wrong_key(tmp_path):
    cache = create_cache(tmp_path)
    assert cache.refresh().healthy
    cache.source.write_bytes(encrypt_page(plain_page(30)))
    assert cache.refresh().full_rebuilds == 2 and version(cache) == 30
    cache.source.write_bytes(encrypt_page(plain_page(40), key=bytes(reversed(FAKE_KEY))))
    assert cache.refresh().error_code == "page_hmac_failed"


def test_incomplete_tail_does_not_advance_then_completed_frame_is_read(tmp_path):
    complete = wal_bytes([(encrypt_page(plain_page(31)), 1)])
    cache = create_cache(tmp_path, complete[:-1])
    assert cache.refresh().healthy and version(cache) == 0
    cache.source.with_name("encrypted.db-wal").write_bytes(complete)
    assert cache.refresh().healthy and version(cache) == 31


def test_preallocated_old_generation_tail_is_ignored(tmp_path):
    active = wal_bytes([(encrypt_page(plain_page(5)), 1)])
    old = wal_bytes([(encrypt_page(plain_page(90)), 1)], salt=b"OLD--WAL")
    cache = create_cache(tmp_path, active + old[32:])
    assert cache.refresh().healthy and version(cache) == 5


def test_private_directory_and_dpapi_roundtrip(tmp_path):
    import os
    directory = private_directory(tmp_path / "restricted")
    assert directory.is_dir()
    if os.name == "nt":
        payload = protect(FAKE_KEY, b"synthetic-account")
        assert FAKE_KEY not in payload
        assert unprotect(payload, b"synthetic-account") == FAKE_KEY
        with pytest.raises(Exception):
            unprotect(payload, b"another-account")
