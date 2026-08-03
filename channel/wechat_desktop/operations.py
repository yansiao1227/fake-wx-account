"""可脱离 Channel 复用的微信会话定位与发送操作。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from channel.wechat_desktop.models import ConversationInfo


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

    如果会话尚未被扫描到，则保留旧行为：把传入值当作会话标题使用。
    """

    row = selectors.get(conversation)
    if row is None:
        return WechatConversationSelector(str(conversation or ""))
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

    def send_text(self, conversation: str, text: str, *, expedited: bool = False) -> dict:
        """发送文本；进度提示可通过 ``expedited`` 跳过拟人化节流。"""

        with self._reply_session():
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

        path = str(Path(image_path).resolve())
        with self._reply_session():
            selector = self._selector_resolver(conversation)
            result = self._client.send_file(
                selector.title,
                [path],
                runtime_id=selector.runtime_id,
                row_index=selector.row_index,
            )
        return build_delivery_result(result, self._observation_reader())
