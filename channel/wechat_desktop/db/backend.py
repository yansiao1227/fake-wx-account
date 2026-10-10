"""旧导入路径兼容；跨来源组合实现位于通道根目录。"""

from channel.wechat_desktop.hybrid import WechatDatabaseBackend

__all__ = ["WechatDatabaseBackend"]
