"""数据库是唯一接收来源；启动与读取失败不得调用 UIA 或 OCR。"""

from types import SimpleNamespace
import threading

import pytest

from channel.wechat_desktop.backend import create_wechat_desktop_backend
from channel.wechat_desktop.config import DEFAULT_CONFIG, load_wechat_desktop_config
from channel.wechat_desktop.db.errors import DatabaseReadError
from channel.wechat_desktop.hybrid import WechatDatabaseBackend
from .helpers import _bare_wechat_channel


def test_default_factory_uses_database_source_without_initializing_uia():
    reader = SimpleNamespace(account_id="synthetic-account")
    backend = create_wechat_desktop_backend({}, db_reader=reader)

    assert DEFAULT_CONFIG["desktop_backend"] == "db_uia"
    assert isinstance(backend, WechatDatabaseBackend)
    assert backend.source.get_reader() is reader
    assert not backend.uia_initialized


@pytest.mark.parametrize("name", ["uia", "unknown"])
def test_removed_backend_is_rejected_by_config_and_factory(name):
    with pytest.raises(ValueError, match="UIA 消息接收后端已移除"):
        load_wechat_desktop_config({"desktop_backend": name})
    with pytest.raises(ValueError, match="UIA 消息接收后端已移除"):
        create_wechat_desktop_backend({"desktop_backend": name})


def test_database_read_failure_never_initializes_or_falls_back_to_uia():
    class FailedReader:
        account_id = "synthetic-account"

        def refresh(self):
            raise DatabaseReadError("synthetic_read_failure", "模拟数据库读取失败")

        def status(self):
            return {"db_read_healthy": False, "db_read_stale": True}

    class ForbiddenClient:
        def __getattr__(self, name):
            pytest.fail(f"数据库读取失败不应调用 UIA：{name}")

    backend = create_wechat_desktop_backend(
        {}, db_reader=FailedReader(), store=object(), client=ForbiddenClient()
    )

    for _ in range(2):
        status, events = backend.observe_events()
        assert events == []
        assert status["message_source"] == "wechat_database"
        assert status["db_read_error_code"] == "synthetic_read_failure"
        assert status["db_read_stale"]
        assert not backend.uia_initialized


def test_channel_startup_only_starts_database_pipeline_workers(monkeypatch):
    import channel.wechat_desktop.pipeline.channel as channel_module

    created = []
    calls = []

    class Worker:
        def __init__(self, *, target, name, daemon):
            self.name, self.target = name, target
            self.ident = None
            created.append(self)

        def start(self):
            calls.append(self.name)

    class Backend:
        # 即使组件没有 capabilities，启动也不能尝试 UIA 接收准备。
        def resume(self):
            calls.append("resume")

        def __getattr__(self, name):
            pytest.fail(f"启动不得预加载 OCR 或激活微信：{name}")

    channel = _bare_wechat_channel()
    channel.config = load_wechat_desktop_config({"shadow_mode": True})
    channel._driver = Backend()
    channel._store = SimpleNamespace(
        recover_interrupted_events=lambda: {}, get_state=lambda *_args: False
    )
    channel._service = SimpleNamespace(
        update_status=lambda **_kwargs: None,
        set_agent_executor=lambda _executor: None,
    )
    channel._materialization_active = threading.Event()
    channel.report_startup_success = lambda: None
    channel._trace = lambda *_args: None
    monkeypatch.setattr(channel_module.threading, "Thread", Worker)

    channel._start_workers()

    assert calls == ["resume", "cow-wechat-reply-fifo", "cow-wechat-materialize", "cow-wechat-scan"]
    assert len(created) == 3
    assert not hasattr(channel, "_warmup_thread")
