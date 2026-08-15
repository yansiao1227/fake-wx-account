"""可脱离 Channel 复用的微信会话定位与发送操作。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import unquote

from channel.wechat_desktop.models import ConversationInfo

# WeChat group headers often append the member count, e.g. "项目群(9)" / "项目群（9）".
# Session list AutomationId usually keeps the bare name (session_item_项目群).
_MEMBER_COUNT_SUFFIX = re.compile(
    r"^(?P<base>.*?)[\s\u00a0]*[（(](?P<count>\d+)[）)]\s*$"
)


def strip_member_count_suffix(title: str) -> str:
    """Remove a trailing ``(n)`` / ``（n）`` member-count suffix from a chat title."""
    value = str(title or "").strip()
    if not value:
        return ""
    match = _MEMBER_COUNT_SUFFIX.match(value)
    if not match:
        return value
    base = str(match.group("base") or "").strip()
    return base or value


def conversation_titles_match(left: str, right: str) -> bool:
    """Compare chat titles, allowing optional group member-count suffixes.

    Exact match wins. Otherwise compare after stripping a trailing ``(n)`` /
    ``（n）`` from either side (group detail headers often include the count;
    session rows / config usually do not).
    """
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return strip_member_count_suffix(a) == strip_member_count_suffix(b)


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

    优先按内部 key（``uia-session:...``）精确查找。若调用方传入的是显示名
    （例如每日热点广播使用 ``auto_reply_groups`` 里的群名），再按标题唯一匹配。
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


def build_delivery_result(result: dict, observation: dict) -> dict:
    """统一封装发送结果，屏蔽不同微信后端的原始返回格式。"""

    return {
        **result,
        "accepted_by": "uia",
        "verification": (
            "outgoing_uia_bubble" if result.get("verified") else "unverified"
        ),
        "observation": observation,
    }


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
    锁调度、会话缓存和状态采集仍由 Driver 持有，因此本类可以独立单测，也便于
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
        self._reply_session = reply_session
        self._observation_reader = observation_reader
        # The client performs its own bounded UI work under this per-section
        # reply-priority lease. Acquiring it per send section (instead of
        # wrapping the whole send here) keeps humanized pacing waits from
        # blocking scans: the lease is only held while UIA is actually used.
        if hasattr(client, "uia_section"):
            client.uia_section = reply_session

    def send_text(self, conversation: str, text: str, *, expedited: bool = False) -> dict:
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
        result = self._client.send_message(selector.title, text, **kwargs)
        return build_delivery_result(result, self._observation_reader())

    def send_image(self, conversation: str, image_path: str) -> dict:
        """解析为绝对路径后，通过微信文件粘贴能力发送图片。"""

        path = str(resolve_local_media_path(image_path))
        if not Path(path).is_file():
            raise FileNotFoundError(f"image file does not exist: {path}")
        selector = self._selector_resolver(conversation)
        result = self._client.send_file(
            selector.title,
            [path],
            runtime_id=selector.runtime_id,
            row_index=selector.row_index,
        )
        return build_delivery_result(result, self._observation_reader())
