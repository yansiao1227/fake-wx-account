"""受锁保护的私有解密副本；WAL 只发布经过认证的已提交事务。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .crypto import PAGE_SIZE, decrypt_page, hmac_key, key_parts, wal_checksum
from .errors import DatabaseReadError
from .security import private_directory
from .snapshot_lock import read_wal_index, source_snapshot_lock

WAL_HEADER_SIZE = 32
WAL_FRAME_SIZE = PAGE_SIZE + 24


@dataclass(frozen=True)
class SnapshotStatus:
    healthy: bool = False
    stale: bool = True
    last_success_at: float = 0.0
    generation: str = ""
    error_code: str = "not_initialized"
    changed: bool = False
    full_rebuilds: int = 0
    incremental_refreshes: int = 0
    decrypted_pages: int = 0


@dataclass
class _WalState:
    salt: bytes = b""
    frames: int = 0
    checksum: tuple[int, int] = (0, 0)
    pending: dict[int, bytes] | None = None
    committed_frames: int = 0
    committed_checksum: tuple[int, int] = (0, 0)
    pending_headers: tuple[tuple[int, bytes], ...] = ()


def _signature(path: Path) -> tuple[int, int, int]:
    try:
        info = path.stat()
        return info.st_mtime_ns, info.st_size, info.st_ino
    except FileNotFoundError:
        return 0, 0, 0


class EncryptedDatabaseCache:
    def __init__(self, source, key: bytes, cache_path, retry_attempts: int = 3):
        self.source = Path(source)
        requested = Path(cache_path)
        # 每个读取器拥有独立副本，诊断进程和通道不能同时覆盖同一个明文库。
        self.path = requested.with_name(requested.name + "." + str(os.getpid()) + "." + uuid.uuid4().hex + ".sqlite")
        private_directory(self.path.parent)
        self.key = key
        self.retry_attempts = retry_attempts
        self._lock = threading.RLock()
        self._source_signature = None
        self._wal_signature = None
        self._probe_signature = None
        self._wal_state = _WalState(pending={})
        self.status = SnapshotStatus()

    def _wal_header(self, wal: Path) -> tuple[bytes, str, tuple[int, int]]:
        if _signature(wal)[1] == 0:
            return b"", "<", (0, 0)
        with wal.open("rb") as handle:
            header = handle.read(WAL_HEADER_SIZE)
        if len(header) != WAL_HEADER_SIZE:
            raise DatabaseReadError("wal_incomplete", "WAL 头尚未写入完成")
        magic, version, page_size = struct.unpack(">III", header[:12])
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or page_size != PAGE_SIZE:
            raise DatabaseReadError("unsupported_wal_format", "WAL 格式不受支持")
        byteorder = "<" if magic == 0x377F0682 else ">"
        checksum = wal_checksum(header[:24], byteorder=byteorder)
        if checksum != struct.unpack(">II", header[24:32]):
            raise DatabaseReadError("wal_checksum_failed", "WAL 头完整性校验失败")
        return header[16:24], byteorder, checksum

    def _probe(self, wal: Path, first_page: bytes, state=None):
        # Windows 对尚未关闭的写句柄不保证即时更新 mtime；预分配 WAL 大小也不变。
        # 观察头和下一个未消费帧头，才能发现原地重置或追加，不能只信任 stat。
        state = state or self._wal_state
        if _signature(wal)[1]:
            with wal.open("rb") as handle:
                header = handle.read(WAL_HEADER_SIZE)
                handle.seek(WAL_HEADER_SIZE + state.frames * WAL_FRAME_SIZE)
                next_frame = handle.read(24)
                pending_headers = []
                for index, _ in state.pending_headers:
                    handle.seek(WAL_HEADER_SIZE + index * WAL_FRAME_SIZE)
                    pending_headers.append(handle.read(24))
        else:
            header, next_frame = b"", b""
            pending_headers = []
        shm = Path(str(self.source) + "-shm")
        shm_header = b""
        if shm.exists():
            with shm.open("rb") as handle:
                shm_header = handle.read(96)
        return hashlib.sha256(first_page).digest(), header, next_frame, tuple(pending_headers), shm_header

    def _verify_source_stable(self, wal, source_sig, wal_sig, probe):
        with self.source.open("rb") as handle:
            first_page = handle.read(PAGE_SIZE)
        if (_signature(self.source) != source_sig or _signature(wal) != wal_sig
                or self._probe(wal, first_page) != probe):
            raise DatabaseReadError("source_changed", "微信数据库正在 checkpoint 或写入")

    def _collect_wal(self, wal: Path, state: _WalState, mac_key: bytes,
                     salt: bytes, byteorder: str, seed: tuple[int, int], size: int):
        if not salt:
            return {}, None, _WalState(pending={}), 0
        # 回滚的尾帧会被下一事务原地复用，salt/大小/mtime 均可能不变。
        # 发现待提交区变化时回退到最后的提交边界，不跳过新事务的覆盖帧。
        with wal.open("rb") as handle:
            for index, previous_header in state.pending_headers:
                handle.seek(WAL_HEADER_SIZE + index * WAL_FRAME_SIZE)
                if handle.read(24) != previous_header:
                    state = _WalState(salt, state.committed_frames, state.committed_checksum, {},
                                      state.committed_frames, state.committed_checksum)
                    break
        pending = dict(state.pending or {})
        pending_headers = list(state.pending_headers)
        committed = {}
        database_pages = None
        checksum = state.checksum if state.frames else seed
        last = state.frames
        committed_frames = state.committed_frames
        committed_checksum = state.committed_checksum if committed_frames else seed
        decoded = 0
        total_frames = max(0, (size - WAL_HEADER_SIZE) // WAL_FRAME_SIZE)
        with wal.open("rb") as handle:
            handle.seek(WAL_HEADER_SIZE + state.frames * WAL_FRAME_SIZE)
            for index in range(state.frames, total_frames):
                header = handle.read(24)
                page = handle.read(PAGE_SIZE)
                if len(header) != 24 or len(page) != PAGE_SIZE:
                    raise DatabaseReadError("source_changed", "WAL 正在变化，等待稳定快照")
                # 文件可能预分配并保留前一世代尾部；旧 salt 不是当前有效帧。
                if header[8:16] != salt:
                    break
                page_number, commit_pages = struct.unpack(">II", header[:8])
                if page_number == 0 or page_number > 0x7FFFFFFF:
                    raise DatabaseReadError("wal_invalid_page", "WAL 页号不正确")
                checksum = wal_checksum(header[:8] + page, checksum, byteorder=byteorder)
                if checksum != struct.unpack(">II", header[16:24]):
                    raise DatabaseReadError("wal_checksum_failed", "WAL 帧完整性校验失败")
                pending[page_number] = decrypt_page(page, page_number, self.key, mac_key)
                pending_headers.append((index, header))
                decoded += 1
                last = index + 1
                if commit_pages:
                    committed.update(pending)
                    pending.clear()
                    pending_headers.clear()
                    database_pages = commit_pages
                    committed_frames, committed_checksum = last, checksum
        index_boundary = read_wal_index(self.source, salt, byteorder)
        if index_boundary is not None and (last != index_boundary or committed_frames != index_boundary or pending):
            raise DatabaseReadError("wal_commit_boundary_invalid", "WAL 索引边界没有对应完整提交帧")
        return committed, database_pages, _WalState(salt, last, checksum, pending,
            committed_frames, committed_checksum, tuple(pending_headers)), decoded

    @staticmethod
    def _check(path: Path):
        connection = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise DatabaseReadError("snapshot_invalid", "解密数据库结构校验失败")
        finally:
            connection.close()

    def _rebuild(self, mac_key, wal, salt, byteorder, seed, source_sig, wal_sig, probe,
                 wal_read_size, validate_source, first_page):
        temporary = self.path.with_name(self.path.name + ".building")
        pages = source_sig[1] // PAGE_SIZE
        if pages < 1 or source_sig[1] % PAGE_SIZE:
            raise DatabaseReadError("source_incomplete", "数据库文件尚未写入完成")
        try:
            with self.source.open("rb") as source, temporary.open("wb") as output:
                for page_number in range(1, pages + 1):
                    output.write(decrypt_page(source.read(PAGE_SIZE), page_number, self.key, mac_key))
            changes, commit_pages, state, wal_decoded = self._collect_wal(
                wal, _WalState(pending={}), mac_key, salt, byteorder, seed, wal_read_size)
            if changes:
                with temporary.open("r+b") as output:
                    for page_number, page in changes.items():
                        if page_number <= commit_pages:
                            output.seek((page_number - 1) * PAGE_SIZE)
                            output.write(page)
                    output.truncate(commit_pages * PAGE_SIZE)
                    output.flush()
                    os.fsync(output.fileno())
            self._check(temporary)
            self._verify_source_stable(wal, source_sig, wal_sig, probe)
            next_probe = self._probe(wal, first_page, state)
            validate_source()
            os.replace(temporary, self.path)
            return state, pages + wal_decoded, next_probe
        finally:
            temporary.unlink(missing_ok=True)

    def _apply_increment(self, changes: dict[int, bytes], commit_pages: int):
        # 所有认证与来源稳定性检查均已完成。写失败时恢复受影响页面，保留旧副本。
        with self.path.open("r+b") as output:
            original_size = os.fstat(output.fileno()).st_size
            old_pages = {}
            for number in changes:
                if number <= commit_pages:
                    output.seek((number - 1) * PAGE_SIZE)
                    old_pages[number] = output.read(PAGE_SIZE)
            try:
                for number, page in changes.items():
                    if number <= commit_pages:
                        output.seek((number - 1) * PAGE_SIZE)
                        output.write(page)
                output.truncate(commit_pages * PAGE_SIZE)
                output.flush()
                os.fsync(output.fileno())
                self._check(self.path)
            except Exception:
                for number, page in old_pages.items():
                    output.seek((number - 1) * PAGE_SIZE)
                    output.write(page)
                output.truncate(original_size)
                output.flush()
                os.fsync(output.fileno())
                self._source_signature = None
                raise

    def _refresh_once(self, validate_source=lambda: None):
        source_sig = _signature(self.source)
        wal = Path(str(self.source) + "-wal")
        wal_sig = _signature(wal)
        with self.source.open("rb") as handle:
            first_page = handle.read(PAGE_SIZE)
        salt, byteorder, seed = self._wal_header(wal)
        # 活跃微信以经过校验的双份 WAL 索引声明提交边界；回滚/预分配尾不得交付。
        committed_limit = read_wal_index(self.source, salt, byteorder)
        wal_read_size = wal_sig[1] if committed_limit is None else WAL_HEADER_SIZE + committed_limit * WAL_FRAME_SIZE
        if salt and wal_read_size > wal_sig[1]:
            raise DatabaseReadError("wal_index_incomplete", "WAL 提交帧尚未完整写入")
        probe = self._probe(wal, first_page)
        # probe 可能在上次解析完以后看到新帧。当前 salt 的未消费帧不能视为空闲。
        unconsumed_frame = (len(probe[2]) == 24 and salt and probe[2][8:16] == salt
                            and (committed_limit is None or self._wal_state.frames < committed_limit))
        if (source_sig == self._source_signature and wal_sig == self._wal_signature
                and probe == self._probe_signature and not unconsumed_frame):
            validate_source()
            self.status = replace(self.status, healthy=True, stale=False, changed=False, error_code="")
            return self.status
        enc_key, file_salt = key_parts(self.key, first_page)
        mac_key = hmac_key(enc_key, file_salt)
        generation = hashlib.sha256(file_salt).hexdigest()[:24]
        rebuild = (source_sig != self._source_signature or not self.path.exists()
                   or self._wal_state.salt != salt
                   or (self._probe_signature and self._probe_signature[0] != probe[0])
                   or (committed_limit is not None and committed_limit < self._wal_state.frames)
                   or wal_sig[1] < WAL_HEADER_SIZE + self._wal_state.frames * WAL_FRAME_SIZE)
        if rebuild:
            state, decoded, next_probe = self._rebuild(mac_key, wal, salt, byteorder, seed, source_sig, wal_sig, probe,
                                                      wal_read_size, validate_source, first_page)
        else:
            changes, commit_pages, state, decoded = self._collect_wal(
                wal, self._wal_state, mac_key, salt, byteorder, seed, wal_read_size)
            self._verify_source_stable(wal, source_sig, wal_sig, probe)
            next_probe = self._probe(wal, first_page, state)
            if commit_pages and commit_pages * PAGE_SIZE < self.path.stat().st_size:
                rebuild = True
                state, decoded, next_probe = self._rebuild(mac_key, wal, salt, byteorder, seed, source_sig, wal_sig, probe,
                                                          wal_read_size, validate_source, first_page)
            elif changes:
                validate_source()
                self._apply_increment(changes, commit_pages)
            else:
                validate_source()
        self._source_signature, self._wal_signature = source_sig, wal_sig
        self._wal_state = state
        self._probe_signature = next_probe
        self.status = SnapshotStatus(True, False, time.time(), generation, "", True,
            self.status.full_rebuilds + int(rebuild),
            self.status.incremental_refreshes + int(not rebuild),
            self.status.decrypted_pages + decoded)
        return self.status

    def refresh(self) -> SnapshotStatus:
        with self._lock:
            last_code = "snapshot_unavailable"
            for _ in range(self.retry_attempts):
                try:
                    with source_snapshot_lock(self.source) as validate_source:
                        return self._refresh_once(validate_source)
                except DatabaseReadError as exc:
                    last_code = exc.code
                except (OSError, sqlite3.Error, ValueError):
                    last_code = "snapshot_io_failed"
            self.status = replace(self.status, healthy=False, stale=True, changed=False, error_code=last_code)
            return self.status

    def close(self):
        with self._lock:
            self.path.unlink(missing_ok=True)
            self.status = replace(self.status, healthy=False, stale=True, error_code="snapshot_closed")

    @contextmanager
    def read(self):
        with self._lock:
            if self.status.stale or not self.status.healthy:
                raise DatabaseReadError(self.status.error_code, "数据库快照不可用或已陈旧")
            connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            connection.row_factory = sqlite3.Row
            connection.text_factory = lambda value: _decode_text(value)
            try:
                yield connection
            finally:
                connection.close()


def _decode_text(value):
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value
