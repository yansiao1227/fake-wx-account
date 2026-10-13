"""每日更新广播持久化去重，仅使用合成 SQLite，不连接微信。"""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from channel.wechat_desktop.contracts import SendResult, SendStatus
from channel.wechat_desktop.storage.store import WechatDesktopStore


DAY = "2026-10-13"
TARGET = "account-a:synthetic-group@chatroom"


@pytest.fixture
def store(tmp_path):
    ledger = WechatDesktopStore(str(tmp_path / "changelog.sqlite3"))
    yield ledger
    ledger._get_connection().close()


@pytest.mark.parametrize("status", list(SendStatus))
def test_changelog_terminal_blocks_replay_after_restart(store, status):
    assert store.claim_changelog(DAY, TARGET)
    result = SendResult(status, "合成发送观测")
    store.finish_changelog(DAY, TARGET, result)
    assert store.get_changelog_delivery(DAY, TARGET)["result"] == result.to_dict()
    assert not store.release_changelog(DAY, TARGET)
    # 迟到回调不能改写已经持久化的结果。
    store.finish_changelog(DAY, TARGET, SendResult(SendStatus.UNCERTAIN, "迟到结果"))
    assert store.get_changelog_delivery(DAY, TARGET)["result"] == result.to_dict()
    reopened = WechatDesktopStore(store.path)
    try:
        # claim 不接受正文散列，追加当天正文也不能改变去重身份。
        assert not reopened.claim_changelog(DAY, TARGET)
        assert reopened.get_changelog_delivery(DAY, TARGET)["status"] == status.value
    finally:
        reopened._get_connection().close()


def test_interrupted_changelog_claim_survives_restart(store):
    assert store.claim_changelog(DAY, TARGET)
    reopened = WechatDesktopStore(store.path)
    try:
        reopened.recover_interrupted_events()
        assert not reopened.claim_changelog(DAY, TARGET)
        assert reopened.get_changelog_delivery(DAY, TARGET)["status"] == "sending"
    finally:
        reopened._get_connection().close()


def test_changelog_claim_distinguishes_date_and_account(store):
    assert store.claim_changelog(DAY, TARGET)
    assert not store.claim_changelog(DAY, TARGET)
    assert store.claim_changelog("2026-10-14", TARGET)
    assert store.claim_changelog(DAY, "account-b:synthetic-group@chatroom")


def test_release_only_removes_pre_submission_claim(store):
    assert store.get_changelog_delivery(DAY, TARGET) == {}
    assert not store.release_changelog(DAY, TARGET)
    assert store.claim_changelog(DAY, TARGET)
    assert store.release_changelog(DAY, TARGET)
    assert store.get_changelog_delivery(DAY, TARGET) == {}
    assert store.claim_changelog(DAY, TARGET)
    store.finish_changelog(DAY, TARGET, SendResult(SendStatus.NOT_SENT))
    assert not store.release_changelog(DAY, TARGET)
    assert not store.claim_changelog(DAY, TARGET)


def test_changelog_claim_is_atomic_across_store_connections(store):
    other = WechatDesktopStore(store.path)
    barrier = threading.Barrier(8)

    def claim(index):
        ledger = store if index % 2 else other
        try:
            barrier.wait(timeout=5)
            return ledger.claim_changelog(DAY, TARGET)
        finally:
            ledger._get_connection().close()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(claim, range(8)))
        assert outcomes.count(True) == 1
        assert store._get_connection().execute(
            "SELECT count(*) FROM changelog_deliveries"
        ).fetchone()[0] == 1
    finally:
        other._get_connection().close()


@pytest.mark.parametrize("retention_days", [0, 1, 2, 7])
def test_changelog_cleanup_preserves_recent_claims(store, monkeypatch, retention_days):
    now = 2_000_000.0
    retention = max(2, retention_days) * 86400
    with monkeypatch.context() as patch:
        patch.setattr("channel.wechat_desktop.storage.store.time.time", lambda: now - retention - 1)
        assert store.claim_changelog("2026-10-01", TARGET)
        patch.setattr("channel.wechat_desktop.storage.store.time.time", lambda: now - 2 * 86400 + 1)
        assert store.claim_changelog("2026-10-12", TARGET)
        patch.setattr("channel.wechat_desktop.storage.store.time.time", lambda: now)
        assert store.claim_changelog(DAY, TARGET)
        store.cleanup(retention_days=retention_days)
    assert store.get_changelog_delivery("2026-10-01", TARGET) == {}
    assert not store.claim_changelog("2026-10-12", TARGET)
    assert not store.claim_changelog(DAY, TARGET)


@pytest.mark.parametrize("day,target", [("20261013", TARGET), ("2026-13-01", TARGET), (DAY, "")])
def test_changelog_claim_rejects_invalid_identity(store, day, target):
    with pytest.raises(ValueError):
        store.claim_changelog(day, target)
