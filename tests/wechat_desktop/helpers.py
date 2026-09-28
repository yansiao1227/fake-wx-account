"""测试替身与控件构造器；不操作真实桌面。"""
import threading
from types import SimpleNamespace
from channel.wechat_desktop.models import ConversationInfo, HeaderInfo, OwnerInfo, UiaChatMessage
from channel.wechat_desktop.pipeline.channel import WechatDesktopChannel


class PipelineStoreStub(SimpleNamespace):
    """局部流水线单测的存储接口替身；持久化契约由独立 SQLite 测试覆盖。"""

    def set_event_state(self, *args, **kwargs):
        pass

    def event_state(self, event_id):
        return {}

    def delivery_outcome(self, event_ids, fallback):
        return fallback

    def claim_delivery(self, *args, **kwargs):
        return "stub-delivery", None

    def finish_delivery(self, *args, **kwargs):
        pass


class GeometryControl:
    def __init__(
        self,
        bounds,
        name="",
        class_name="",
        children=None,
        automation_id="",
    ):
        self.BoundingRectangle = SimpleNamespace(
            left=bounds[0], top=bounds[1], right=bounds[2], bottom=bounds[3]
        )
        self.Name = name
        self.ClassName = class_name
        self.AutomationId = automation_id
        self.ControlTypeName = "CustomControl"
        self._children = list(children or [])

    def GetChildren(self):
        return list(self._children)


class ClickableGeometryControl(GeometryControl):
    def __init__(self, *args, on_click=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.click_count = 0
        self.on_click = on_click

    def Click(self):
        self.click_count += 1
        if self.on_click:
            self.on_click()


def _history_row(name, class_name="mmui::ChatTextItemView", runtime_id=(42, 1)):
    row_control = GeometryControl((0, 0, 700, 80), name, class_name)
    row_control.ControlTypeName = "ListItemControl"
    row_control.GetRuntimeId = lambda: runtime_id
    return row_control


def _selection_tree(active_title, messages, row):
    header = GeometryControl(
        (400, 100, 700, 130),
        active_title,
        "Text",
        automation_id="current_chat_name_label",
    )
    message_list = GeometryControl(
        (400, 140, 900, 700),
        class_name="List",
        children=messages,
        automation_id="chat_message_list",
    )
    root = GeometryControl((0, 0, 1000, 800), children=[header, message_list, row])
    return root, header, message_list


class FakeClient:
    def __init__(self):
        self.operation_lock = threading.RLock()
        self.rows = []
        self.histories = {}
        self.headers = {}
        self.history_calls = []
        self.history_ensure_conversation = []
        self.focus_calls = 0
        self.owner_calls = 0
        self.file_paths = {}
        self.file_fetches = []
        self.image_paths = {}
        self.image_fetches = []

    def allowed_process_ids(self):
        return {42}

    def get_owner_info(self):
        self.owner_calls += 1
        return OwnerInfo("小牛", source="config")

    def get_owner_window_process_id(self):
        return 42

    def probe_tree(self):
        return 10

    def focus_window(self):
        self.focus_calls += 1

    def get_visible_conversations(self):
        return list(self.rows)

    def locate_conversation(self, name, runtime_id="", row_index=-1):
        self.selected = name
        self.selected_key = runtime_id or name
        return self.selected_key in self.headers or name in self.headers

    def get_title(self):
        name = getattr(self, "selected", "")
        if name:
            key = getattr(self, "selected_key", "")
            return self.headers[key] if key in self.headers else self.headers[name]
        return next(iter(self.headers.values()), HeaderInfo("", "unknown", 1))

    def get_chat_history(
        self,
        name=None,
        limit=5,
        runtime_id="",
        row_index=-1,
        ensure_conversation=True,
    ):
        if name:
            self.selected = name
            self.selected_key = runtime_id or name
        self.history_calls.append(name)
        self.history_ensure_conversation.append(ensure_conversation)
        return list(self.histories.get(runtime_id or name, []))[-limit:]

    def fetch_message_file(self, message):
        self.file_fetches.append(message.content)
        return self.file_paths.get(message.content, "")

    def fetch_message_image(self, message):
        self.image_fetches.append(message.content)
        return self.image_paths.get(message.content, "")

    def send_message(self, who, text, runtime_id="", row_index=-1):
        return {"success": True, "verified": True}

    def send_file(self, who, files, runtime_id="", row_index=-1):
        return {"success": True, "verified": True}


class FakeHook:
    available = True
    error = ""

    def start(self):
        return True

    def close(self):
        pass


def row(
    name,
    unread=1,
    mention=False,
    signature="new",
    runtime_id="",
    row_index=0,
    preview_prefix=False,
):
    return ConversationInfo(
        name,
        not_read_number=unread,
        mentions_self=mention,
        row_signature=signature,
        automation_id=f"session_item_{name}",
        runtime_id=runtime_id,
        row_index=row_index,
        preview_sender="成员" if preview_prefix else "",
        preview_has_sender_prefix=preview_prefix,
    )


def incoming(content, runtime_id):
    return UiaChatMessage(
        "成员",
        content,
        direction="incoming",
        runtime_id=runtime_id,
    )


def outgoing(content, runtime_id):
    return UiaChatMessage(
        "小牛",
        content,
        direction="outgoing",
        runtime_id=runtime_id,
    )


def _bare_wechat_channel():
    implementation = WechatDesktopChannel.__closure__[0].cell_contents
    channel = object.__new__(implementation)
    channel._daily_hot_scheduler = None
    return channel


class FakeTimer:
    instances = []

    def __init__(self, interval, callback, args=()):
        self.interval = interval
        self.callback = callback
        self.args = args
        self.daemon = False
        self.cancelled = False
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback(*self.args)
