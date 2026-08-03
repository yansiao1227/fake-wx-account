"""微信桌面后端的稳定接口与创建入口。

通道编排层只依赖本模块定义的接口，不直接了解 UIA、窗口句柄等实现细节。
未来迁移到其他微信桌面实现时，只需新增后端并在工厂中注册。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from channel.wechat_desktop.models import ReplyTargetValidation, WechatDesktopEvent


class WechatDesktopBackend(ABC):
    """微信桌面操作后端。

    接口按“观察、物化、发送、生命周期”划分，避免上层直接调用 UIA 私有方法。
    """

    @abstractmethod
    def ensure_foreground(self) -> bool:
        """尽力把微信主窗口切到前台。"""

    @abstractmethod
    def close(self) -> None:
        """停止等待并释放后端资源。"""

    @abstractmethod
    def wait_for_changes(self, stop_event) -> str:
        """等待微信会话变化，返回本次唤醒原因。"""

    @abstractmethod
    def observe_events(self) -> tuple[dict, list[WechatDesktopEvent]]:
        """读取可见会话并转换为统一事件。"""

    @abstractmethod
    def validate_reply_target(self, event: WechatDesktopEvent) -> ReplyTargetValidation:
        """发送前确认目标消息仍然有效。"""

    @abstractmethod
    def materialize_event(
        self, event: WechatDesktopEvent
    ) -> tuple[WechatDesktopEvent, int]:
        """按需下载或截图事件中的附件。"""

    @abstractmethod
    def begin_reply_cycle(self, conversation: str, conversation_id: str = ""):
        """标记某个会话进入回复周期。"""

    @abstractmethod
    def end_reply_cycle(self):
        """结束当前回复周期。"""

    @abstractmethod
    def register_interim_text(self, conversation_id: str, text: str):
        """登记进度提示，防止被误识别为新消息。"""

    @abstractmethod
    def forget_interim_text(self, conversation_id: str, text: str):
        """移除已发送的进度提示记录。"""

    @abstractmethod
    def send_text(self, conversation: str, text: str) -> dict:
        """向指定会话发送普通文本。"""

    def send_interim_text(self, conversation: str, text: str) -> dict:
        """发送进度提示；不支持加速的后端可退化为普通文本。"""
        return self.send_text(conversation, text)

    @abstractmethod
    def send_image(self, conversation: str, image_path: str) -> dict:
        """向指定会话发送图片文件。"""


def create_wechat_desktop_backend(
    config: dict,
    *,
    client: Optional[object] = None,
    shell_hook: Optional[object] = None,
) -> WechatDesktopBackend:
    """按配置创建微信桌面后端。

    ``desktop_backend`` 是预留迁移点。当前仅注册 UIA，但调用方不再与它耦合。
    测试可通过 ``client`` 和 ``shell_hook`` 注入替身，避免真实操作微信窗口。
    """

    backend_name = str(config.get("desktop_backend", "uia") or "uia").strip().lower()
    if backend_name != "uia":
        raise ValueError(f"Unsupported WeChat desktop backend: {backend_name}")

    # 延迟导入可避免后端模块反向依赖接口时产生循环引用。
    from channel.wechat_desktop.uia_driver import WechatUiaDriver

    return WechatUiaDriver(config, client=client, shell_hook=shell_hook)
