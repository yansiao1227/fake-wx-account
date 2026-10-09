"""合成 WAL 索引与本机 SQLite 共享内存锁验证，不读取微信账号。"""

import os
import sqlite3
import struct
from pathlib import Path

import pytest

from channel.wechat_desktop.db.crypto import PAGE_SIZE, wal_checksum
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.db.snapshot_lock import read_wal_index, source_snapshot_lock


SALT = bytes.fromhex("0123456789abcdef")


def index_header(mx_frame=7, *, salt=SALT, byteorder="<"):
    header = bytearray(48)
    struct.pack_into("<III", header, 0, 3007000, 0, 2)
    header[12] = 1
    header[13] = byteorder == ">"
    struct.pack_into("<HIIII", header, 14, PAGE_SIZE, mx_frame, 2, 123, 456)
    header[32:40] = salt
    struct.pack_into("<II", header, 40, *wal_checksum(header[:40], byteorder="<"))
    return bytes(header)


def write_index(source, header, second=None):
    Path(str(source) + "-shm").write_bytes(header + (header if second is None else second))


@pytest.mark.parametrize("byteorder", ["<", ">"])
@pytest.mark.parametrize("mx_frame", [0, 7, 0xFFFFFFFF])
def test_reads_committed_frame_from_consistent_headers(tmp_path, byteorder, mx_frame):
    source = tmp_path / "synthetic.db"
    write_index(source, index_header(mx_frame, byteorder=byteorder))
    with source_snapshot_lock(source) as validate_source:
        assert read_wal_index(source, SALT, byteorder) == mx_frame
        validate_source()


def test_absent_index_and_absent_wal_salt_are_optional(tmp_path):
    source = tmp_path / "offline.db"
    assert read_wal_index(source, SALT, "<") is None
    write_index(source, b"invalid")
    assert read_wal_index(source, b"", "<") is None


@pytest.mark.parametrize("corruption", ["short", "mismatch", "checksum", "salt", "version", "init", "page_size", "frame_byteorder"])
def test_rejects_unverified_index_headers(tmp_path, corruption):
    source = tmp_path / "synthetic.db"
    header = bytearray(index_header())
    second = None
    if corruption == "short":
        header = header[:20]
    elif corruption == "mismatch":
        second = index_header(8)
    elif corruption == "checksum":
        header[40] ^= 1
    else:
        if corruption == "salt":
            header[32] ^= 1
        elif corruption == "version":
            struct.pack_into("<I", header, 0, 3007001)
        elif corruption == "init":
            header[12] = 0
        elif corruption == "page_size":
            struct.pack_into("<H", header, 14, 8192)
        else:
            header[13] = 1
        struct.pack_into("<II", header, 40, *wal_checksum(header[:40], byteorder="<"))
    write_index(source, bytes(header), second)
    with pytest.raises(DatabaseReadError) as caught:
        read_wal_index(source, SALT, "<")
    assert caught.value.code == "wal_index_invalid"


def test_offline_validation_happens_before_publish(tmp_path):
    source = tmp_path / "offline.db"
    with source_snapshot_lock(source) as validate_source:
        Path(str(source) + "-shm").write_bytes(bytes(96))
        with pytest.raises(DatabaseReadError) as caught:
            validate_source()
        assert caught.value.code == "source_changed"
    # Context exit must not fail after the caller has already published a cache.


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows SQLite 的原生 shm 锁")
def test_real_sqlite_committed_boundary_and_shared_lock(tmp_path):
    source = tmp_path / "real-sqlite.db"
    connection = sqlite3.connect(source, timeout=0)
    try:
        connection.execute("PRAGMA page_size=4096")
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE synthetic_messages (id INTEGER PRIMARY KEY, body TEXT)")
        connection.commit()
        for number in range(3):
            connection.execute("INSERT INTO synthetic_messages VALUES (?, ?)", (number, "synthetic"))
            connection.commit()
        with source_snapshot_lock(source) as validate_source:
            wal = Path(str(source) + "-wal").read_bytes()
            byteorder = ">" if struct.unpack_from(">I", wal)[0] & 1 else "<"
            salt = wal[16:24]
            boundary = read_wal_index(source, salt, byteorder)
            frame_count = (len(wal) - 32) // (24 + PAGE_SIZE)
            commits = []
            for frame_number in range(1, frame_count + 1):
                offset = 32 + (frame_number - 1) * (24 + PAGE_SIZE)
                if (wal[offset + 8:offset + 16] == salt
                        and struct.unpack_from(">I", wal, offset + 4)[0] != 0):
                    commits.append(frame_number)
            assert boundary == commits[-1] == frame_count
            validate_source()
            # 只读共享锁必须阻止新写入，同时保留已提交读取能力。
            assert connection.execute("SELECT COUNT(*) FROM synthetic_messages").fetchone()[0] == 3
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connection.execute("INSERT INTO synthetic_messages VALUES (99, 'synthetic')")
        connection.execute("INSERT INTO synthetic_messages VALUES (99, 'synthetic')")
        connection.commit()
    finally:
        connection.close()


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows SQLite 的原生 shm 锁")
def test_snapshot_lock_is_released_when_reader_fails(tmp_path):
    source = tmp_path / "real-sqlite.db"
    connection = sqlite3.connect(source, timeout=0)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE synthetic_values (value INTEGER)")
        connection.commit()
        with pytest.raises(RuntimeError, match="synthetic_failure"):
            with source_snapshot_lock(source):
                raise RuntimeError("synthetic_failure")
        connection.execute("INSERT INTO synthetic_values VALUES (1)")
        connection.commit()
    finally:
        connection.close()
