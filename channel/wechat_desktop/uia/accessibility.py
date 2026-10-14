"""仅对已验证的微信构建恢复 Qt 无障碍标志，不修改 DLL 或代码页。

Qt gate 的含义参考 wechatauto-replica，来源及许可证见本目录 NOTICE。
构建指纹、运行态指令、窗口与进程全部一致才允许写一个布尔字节。
"""

from __future__ import annotations

import ctypes
import hashlib
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from common.log import logger
from channel.wechat_desktop.send_control import check_send_allowed


@dataclass(frozen=True)
class AccessibilityBuild:
    version: str
    size: int
    sha256: str
    gate_rva: int
    code_rva: int
    code_bytes: bytes


BUILD_FINGERPRINT = AccessibilityBuild(
    version="4.1.9.30",
    size=181126696,
    sha256="89fc940377a779babbd64c9cd4abe6256663681d92f12802aa2df97a8db18494",
    gate_rva=0x09FA6FC8,
    code_rva=0x00784E52,
    code_bytes=bytes.fromhex(
        "4885c90f8433070000803d66218209000f84260700004889cee8101920014c8b4020418378140074"
    ),
)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_build(process) -> bool:
    build = BUILD_FINGERPRINT
    path = Path(process.module_path)
    return (
        process.version == build.version
        and path.name.casefold() == "weixin.dll"
        and path.stat().st_size == build.size
        and hash_file(path) == build.sha256
        and process.read(process.module_base + build.code_rva, len(build.code_bytes)) == build.code_bytes
    )


class _WechatProcess:
    def __init__(self, hwnd, pid, handle, module_base, module_path, version):
        self.hwnd = hwnd
        self.pid = pid
        self.handle = handle
        self.module_base = module_base
        self.module_path = module_path
        self.version = version
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signature = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                     ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.kernel.ReadProcessMemory.argtypes = signature
        self.kernel.ReadProcessMemory.restype = wintypes.BOOL
        self.kernel.WriteProcessMemory.argtypes = signature
        self.kernel.WriteProcessMemory.restype = wintypes.BOOL

    def require_window(self):
        import win32gui
        import win32process

        self._require_module()
        if (not win32gui.IsWindow(self.hwnd)
                or win32process.GetWindowThreadProcessId(self.hwnd)[1] != self.pid):
            raise RuntimeError("微信无障碍恢复期间窗口或模块身份已变化")

    def _require_module(self):
        import win32process

        if (Path(win32process.GetModuleFileNameEx(self.handle, self.module_base)) != self.module_path
                or self.read(self.module_base + BUILD_FINGERPRINT.code_rva,
                             len(BUILD_FINGERPRINT.code_bytes)) != BUILD_FINGERPRINT.code_bytes):
            raise RuntimeError("微信无障碍恢复期间模块身份已变化")

    def read(self, address, size):
        value = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if (not self.kernel.ReadProcessMemory(int(self.handle), address, value, size, ctypes.byref(count))
                or count.value != size):
            raise RuntimeError("无法读取微信无障碍标志")
        return value.raw

    def write(self, address, value):
        if address != self.module_base + BUILD_FINGERPRINT.gate_rva or value not in (b"\0", b"\1"):
            raise ValueError("仅允许恢复已验证的微信无障碍布尔标志")
        # 回滚仍使用原进程句柄和已校验模块，即使主窗口已重建也能撤销。
        # 启用标志则必须同时校验当前窗口，避免操作已变化的目标。
        self._require_module()
        if value == b"\1":
            self.require_window()
        count = ctypes.c_size_t()
        data = ctypes.create_string_buffer(value)
        if (not self.kernel.WriteProcessMemory(int(self.handle), address, data, 1, ctypes.byref(count))
                or count.value != 1):
            raise RuntimeError("无法恢复微信无障碍标志")


@contextmanager
def _open_process(hwnd):
    import win32api
    import win32con
    import win32gui
    import win32process

    if not win32gui.IsWindow(hwnd):
        raise RuntimeError("微信窗口已失效")
    pid = int(win32process.GetWindowThreadProcessId(hwnd)[1])
    access = (win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
              | win32con.PROCESS_VM_WRITE | win32con.PROCESS_VM_OPERATION)
    handle = win32api.OpenProcess(access, False, pid)
    try:
        executable = Path(win32process.GetModuleFileNameEx(handle, 0))
        if executable.name.casefold() != "weixin.exe":
            raise RuntimeError("目标窗口进程不是微信")
        modules = [(base, Path(win32process.GetModuleFileNameEx(handle, base)))
                   for base in win32process.EnumProcessModules(handle)]
        matches = [(base, path) for base, path in modules if path.name.casefold() == "weixin.dll"]
        if len(matches) != 1:
            raise RuntimeError("无法唯一核验微信主模块")
        base, path = matches[0]
        info = win32api.GetFileVersionInfo(str(path), "\\")
        version = ".".join(str(value) for value in (
            win32api.HIWORD(info["FileVersionMS"]), win32api.LOWORD(info["FileVersionMS"]),
            win32api.HIWORD(info["FileVersionLS"]), win32api.LOWORD(info["FileVersionLS"])))
        yield _WechatProcess(hwnd, pid, handle, int(base), path, version)
    finally:
        handle.Close()


def restore_accessibility(hwnd, *, probe, wait) -> bool:
    """匹配固定构建才启用标志；控件未恢复或操作异常时撤销本次写入。"""
    check_send_allowed()
    with _open_process(hwnd) as process:
        process.require_window()
        if not _verify_build(process):
            logger.warning("[WechatDesktop] UIA accessibility build could not be verified")
            return False
        address = process.module_base + BUILD_FINGERPRINT.gate_rva
        original = process.read(address, 1)
        if original not in (b"\0", b"\1"):
            logger.warning("[WechatDesktop] UIA accessibility flag is not a verified boolean")
            return False
        changed = original == b"\0"
        recovered = False
        try:
            if changed:
                check_send_allowed()
                process.write(address, b"\1")
            process.require_window()
            wait()
            check_send_allowed()
            ready = bool(probe())
            process.require_window()
            check_send_allowed()
            recovered = ready
            if recovered and changed:
                logger.info("[WechatDesktop] restored verified WeChat Qt accessibility flag")
            return recovered
        finally:
            if changed and not recovered:
                process.write(address, original)
