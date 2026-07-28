"""
channel factory
"""
from common import const
from .channel import Channel


def create_channel(channel_type) -> Channel:
    """
    create a channel instance
    :param channel_type: channel type code
    :return: channel instance
    """
    ch = Channel()
    if channel_type == 'web':
        from channel.web.web_channel import WebChannel
        ch = WebChannel()
    elif channel_type in (const.WEIXIN, "wx"):
        from channel.weixin.weixin_channel import WeixinChannel
        ch = WeixinChannel()
        channel_type = const.WEIXIN
    elif channel_type == const.WECHAT_DESKTOP:
        from channel.wechat_desktop.wechat_desktop_channel import WechatDesktopChannel
        ch = WechatDesktopChannel()
    else:
        raise RuntimeError
    ch.channel_type = channel_type
    return ch
