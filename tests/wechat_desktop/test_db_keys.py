"""合成内存中的密钥候选复核；不访问真实进程或生产密钥。"""

import pytest
from types import SimpleNamespace

from channel.wechat_desktop.db import keys
from channel.wechat_desktop.db.errors import DatabaseReadError
from .test_db_crypto import FAKE_KEY, encrypt_page, plain_page


@pytest.fixture
def synthetic_dpapi(monkeypatch):
    # Windows DPAPI 本身由 crypto 测试覆盖；这里只测 provider 的重试与缓存策略。
    monkeypatch.setattr(keys, "protect", lambda data, entropy: b"\x00sealed:" + data[::-1])
    monkeypatch.setattr(keys, "unprotect", lambda data, entropy: data[8:][::-1])


@pytest.mark.parametrize("prefix", [b"x", b"X"])
@pytest.mark.parametrize("include_salt", [False, True])
def test_raw_hex_candidate_remains_resident_without_rescanning(monkeypatch, prefix, include_salt):
    pid = 43210
    base = 0x10000
    synthetic_key = bytes(range(32))
    synthetic_salt = bytes(range(32, 48))
    candidate = synthetic_key + synthetic_salt if include_salt else synthetic_key
    literal = prefix + b"'" + candidate.hex().encode("ascii") + b"'"
    leading = b"synthetic-prefix\x00"
    memory_image = bytearray(leading + literal + b"\x00synthetic-tail")
    reads = []
    scanned = []
    closed = []

    class SyntheticMemoryReader:
        def __init__(self, process_id, deadline):
            assert process_id == pid
            self.handle = True

        def chunks(self, *, private_only=False):
            scanned.append(private_only)
            yield base, bytes(memory_image)

        def read(self, pointer, length):
            reads.append((pointer, length))
            offset = pointer - base
            return bytes(memory_image[offset:offset + length])

        def close(self):
            closed.append(True)

    monkeypatch.setattr(keys, "_MemoryReader", SyntheticMemoryReader)
    monkeypatch.setattr(keys, "_candidate_locations", {})

    candidates = keys.scan_key_candidates([(pid, "synthetic-version")], timeout_seconds=1)

    assert (pid, candidate) in candidates
    pointer = base + len(leading)
    assert keys._candidate_locations[(pid, candidate)] == {(pointer, len(literal), False)}
    assert keys.candidate_is_resident(pid, candidate)
    assert reads == [(pointer, len(literal))]
    assert scanned == [False]  # 复核只读候选位置，不能再扫描所有内存区域。

    # 模拟退出原账号或 Cipher 对象被释放，保留同长度的无效内容。
    memory_image[len(leading):len(leading) + len(literal)] = b"\x00" * len(literal)
    assert not keys.candidate_is_resident(pid, candidate)
    assert not keys.candidate_is_resident(pid + 1, candidate)
    assert scanned == [False]
    assert reads == [(pointer, len(literal)), (pointer, len(literal))]
    assert len(closed) == 3


def test_new_shard_missing_initial_candidate_rescans_once_and_reuses_dpapi_cache(tmp_path, monkeypatch, synthetic_dpapi):
    source = tmp_path / "message_1.db"
    source.write_bytes(encrypt_page(plain_page()))
    binding = SimpleNamespace(account_id="synthetic-account", pid=42, version="synthetic-version")
    scans = []
    monkeypatch.setattr(keys, "scan_key_candidates", lambda processes, timeout: scans.append((processes, timeout)) or ((42, FAKE_KEY),))
    provider = keys.KeyProvider(binding, tmp_path / "private", candidates=())
    assert provider.get_key("message/message_1.db", source) == FAKE_KEY
    assert provider.get_key("message/message_1.db", source) == FAKE_KEY
    restored = keys.KeyProvider(binding, tmp_path / "private", candidates=())
    assert restored.get_key("message/message_1.db", source) == FAKE_KEY
    assert scans == [([(42, "synthetic-version")], 30.0)]


def test_missing_key_failure_has_bounded_rescan_and_recovers_after_deadline(tmp_path, monkeypatch, synthetic_dpapi):
    source = tmp_path / "message_1.db"
    source.write_bytes(encrypt_page(plain_page()))
    binding = SimpleNamespace(account_id="synthetic-account", pid=42, version="synthetic-version")
    current = [100.0]
    scans = []
    def scanner(processes, timeout):
        scans.append(current[0])
        if len(scans) == 1:
            raise DatabaseReadError("process_access_denied", "synthetic failure")
        return ((42, FAKE_KEY),)
    monkeypatch.setattr(keys, "scan_key_candidates", scanner)
    monkeypatch.setattr(keys.time, "monotonic", lambda: current[0])
    provider = keys.KeyProvider(binding, tmp_path / "private")
    with pytest.raises(DatabaseReadError, match="synthetic failure"):
        provider.get_key("message/message_1.db", source)
    current[0] = 101.0
    with pytest.raises(DatabaseReadError) as failure:
        provider.get_key("message/message_1.db", source)
    assert failure.value.code == "key_not_found" and scans == [100.0]
    current[0] = 131.0
    assert provider.get_key("message/message_1.db", source) == FAKE_KEY
    assert scans == [100.0, 131.0]
