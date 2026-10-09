"""微信桌面通道的稳定接口与创建入口。

通道编排层只依赖本模块定义的接口，不直接了解 UIA、窗口句柄等实现细节。
未来迁移到其他微信桌面实现时，只需新增后端并在工厂中注册。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional
from channel.wechat_desktop.contracts import ConversationTarget, SendResult, TargetResolution

from channel.wechat_desktop.models import (
    ReplyTargetValidation,
    WechatDesktopEvent,
    WechatHistoryReadResult,
)


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

    def resume(self) -> None:
        """可选生命周期入口：通道显式重启时重新允许后端操作。"""

    @abstractmethod
    def wait_for_changes(self, stop_event) -> str:
        """等待微信会话变化，返回本次唤醒原因。"""

    @abstractmethod
    def observe_events(self) -> tuple[dict, list[WechatDesktopEvent]]:
        """读取事件；未确认事件必须保留并重新交付，确认不等同于回复完成。"""

    @abstractmethod
    def acknowledge_events(self, event_ids: list[str]) -> None:
        """仅在持久化接收成功后确认；重复确认幂等。"""

    @abstractmethod
    def resolve_target(self, conversation: str) -> TargetResolution:
        """返回不透明会话 ID；失效 ID 不得悄悄降级为同名会话。"""

    def resolve_send_target(self, conversation: str) -> TargetResolution:
        """主动发送授权前解析可信显示名与类型；无法确认类型时必须拒绝发送。"""
        return self.resolve_target(conversation)

    @abstractmethod
    def validate_reply_target(self, event: WechatDesktopEvent) -> ReplyTargetValidation:
        """发送前确认目标消息仍然有效。"""

    @abstractmethod
    def materialize_event(
        self, event: WechatDesktopEvent, *, before_share_fetch=None
    ) -> tuple[WechatDesktopEvent, int]:
        """按需下载或截图事件中的附件。"""

    @abstractmethod
    def read_current_chat_history(self, limit: int = 20) -> WechatHistoryReadResult:
        """读取微信当前已打开会话的最近聊天记录。"""

    def begin_reply_cycle(self, conversation: str, conversation_id: str = ""):
        """可选接收钩子：UIA 标记回复周期以协调可见气泡扫描。"""

    def end_reply_cycle(self):
        """可选接收钩子：结束 UIA 回复监控；数据库接收无需额外状态。"""

    def register_interim_text(self, conversation_id: str, text: str):
        """可选接收钩子：UIA 登记进度提示，避免把出站气泡当作新消息。"""

    def forget_interim_text(self, conversation_id: str, text: str):
        """可选接收钩子：移除 UIA 进度提示记录。"""

    @abstractmethod
    def send_text(self, conversation: str, text: str, *,
                  authorized_target: ConversationTarget | None = None) -> SendResult:
        """向指定会话发送普通文本；每段提交前复核已授权身份。"""

    def send_interim_text(self, conversation: str, text: str, *,
                          authorized_target: ConversationTarget | None = None) -> SendResult:
        """发送进度提示；不支持加速的后端可退化为普通文本。"""
        if authorized_target is None:
            return self.send_text(conversation, text)
        return self.send_text(conversation, text, authorized_target=authorized_target)

    def read_chat_history(self, conversation_id: str, limit: int = 20) -> WechatHistoryReadResult:
        """可选能力：按稳定会话身份查询历史；UIA 后端仍使用当前会话入口。"""
        raise NotImplementedError("当前后端不支持按会话身份查询历史")

    def search_contacts(self, query: str = "", limit: int = 20) -> dict:
        """可选能力：联系人检索不得改变实时接收游标。"""
        raise NotImplementedError("当前后端不支持数据库联系人检索")

    @abstractmethod
    def send_image(self, conversation: str, image_path: str, *,
                   authorized_target: ConversationTarget | None = None) -> SendResult:
        """向指定会话发送图片文件。"""


def create_wechat_desktop_backend(
    config: dict,
    *,
    client: Optional[object] = None,
    shell_hook: Optional[object] = None,
    store: Optional[object] = None,
    db_reader: Optional[object] = None,
    uia_gateway: Optional[object] = None,
    uia_driver: Optional[object] = None,
) -> WechatDesktopBackend:
    """按配置创建微信桌面后端。

    ``desktop_backend`` 注册 UIA 和数据库接收/UIA 发送混合后端。
    ``uia_gateway`` 注入只负责界面操作的组件；``uia_driver`` 保留为旧接口兼容入口。
    测试可通过 ``client`` 和 ``shell_hook`` 注入替身，避免真实操作微信窗口。
    """

    backend_name = str(config.get("desktop_backend", "uia") or "uia").strip().lower()
    if backend_name == "db_uia":
        from channel.wechat_desktop.hybrid import WechatDatabaseBackend
        return WechatDatabaseBackend(
            config,
            db_reader=db_reader,
            store=store,
            client=client,
            shell_hook=shell_hook,
            uia_gateway=uia_gateway,
            uia_driver=uia_driver,
        )
    if backend_name != "uia":
        raise ValueError(f"Unsupported WeChat desktop backend: {backend_name}")

    # 延迟导入可避免后端模块反向依赖接口时产生循环引用。
    from channel.wechat_desktop.uia.driver import WechatUiaDriver

    return WechatUiaDriver(config, client=client, shell_hook=shell_hook)
