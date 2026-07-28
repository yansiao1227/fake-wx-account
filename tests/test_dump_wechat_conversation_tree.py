from contextlib import contextmanager
from pathlib import Path
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dump_wechat_conversation_tree import dump_conversation_tree


class Rect:
    left = 1
    top = 2
    right = 3
    bottom = 4


class Control:
    def __init__(self, name="", class_name="", automation_id="", children=None):
        self.Name = name
        self.ClassName = class_name
        self.AutomationId = automation_id
        self.ControlTypeName = "ListItemControl"
        self.BoundingRectangle = Rect()
        self._children = children or []

    def GetChildren(self):
        return self._children

    def GetRuntimeId(self):
        return [1, id(self)]


class Client:
    def __init__(self, root):
        self.root = root
        self.operation_lock = threading.RLock()

    @contextmanager
    def _uia_root(self):
        yield self.root


def _tree():
    rows = [
        Control("甲\n预览一", "mmui::ChatSessionCell", "session_item_甲"),
        Control("乙\n预览二", "mmui::ChatSessionCell", "session_item_乙"),
    ]
    session_list = Control(class_name="SessionList", children=rows)
    unrelated = Control("聊天内容", "mmui::ChatDetailView")
    return Control(class_name="MainWindow", children=[session_list, unrelated])


def test_dump_conversation_tree_selects_list_and_redacts_content():
    result = dump_conversation_tree(Client(_tree()))

    assert result["root_class"] == "SessionList"
    assert result["visible_session_count"] == 2
    assert result["node_count"] == 3
    assert [node["automation_id"] for node in result["nodes"][1:]] == [
        "session_item_<redacted>",
        "session_item_<redacted>",
    ]
    assert all("name" not in node for node in result["nodes"])


def test_dump_conversation_tree_can_include_content():
    result = dump_conversation_tree(Client(_tree()), include_content=True)

    assert [node["automation_id"] for node in result["nodes"][1:]] == [
        "session_item_甲",
        "session_item_乙",
    ]
    assert result["nodes"][1]["name"] == "甲\n预览一"
