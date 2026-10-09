"""旧 UIA 包导入路径的兼容入口；公共接口与工厂由通道根模块维护。"""

from channel.wechat_desktop.backend import (
    WechatDesktopBackend,
    create_wechat_desktop_backend,
)

__all__ = ["WechatDesktopBackend", "create_wechat_desktop_backend"]
