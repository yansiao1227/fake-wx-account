from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from channel.wechat_desktop.store import WechatDesktopStore
from common.utils import expand_path
from config import conf


class WechatDesktopService:
    def __init__(self, store: WechatDesktopStore):
        self.store = store
        self._lock = threading.RLock()
        self._agent_executor: Optional[Callable] = None
        self._status = {
            "running": False,
            "paused": bool(store.get_state("paused", False)),
            "login_status": "unknown",
            "mode": "unavailable",
            "last_observation_at": 0,
            "last_error": "",
            "shadow_mode": True,
            "auto_reply_private_all": False,
            "auto_reply_groups_all": False,
            "auto_reply_blacklist": [],
            "learn_from_official_accounts": False,
            "diagnostic_logging": False,
            "reply_in_flight": False,
            "reply_conversation": "",
            "uia_available": False,
            "shell_hook_active": False,
            "owner_name": "",
            "owner_source": "unknown",
            "queue_depth": 0,
            "queue_active_event": "",
            "queue_active_conversation": "",
            "queue_active_since": 0,
            "queue_last_terminal": "",
            "queue_completed": 0,
            "queue_skipped": 0,
            "queue_failed": 0,
            "queue_timeout": 0,
            "queue_superseded": 0,
            "auto_reply_contacts": [],
            "auto_reply_groups": [],
            "group_reply_mode": "at_only",
        }

    def set_agent_executor(self, executor: Optional[Callable]):
        with self._lock:
            self._agent_executor = executor

    def execute_agent_action(self, action: str, **params) -> dict:
        with self._lock:
            executor = self._agent_executor
        if executor is None:
            return {
                "status": "error",
                "message": "wechat_desktop channel is not running",
            }
        try:
            result = executor(action, **params)
            return result if isinstance(result, dict) else {
                "status": "error",
                "message": "invalid wechat_desktop executor result",
            }
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def update_status(self, **updates):
        with self._lock:
            self._status.update(updates)

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def set_paused(self, paused: bool):
        paused = bool(paused)
        self.store.set_state("paused", paused)
        self.update_status(paused=paused)
        return self.status()

_service = None
_service_lock = threading.Lock()


def _default_store_path() -> str:
    workspace = Path(expand_path(conf().get("agent_workspace", "~/cow")))
    return str(workspace / "wechat_desktop.sqlite3")


def get_wechat_desktop_service() -> WechatDesktopService:
    global _service
    with _service_lock:
        expected = _default_store_path()
        if _service is None or _service.store.path != expected:
            _service = WechatDesktopService(WechatDesktopStore(expected))
        return _service


def reset_wechat_desktop_service_for_tests(path: Optional[str] = None):
    global _service
    with _service_lock:
        _service = WechatDesktopService(WechatDesktopStore(path or _default_store_path()))
        return _service
