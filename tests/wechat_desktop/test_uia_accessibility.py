"""指定微信构建的 UIA 可访问性恢复；进程及 DLL 全部使用替身。"""

import struct
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from channel.wechat_desktop.uia import accessibility
from channel.wechat_desktop.send_control import SendCancelled, send_scope


class FakeProcess:
    def __init__(self, module_path, fingerprint):
        self.module_path = module_path
        self.version = fingerprint.version
        self.module_base = 0x180000000
        self.fingerprint = fingerprint
        self.code = fingerprint.code_bytes
        self.gate = b"\x00"
        self.read_calls = []
        self.write_calls = []
        self.require_calls = 0
        self.on_require = None
        self.on_write = None

    def read(self, address, size):
        self.read_calls.append((address, size))
        if address == self.module_base + self.fingerprint.code_rva:
            assert size == len(self.fingerprint.code_bytes)
            return self.code
        assert address == self.module_base + self.fingerprint.gate_rva
        assert size == 1
        return self.gate

    def write(self, address, value):
        assert address == self.module_base + self.fingerprint.gate_rva
        assert len(value) == 1
        self.write_calls.append((address, value))
        self.gate = value
        if self.on_write is not None:
            self.on_write(value)

    def require_window(self):
        self.require_calls += 1
        if self.on_require is not None:
            self.on_require()


@pytest.fixture
def simulated_process(monkeypatch, tmp_path):
    path = tmp_path / "Weixin.dll"
    path.write_bytes(b"test-dll")
    fingerprint = replace(accessibility.BUILD_FINGERPRINT, size=path.stat().st_size)
    monkeypatch.setattr(accessibility, "BUILD_FINGERPRINT", fingerprint)
    monkeypatch.setattr(accessibility, "hash_file", lambda module_path: fingerprint.sha256)
    process = FakeProcess(path, fingerprint)
    lifecycle = []

    @contextmanager
    def open_process(hwnd):
        assert hwnd == 123
        lifecycle.append("open")
        try:
            yield process
        finally:
            lifecycle.append("close")

    monkeypatch.setattr(accessibility, "_open_process", open_process)
    return process, lifecycle


def test_verified_guard_instruction_points_to_gate_byte():
    fingerprint = accessibility.BUILD_FINGERPRINT
    # cmp byte ptr [rip+disp32], 0 的下一条指令地址为 code_rva + 16。
    assert fingerprint.code_bytes[9:11] == b"\x80\x3d"
    displacement = struct.unpack("<i", fingerprint.code_bytes[11:15])[0]
    assert fingerprint.code_rva + 16 + displacement == fingerprint.gate_rva


@pytest.mark.parametrize("mismatch", ["version", "module_name", "size", "sha256", "code", "gate"])
def test_unsupported_build_or_memory_is_never_written(simulated_process, monkeypatch, mismatch):
    process, lifecycle = simulated_process
    if mismatch == "version":
        process.version = "4.1.9.31"
    elif mismatch == "module_name":
        renamed = process.module_path.with_name("Unrelated.dll")
        process.module_path.rename(renamed)
        process.module_path = renamed
    elif mismatch == "size":
        process.module_path.write_bytes(b"different-size")
    elif mismatch == "sha256":
        monkeypatch.setattr(accessibility, "hash_file", lambda path: "0" * 64)
    elif mismatch == "code":
        process.code = b"\x90" + process.code[1:]
    else:
        process.gate = b"\x02"

    assert accessibility.restore_accessibility(
        123, probe=lambda: pytest.fail("构建或内存校验失败不能探测恢复结果"),
        wait=lambda: pytest.fail("构建或内存校验失败不能等待恢复"),
    ) is False

    assert process.write_calls == []
    assert lifecycle == ["open", "close"]


def test_supported_build_restores_only_gate_byte_and_keeps_success(simulated_process):
    process, lifecycle = simulated_process
    calls = []

    def probe():
        calls.append("probe")
        assert process.gate == b"\x01"
        return True

    assert accessibility.restore_accessibility(123, probe=probe, wait=lambda: calls.append("wait")) is True

    assert process.write_calls == [(process.module_base + process.fingerprint.gate_rva, b"\x01")]
    assert process.gate == b"\x01"
    assert process.require_calls >= 1
    assert calls == ["wait", "probe"]
    assert lifecycle == ["open", "close"]


def test_already_enabled_gate_is_never_rewritten(simulated_process):
    process, lifecycle = simulated_process
    process.gate = b"\x01"

    assert accessibility.restore_accessibility(123, probe=lambda: True, wait=lambda: None) is True

    assert process.gate == b"\x01"
    assert process.write_calls == []
    assert lifecycle == ["open", "close"]


def test_failed_probe_restores_original_disabled_gate(simulated_process):
    process, lifecycle = simulated_process

    assert accessibility.restore_accessibility(123, probe=lambda: False, wait=lambda: None) is False

    address = process.module_base + process.fingerprint.gate_rva
    assert process.write_calls == [(address, b"\x01"), (address, b"\x00")]
    assert process.gate == b"\x00"
    assert lifecycle == ["open", "close"]


@pytest.mark.parametrize("failure_stage", ["window", "wait", "probe"])
def test_exception_after_enable_rolls_back_gate(simulated_process, failure_stage):
    process, lifecycle = simulated_process

    def fail():
        raise RuntimeError("模拟恢复检查失败")

    if failure_stage == "window":
        process.on_require = lambda: fail() if process.gate == b"\x01" else None
    wait = fail if failure_stage == "wait" else lambda: None
    probe = fail if failure_stage == "probe" else lambda: True

    try:
        result = accessibility.restore_accessibility(123, probe=probe, wait=wait)
    except RuntimeError as exc:
        assert str(exc) == "模拟恢复检查失败"
    else:
        assert result is False

    address = process.module_base + process.fingerprint.gate_rva
    assert process.write_calls == [(address, b"\x01"), (address, b"\x00")]
    assert process.gate == b"\x00"
    assert lifecycle == ["open", "close"]


def test_failed_probe_does_not_disable_previously_enabled_gate(simulated_process):
    process, lifecycle = simulated_process
    process.gate = b"\x01"

    assert accessibility.restore_accessibility(123, probe=lambda: False, wait=lambda: None) is False

    assert process.gate == b"\x01"
    assert process.write_calls == []
    assert lifecycle == ["open", "close"]


def test_window_changed_during_successful_probe_rolls_back(simulated_process):
    process, lifecycle = simulated_process

    def probe():
        process.on_require = lambda: (_ for _ in ()).throw(RuntimeError("窗口已重建"))
        return True

    with pytest.raises(RuntimeError, match="窗口已重建"):
        accessibility.restore_accessibility(123, probe=probe, wait=lambda: None)
    assert process.gate == b"\0"
    assert lifecycle == ["open", "close"]


def test_cancelled_after_successful_probe_rolls_back(simulated_process):
    process, _ = simulated_process
    cancelled = False

    def check():
        if cancelled:
            raise SendCancelled("发送已暂停")

    def probe():
        nonlocal cancelled
        cancelled = True
        return True

    with send_scope(check), pytest.raises(SendCancelled, match="发送已暂停"):
        accessibility.restore_accessibility(123, probe=probe, wait=lambda: None)
    assert process.gate == b"\0"


def test_native_rollback_checks_original_module_without_requiring_old_window(monkeypatch):
    process = accessibility._WechatProcess.__new__(accessibility._WechatProcess)
    process.module_base = 0x180000000
    process.handle = 42
    writes = []
    validations = []

    def write_memory(handle, address, data, size, count):
        writes.append((address, data.raw[:size]))
        count._obj.value = size
        return True

    process.kernel = SimpleNamespace(WriteProcessMemory=write_memory)
    monkeypatch.setattr(process, "_require_module", lambda: validations.append("module"))

    def missing_window():
        raise RuntimeError("原窗口已失效")

    monkeypatch.setattr(process, "require_window", missing_window)
    address = process.module_base + accessibility.BUILD_FINGERPRINT.gate_rva
    process.write(address, b"\0")
    with pytest.raises(RuntimeError, match="原窗口已失效"):
        process.write(address, b"\1")
    assert writes == [(address, b"\0")]
    assert validations == ["module", "module"]
