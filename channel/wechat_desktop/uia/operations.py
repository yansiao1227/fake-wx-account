"""可脱离 Channel 复用的微信会话定位与发送操作。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import unquote

from channel.wechat_desktop.config import DEFAULT_CONFIG

# 保留旧导入路径；标题规则只有通道根模块这一份实现。
from channel.wechat_desktop.conversation import (
    conversation_header_matches_target,
    conversation_titles_match,
    strip_member_count_suffix,
)
from channel.wechat_desktop.models import ConversationInfo
from channel.wechat_desktop.contracts import SendResult, SendStatus
from channel.wechat_desktop.send_control import SendCancelled, SendNotSubmitted, wait_send_delay
from channel.wechat_desktop.uia.controls import _runtime_id, _text


class WechatConversationSearchOperations:
    """为唯一数据库联系人有界打开本地搜索结果，不把搜索行当作会话绑定。"""

    def __init__(self, client):
        self._client = client

    def _require_window(self, hwnd: int, pid: int) -> None:
        if (int(self._client.get_owner_window_handle()) != hwnd
                or int(self._client.get_owner_window_process_id()) != pid):
            raise SendNotSubmitted("conversation_search_window_changed")

    @staticmethod
    def _require_root(root, hwnd: int, pid: int) -> None:
        if (int(getattr(root, "NativeWindowHandle", 0)) != hwnd
                or int(getattr(root, "ProcessId", 0)) != pid):
            raise SendNotSubmitted("conversation_search_root_unverified")

    def _search_edit(self, root, pid: int):
        candidates = [control for control in self._client._walk(root)
                      if _text(getattr(control, "ControlTypeName", "")) == "EditControl"
                      and _text(getattr(control, "Name", "")) == "搜索"
                      and int(getattr(control, "ProcessId", 0)) == pid]
        return candidates[0] if len(candidates) == 1 and _runtime_id(candidates[0]) else None

    def _search_result(self, root, name: str, pid: int):
        candidates = [control for control in self._client._walk(root)
                      if _text(getattr(control, "AutomationId", "")) == "search_item_" + name
                      and _text(getattr(control, "Name", "")) == name
                      and int(getattr(control, "ProcessId", 0)) == pid]
        return candidates[0] if len(candidates) == 1 and _runtime_id(candidates[0]) else None

    def _settle(self) -> None:
        # 固定次数、单次至多一秒；不引入独立的业务默认值或无限重试。
        delay = max(0, min(int(self._client.config.get(
            "uia_selection_settle_ms", DEFAULT_CONFIG["uia_selection_settle_ms"])), 1000))
        wait_send_delay(delay / 1000.0, self._client._stop_event)

    def _clear_own_search(self, name: str, edit_id: str, hwnd: int, pid: int) -> None:
        """仅清除本次仍持有的查询；不覆盖期间改动的搜索输入。"""
        self._require_window(hwnd, pid)
        with self._client._uia_root() as root:
            self._require_root(root, hwnd, pid)
            edit = self._search_edit(root, pid)
            if edit is not None and _runtime_id(edit) == edit_id:
                pattern = edit.GetValuePattern()
                if str(pattern.Value or "") == name:
                    pattern.SetValue("")

    def ensure_conversation_visible(self, display_name: str, *, is_group: bool, expected_pid: int) -> bool:
        name = str(display_name or "").strip()
        pid = int(expected_pid or 0)
        if not name or not isinstance(is_group, bool) or pid <= 0:
            raise SendNotSubmitted("conversation_search_identity_unverified")
        hwnd = int(self._client.get_owner_window_handle())
        self._require_window(hwnd, pid)
        with self._client.operation_lock:
            self._client.ensure_foreground_window()
            self._require_window(hwnd, pid)
            with self._client._uia_root() as root:
                self._require_root(root, hwnd, pid)
                edit = self._search_edit(root, pid)
                if edit is None:
                    return False
                edit_id = _runtime_id(edit)
                pattern = edit.GetValuePattern()
                pattern.SetValue(name)
                if str(pattern.Value or "") != name:
                    raise SendNotSubmitted("conversation_search_query_unverified")
            opened = False
            try:
                for _ in range(3):
                    self._settle()
                    self._require_window(hwnd, pid)
                    with self._client._uia_root() as root:
                        self._require_root(root, hwnd, pid)
                        current_edit = self._search_edit(root, pid)
                        if (current_edit is None or _runtime_id(current_edit) != edit_id
                                or str(current_edit.GetValuePattern().Value or "") != name):
                            raise SendNotSubmitted("conversation_search_query_changed")
                        result = self._search_result(root, name, pid)
                        if result is None:
                            continue
                        runtime_id = _runtime_id(result)
                        # 在提交点击前重新读取唯一身份，拒绝运行期间换行。
                        fresh = self._search_result(root, name, pid)
                        if fresh is None or _runtime_id(fresh) != runtime_id:
                            raise SendNotSubmitted("conversation_search_result_changed")
                        old_cursor = None
                        try:
                            import win32api
                            old_cursor = win32api.GetCursorPos()
                            click = getattr(self._client, "_click_control_point", None)
                            if callable(click):
                                if not click(fresh):
                                    raise SendNotSubmitted("conversation_search_result_not_clickable")
                            else:
                                fresh.Click()
                        finally:
                            if old_cursor is not None:
                                win32api.SetCursorPos(old_cursor)
                        opened = True
                        break
                if not opened:
                    return False
                self._settle()
                self._require_window(hwnd, pid)
                if not conversation_header_matches_target(
                        self._client.get_title(), name, is_group=is_group):
                    raise SendNotSubmitted("conversation_search_header_unverified")
            finally:
                self._clear_own_search(name, edit_id, hwnd, pid)
            self._settle()
            self._require_window(hwnd, pid)
            rows = [row for row in self._client.get_visible_conversations()
                    if (conversation_titles_match(row.conversation_title, name) if is_group
                        else str(row.conversation_title or "").strip() == name)]
            return len(rows) == 1 and bool(rows[0].runtime_id)


@dataclass(frozen=True)
class WechatConversationSelector:
    """一次微信会话定位所需的稳定参数。"""

    title: str
    runtime_id: str = ""
    row_index: int = -1


def resolve_conversation_selector(
    selectors: Mapping[str, ConversationInfo], conversation: str
) -> WechatConversationSelector:
    """把内部会话 ID 转成客户端可使用的标题和 UIA 定位信息。

    优先按内部 key（``uia-session:...``）精确查找。若调用方传入的是显示名，
    再按标题唯一匹配。
    标题歧义时不猜测，退回仅标题定位。
    """

    key = str(conversation or "")
    row = selectors.get(key)
    if row is None:
        target = key.strip()
        # Never treat an internal session key as a WeChat display title.
        if target and not target.startswith("uia-session:"):
            title_matches = [
                item
                for item in selectors.values()
                if conversation_titles_match(
                    getattr(item, "conversation_title", ""), target
                )
            ]
            if len(title_matches) == 1:
                row = title_matches[0]
    if row is None:
        # Stale uia-session keys must not become locate titles.
        if key.strip().startswith("uia-session:"):
            return WechatConversationSelector("")
        return WechatConversationSelector(key)
    return WechatConversationSelector(
        title=row.conversation_title,
        runtime_id=row.runtime_id,
        row_index=row.row_index,
    )


def build_delivery_result(result: dict, observation: dict) -> SendResult:
    """统一封装发送结果，屏蔽不同微信后端的原始返回格式。"""

    return SendResult.from_backend({
        **result,
        "accepted_by": "uia",
        "verification": (
            "outgoing_uia_bubble" if result.get("verified") else "unverified"
        ),
        "observation": observation,
    })


def resolve_local_media_path(path_or_url: str) -> Path:
    """Normalize local file paths and ``file://`` URLs for Windows/Unix.

    AgentBridge packages local images as ``file://C:\\...`` / ``file:///C:/...``.
    ``Path(\"file://D:\\\\foo\").resolve()`` on Windows becomes the broken path
    ``D:foo`` (missing separator), so callers must strip the scheme first.
    """
    raw = str(path_or_url or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError("empty media path")

    if raw.lower().startswith("file:"):
        rest = raw[5:]  # after "file:"
        # file:///C:/x  or  file:///home/x
        if rest.startswith("///"):
            rest = rest[3:]
            # Unix absolute: file:///home/x → need leading slash back
            if not re.match(r"^[A-Za-z]:", rest):
                rest = "/" + rest
        # file://C:\x  or  file://localhost/C:/x  or  file://server/share
        elif rest.startswith("//"):
            rest = rest[2:]
            lower_rest = rest.lower()
            if lower_rest.startswith("localhost/") or lower_rest.startswith(
                "localhost\\"
            ):
                rest = rest[10:]
            elif not re.match(r"^[A-Za-z]:", rest) and (
                "/" in rest or "\\" in rest
            ):
                # UNC: server/share → \\server\share
                rest = "\\\\" + rest.replace("/", "\\")
        # file:/C:/x
        elif rest.startswith("/"):
            if len(rest) >= 3 and rest[2] == ":":
                rest = rest[1:]
        raw = unquote(rest)

    path = Path(raw).expanduser()
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


class WechatSendOperations:
    """微信发送动作集合。

    该类只依赖一个兼容 ``send_message``/``send_file`` 的客户端，以及三个回调。
    锁调度、会话绑定和状态采集由网关或调用方提供，因此本类可以独立单测，也便于
    未来把 UIA 客户端换成其他桌面自动化实现。
    """

    def __init__(
        self,
        client,
        selector_resolver: Callable[[str], WechatConversationSelector],
        reply_session: Callable,
        observation_reader: Callable[[], dict],
    ):
        self._client = client
        self._selector_resolver = selector_resolver
        self._observation_reader = observation_reader
        # The client performs its own bounded UI work under this per-section
        # reply-priority lease. Acquiring it per send section (instead of
        # wrapping the whole send here) keeps humanized pacing waits from
        # blocking scans: the lease is only held while UIA is actually used.
        if hasattr(client, "uia_section"):
            client.uia_section = reply_session

    def send_text(self, conversation: str, text: str, *, expedited: bool = False) -> SendResult:
        """发送文本；进度提示可通过 ``expedited`` 跳过拟人化节流。

        不在这里整体持有回复租约：客户端按有界 UI 段自行获取
        （``client.uia_section``），拟人化发送间隔在租约之外等待。
        """

        selector = self._selector_resolver(conversation)
        kwargs = {
            "runtime_id": selector.runtime_id,
            "row_index": selector.row_index,
        }
        if expedited:
            kwargs["expedited"] = True
        try:
            result = self._client.send_message(selector.title, text, **kwargs)
        except SendCancelled:
            raise
        except SendNotSubmitted as exc:
            result = SendResult(SendStatus.NOT_SENT, str(exc))
        except Exception as exc:
            result = SendResult(SendStatus.UNCERTAIN, str(exc))
        return self._result_with_observation(result)

    def _result_with_observation(self, result) -> SendResult:
        try:
            observation = self._observation_reader()
        except Exception as exc:
            observation = {"error": str(exc)}
        return build_delivery_result(result, observation)

    def send_image(self, conversation: str, image_path: str) -> SendResult:
        """解析为绝对路径后，通过微信文件粘贴能力发送图片。"""

        try:
            path = str(resolve_local_media_path(image_path))
        except (ValueError, OSError) as exc:
            return SendResult(SendStatus.NOT_SENT, str(exc))
        if not Path(path).is_file():
            return SendResult(SendStatus.NOT_SENT, f"image file does not exist: {path}")
        selector = self._selector_resolver(conversation)
        try:
            result = self._client.send_file(
                selector.title, [path], runtime_id=selector.runtime_id, row_index=selector.row_index,
            )
        except SendCancelled:
            raise
        except SendNotSubmitted as exc:
            result = SendResult(SendStatus.NOT_SENT, str(exc))
        except Exception as exc:
            result = SendResult(SendStatus.UNCERTAIN, str(exc))
        return self._result_with_observation(result)
