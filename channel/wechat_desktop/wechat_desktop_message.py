from bridge.context import ContextType
from channel.chat_message import ChatMessage
from channel.wechat_desktop.models import WechatDesktopEvent


class WechatDesktopMessage(ChatMessage):
    def __init__(self, event: WechatDesktopEvent):
        super().__init__(event.to_dict())
        self.event = event
        self.msg_id = event.event_id
        self.create_time = event.observed_at
        self.ctype = ContextType.IMAGE if event.content_type == "image" else ContextType.TEXT
        self.content = event.content
        self.from_user_id = event.sender_id
        self.from_user_nickname = event.sender_name
        self.to_user_id = "wechat_desktop_self"
        self.to_user_nickname = "我"
        self.other_user_id = event.conversation_id
        self.other_user_nickname = event.conversation_name
        self.is_group = event.is_group
        self.is_at = event.is_at
        self.actual_user_id = event.sender_id
        self.actual_user_nickname = event.sender_name
        self.at_list = []
        self.self_display_name = ""
        self.confidence = event.confidence
        self.evidence_path = event.evidence_path
