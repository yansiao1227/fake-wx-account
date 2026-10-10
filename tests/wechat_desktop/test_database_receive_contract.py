"""唯一数据库接收入口与附件缓存终态，全部使用合成事件与本地文件。"""

from types import SimpleNamespace

import pytest

from channel.wechat_desktop.models import WechatDesktopEvent
from channel.wechat_desktop.storage.store import WechatDesktopStore
from .test_contracts import make_channel


@pytest.fixture
def store(tmp_path):
    ledger = WechatDesktopStore(str(tmp_path / "receive.sqlite3"))
    yield ledger
    ledger._get_connection().close()


def test_non_batch_observation_cannot_enter_receive_pipeline(store):
    event = WechatDesktopEvent("message", "synthetic", "Synthetic", "sender", "Sender",
                               "text", "synthetic message", source_type="private")
    acknowledged = []
    driver = SimpleNamespace(observe_events=lambda: ({"mode": "db_uia"}, [event]),
                             acknowledge_events=acknowledged.append)
    channel = make_channel(store, driver)
    channel._route_reply_event = lambda event: pytest.fail("non-database event routed")

    channel._poll_once()

    assert store.event_state(event.event_id) == {}
    assert acknowledged == []
    assert channel._service.status()["last_error"] == "source_batch_required"


@pytest.mark.parametrize("status,exists,expected", [
    ("materialized", True, "cached"),
    ("materialized", False, "observed"),
    ("attachment_identity_unavailable", True, "observed"),
])
def test_standalone_file_is_cached_only_after_verified_materialization(
    tmp_path, store, status, exists, expected
):
    path = tmp_path / "synthetic.txt"
    if exists:
        path.write_text("synthetic fixture", encoding="utf-8")
    event = WechatDesktopEvent("message", "synthetic", "Synthetic", "sender", "Sender",
                               "file", str(path), source_type="private",
                               attachment_status=status)
    store.receive_event(event)
    channel = make_channel(store, SimpleNamespace(materialize_event=lambda event: (event, 0)))
    channel._preserve_event_evidence = lambda event: None
    channel._enqueue_reply_event = lambda event: pytest.fail("standalone file replied")
    finished = []

    def finish(ids, state):
        finished.append((ids, state))
        channel._stop_event.set()

    channel._finish_lifecycle = finish
    channel._materialize_queue.put([event])
    channel._consume_materialization_queue()

    assert store.event_state(event.event_id)["state"] == expected
    assert finished == [([event.event_id], expected)]
    if expected == "observed":
        assert store.event_state(event.event_id)["reason"] == "attachment_unavailable"
