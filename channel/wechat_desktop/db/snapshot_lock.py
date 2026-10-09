"""与 Windows SQLite 的 WAL 写入/checkpoint 锁协调；不写入微信文件。"""

from __future__ import annotations

import ctypes
import os
import struct
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

from .errors import DatabaseReadError
from .crypto import PAGE_SIZE, wal_checksum


def read_wal_index(source: Path, salt: bytes, byteorder: str) -> int | None:
    """读取当前 WAL 的已提交边界；调用者须持有 source_snapshot_lock。

    Windows 的 WalIndexHdr 用本机小端格式，但 aSalt 是从 WAL 头直接
    memcpy 的原始字节；头校验也总使用本机小端，与 WAL 帧校验端序独立。
    格式及双头校验依据 https://sqlite.org/src/file/src/wal.c 中
    WalIndexHdr、walIndexWriteHdr 和 walIndexTryHdr。
    """
    if not salt:
        return None
    shm = Path(str(source) + "-shm")
    if not shm.exists():
        return None
    if len(salt) != 8 or byteorder not in ("<", ">"):
        raise DatabaseReadError("wal_index_invalid", "WAL 索引输入格式不正确")
    try:
        with shm.open("rb", buffering=0) as file:
            headers = file.read(96)
    except OSError as exc:
        raise DatabaseReadError("wal_index_invalid", "无法只读读取 WAL 索引头") from exc
    if len(headers) != 96 or headers[:48] != headers[48:]:
        raise DatabaseReadError("wal_index_invalid", "WAL 索引双头不一致或不完整")
    header = headers[:48]
    if (struct.unpack_from("<I", header, 0)[0] != 3007000
            or header[12] != 1
            or header[13] != (byteorder == ">")
            or struct.unpack_from("<H", header, 14)[0] != PAGE_SIZE
            or header[32:40] != salt):
        raise DatabaseReadError("wal_index_invalid", "WAL 索引格式或世代与 WAL 不一致")
    if wal_checksum(header[:40], byteorder="<") != struct.unpack_from("<II", header, 40):
        raise DatabaseReadError("wal_index_invalid", "WAL 索引头校验失败")
    return struct.unpack_from("<I", header, 16)[0]


class _Overlapped(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE)]


@contextmanager
def source_snapshot_lock(source: Path) -> Iterator[Callable[[], None]]:
    """只读共享锁住 WAL_WRITE_LOCK 和 WAL_CKPT_LOCK，忙时下次轮询重试。

    SQLite os_win.c 的 WIN_SHM_BASE=(22+8)*4=120；锁槽 0/1 分别为
    WAL 写入和 checkpoint。共享锁不排斥其它读取者，也不改共享内存。
    先共享锁住 WIN_SHM_DMS=128 防止初始化截断。已关闭的离线库可能
    没有 -shm；不为它创建任何源文件。返回发布前调用的稳定性校验函数。
    """
    shm = Path(str(source) + "-shm")
    if os.name != "nt" or not shm.exists():
        initially_present = shm.exists()
        def validate_source():
            if shm.exists() != initially_present:
                raise DatabaseReadError("source_changed", "微信共享内存正在初始化，等待下一次快照")
        yield validate_source
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # 共享读/写，但在锁存续期间禁止删除或替换锁文件。
    handle = kernel.CreateFileW(str(shm), 0x80000000, 3, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise DatabaseReadError("source_lock_unavailable", "无法只读打开微信共享内存锁文件")
    held_locks = []
    try:
        inode = shm.stat().st_ino
        def validate_source():
            if not shm.exists() or shm.stat().st_ino != inode:
                raise DatabaseReadError("source_changed", "微信共享内存锁文件已替换")
        # FAIL_IMMEDIATELY=1；不设置 EXCLUSIVE_LOCK，取得共享锁。
        for offset, length in ((128, 1), (120, 2)):
            overlapped = _Overlapped()
            overlapped.Offset = offset
            if not kernel.LockFileEx(handle, 1, 0, length, 0, ctypes.byref(overlapped)):
                raise DatabaseReadError("source_busy", "微信正在写入、checkpoint 或初始化，等待下一次快照")
            held_locks.append((overlapped, length))
        yield validate_source
    finally:
        for overlapped, length in reversed(held_locks):
            kernel.UnlockFileEx(handle, 0, length, 0, ctypes.byref(overlapped))
        kernel.CloseHandle(handle)
