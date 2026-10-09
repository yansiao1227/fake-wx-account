"""只读微信进程中的 WCDB Cipher 对象；禁止写入进程或激活 UIA。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import struct
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from .crypto import PAGE_SIZE, verify_key
from .errors import DatabaseReadError
from .security import private_directory, protect, unprotect

CONFIG_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
HEX_KEY = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
_candidate_locations = {}


class _ProcessEntry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


class _MemoryInfo(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("alignment", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD), ("alignment2", wintypes.DWORD)]


def _kernel():
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise DatabaseReadError("unsupported_platform", "数据库密钥读取需要 64 位 Windows Python")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = wintypes.BOOL
    kernel.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                     ctypes.POINTER(_MemoryInfo), ctypes.c_size_t]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    return kernel


def process_is_alive(pid: int) -> bool:
    if os.name != "nt":
        return True  # 可注入合成账号的跨平台单元测试。
    kernel = _kernel()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        result = wintypes.DWORD()
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(result))) and result.value == 259
    finally:
        kernel.CloseHandle(handle)


def find_weixin_processes() -> list[tuple[int, str]]:
    kernel = _kernel()
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise DatabaseReadError("process_scan_failed", "无法枚举微信进程")
    result = []
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.lower() == "weixin.exe":
                result.append((entry.th32ProcessID, _process_version(entry.th32ProcessID)))
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    # 主窗口进程优先；此处只读取 HWND/PID，不改变焦点或控件树。
    try:
        import win32gui
        import win32process
        main_pids = set()
        def collect(hwnd, _):
            if win32gui.GetClassName(hwnd) == "mmui::MainWindow":
                main_pids.add(win32process.GetWindowThreadProcessId(hwnd)[1])
        win32gui.EnumWindows(collect, None)
        result.sort(key=lambda item: item[0] not in main_pids)
    except Exception:
        pass
    return result


def _process_version(pid):
    try:
        import win32api
        kernel = _kernel()
        handle = kernel.OpenProcess(0x1000, False, pid)
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return "unknown"
            path = buffer.value
        finally:
            kernel.CloseHandle(handle)
        version = win32api.GetFileVersionInfo(path, "\\")
        high, low = version["FileVersionMS"], version["FileVersionLS"]
        return ".".join(map(str, (high >> 16, high & 65535, low >> 16, low & 65535)))
    except Exception:
        return "unknown"


class _MemoryReader:
    def __init__(self, pid, deadline):
        self.kernel = _kernel()
        self.handle = self.kernel.OpenProcess(0x0010 | 0x0400, False, pid)
        self.deadline = deadline

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)

    def read(self, address, size):
        if not self.handle or not (0 < size <= 4 * 1024 * 1024):
            return b""
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        self.kernel.ReadProcessMemory(self.handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read))
        return buffer.raw[:read.value]

    def chunks(self, *, private_only=False):
        address = 0
        while address < 0x7FFFFFFF0000 and time.monotonic() < self.deadline:
            info = _MemoryInfo()
            if not self.kernel.VirtualQueryEx(self.handle, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)):
                break
            base, size = info.BaseAddress or 0, info.RegionSize
            if not size:
                break
            address = base + size
            if info.State != 0x1000 or info.Protect & (0x100 | 0x01) or not info.Protect:
                continue
            if private_only and info.Type != 0x20000:
                continue
            offset = 0
            while offset < size and time.monotonic() < self.deadline:
                block = self.read(base + offset, min(4 * 1024 * 1024, size - offset))
                if block:
                    yield base + offset, block
                offset += max(1, len(block) - 256) if block else min(4 * 1024 * 1024, size - offset)
                if len(block) <= 256 and block:
                    break


def _keys_in_blob(blob: bytes):
    for match in HEX_KEY.finditer(blob):
        run = match.group(1)
        starts = [0] if len(run) <= 96 else list(range(0, len(run) - 63, 32)) + [len(run) - 64]
        for start in dict.fromkeys(starts):
            key = bytes.fromhex(run[start:start + 64].decode("ascii"))
            yield key
            if start + 96 <= len(run):
                yield key + bytes.fromhex(run[start + 64:start + 96].decode("ascii"))


def scan_key_candidates(processes, timeout_seconds: float):
    deadline = time.monotonic() + timeout_seconds
    candidates = set()
    accessible = False
    for pid, _ in processes:
        if time.monotonic() >= deadline:
            break
        memory = _MemoryReader(pid, deadline)
        if not memory.handle:
            continue
        accessible = True
        try:
            addresses = set()
            for base, block in memory.chunks():
                position = block.find(CONFIG_NAME)
                while position >= 0:
                    addresses.add(base + position)
                    position = block.find(CONFIG_NAME, position + 1)
                for match in HEX_KEY.finditer(block):
                    literal = match.group(0)
                    for key in _keys_in_blob(literal):
                        candidates.add((pid, key))
                        _candidate_locations.setdefault((pid, key), set()).add(
                            (base + match.start(), len(literal), False))
            patterns = {struct.pack("<QQ", address, len(CONFIG_NAME)) for address in addresses}
            if not patterns:
                continue
            for base, block in memory.chunks(private_only=True):
                for pattern in patterns:
                    position = block.find(pattern)
                    while position >= 0:
                        node = memory.read(base + position - 16, 80)
                        if len(node) >= 64:
                            pointer = struct.unpack_from("<Q", node, 40)[0]
                            obj = memory.read(pointer + 0x88, 40)
                            if len(obj) >= 24:
                                data_pointer, data_length = struct.unpack_from("<QQ", obj, 8)
                                if 0 < data_length <= 1024:
                                    blob = memory.read(data_pointer, data_length)
                                    decoded = bytes(value ^ CONFIG_MASK[index % len(CONFIG_MASK)]
                                                    for index, value in enumerate(blob))
                                    for key in _keys_in_blob(decoded):
                                        candidates.add((pid, key))
                                        _candidate_locations.setdefault((pid, key), set()).add((data_pointer, data_length, True))
                        position = block.find(pattern, position + 1)
        finally:
            memory.close()
    if not accessible:
        raise DatabaseReadError("process_access_denied", "无法只读访问微信进程，请检查进程权限")
    return tuple(candidates)


def candidate_is_resident(pid: int, key: bytes) -> bool:
    """复核已认证 Cipher 对象；正常轮询只读少量字节，不重复全内存扫描。"""
    locations = _candidate_locations.get((pid, key), ())
    if not locations:
        return False
    memory = _MemoryReader(pid, time.monotonic() + 1)
    try:
        for pointer, length, masked in locations:
            blob = memory.read(pointer, length)
            decoded = (bytes(value ^ CONFIG_MASK[index % len(CONFIG_MASK)] for index, value in enumerate(blob))
                       if masked else blob)
            if key in _keys_in_blob(decoded):
                return True
        return False
    finally:
        memory.close()


class KeyProvider:
    def __init__(self, binding, cache_dir, timeout_seconds=30.0, *, candidates=None):
        self.binding = binding
        self.cache_dir = private_directory(cache_dir)
        self.timeout_seconds = timeout_seconds
        self._candidates = candidates
        self._keys = {}
        self._next_rescan_at = 0.0

    def _rescan(self):
        # 新分片可在运行中创建新的 Cipher 对象。失败按节拍重试，不每秒扫全内存。
        self._next_rescan_at = time.monotonic() + max(1.0, self.timeout_seconds)
        if self._candidates is None:
            self._candidates = ()
        self._candidates = scan_key_candidates([(self.binding.pid, self.binding.version)], self.timeout_seconds)

    def get_key(self, relative_path, path) -> bytes:
        with Path(path).open("rb") as handle:
            page = handle.read(PAGE_SIZE)
        token = hashlib.sha256((self.binding.account_id + ":" + str(relative_path)).encode()).hexdigest()
        protected_file = self.cache_dir / (token + ".dpapi")
        entropy = self.binding.account_id.encode()
        cached = self._keys.get(token)
        if cached and verify_key(cached, page):
            return cached
        if protected_file.is_file():
            try:
                cached = unprotect(protected_file.read_bytes(), entropy)
                if verify_key(cached, page):
                    self._keys[token] = cached
                    return cached
            except Exception:
                pass
        if self._candidates is None:
            self._rescan()
        for attempt in range(2):
            for pid, candidate in self._candidates:
                if pid == self.binding.pid and verify_key(candidate, page):
                    self._keys[token] = candidate
                    temporary = protected_file.with_suffix("." + uuid.uuid4().hex + ".tmp")
                    try:
                        temporary.write_bytes(protect(candidate, entropy))
                        os.replace(temporary, protected_file)
                    finally:
                        temporary.unlink(missing_ok=True)
                    return candidate
            if attempt == 0 and time.monotonic() >= self._next_rescan_at:
                self._rescan()
            else:
                break
        raise DatabaseReadError("key_not_found", "当前微信版本未发现可认证的数据库密钥")
