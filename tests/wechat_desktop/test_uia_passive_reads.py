"""合成 UIA 树验证被动读取边界；不访问真实微信或桌面。"""

from contextlib import nullcontext
from dataclasses import replace

import pytest
from PIL import ImageGrab

from channel.wechat_desktop.models import OwnerInfo
from channel.wechat_desktop.uia.client import WechatUiaClient
from .helpers import GeometryControl


def passive_client(monkeypatch, items=(), sidebar=(), config=None):
    header = GeometryControl((0, 0, 200, 30), "Synthetic",
                             automation_id="current_chat_name_label")
    pane = GeometryControl((0, 100, 900, 700), children=items,
                           automation_id="chat_message_list")
    root = GeometryControl((0, 0, 1000, 800), children=[header, pane, *sidebar])
    client = WechatUiaClient(config or {})
    monkeypatch.setattr(client, "_window", lambda: (100, 42))
    monkeypatch.setattr(client, "_uia_root", lambda: nullcontext(root))

    def forbidden(*args, **kwargs):
        pytest.fail("被动读取不得激活窗口、点击、打开资料、截图或初始化 OCR")

    for name in ("focus_window", "ensure_foreground_window", "locate_conversation",
                 "_recover_empty_tree", "_click_taskbar_button", "_read_owner_profile_popup"):
        monkeypatch.setattr(client, name, forbidden)
    monkeypatch.setattr(client._group_sender_ocr, "enrich", forbidden)
    monkeypatch.setattr(client._group_sender_ocr, "_get_engine", forbidden)
    monkeypatch.setattr(ImageGrab, "grab", forbidden)
    return client, root, pane


def message(children=(), bounds=(0, 140, 900, 200), content="合成文本", runtime=(42, 1)):
    item = GeometryControl(bounds, content, "mmui::ChatTextItemView", children=children)
    item.GetRuntimeId = lambda: runtime
    return item


@pytest.mark.parametrize("kind, bounds, expected", [
    ("mmui::ChatTextBubble", (40, 150, 220, 190), "incoming"),
    ("mmui::ChatTextBubble", (680, 150, 860, 190), "outgoing"),
    ("mmui::ChatAvatarView", (40, 150, 80, 190), "incoming"),
    ("mmui::ChatAvatarView", (820, 150, 860, 190), "outgoing"),
    ("mmui::XTextView", (680, 150, 860, 190), "unknown"),
    ("mmui::ChatTextBubble", (0, 150, 900, 190), "unknown"),
    ("mmui::ChatTextBubble", (300, 150, 650, 190), "unknown"),
    ("mmui::ChatTextBubble", (800, 150, 960, 190), "unknown"),
])
def test_send_snapshot_uses_semantic_child_geometry_without_ocr(monkeypatch, kind, bounds, expected):
    child = GeometryControl(bounds, "合成文本", kind)
    client, _, _ = passive_client(monkeypatch, [message([child])])
    monkeypatch.setattr(client, "get_owner_info", lambda: pytest.fail("发送快照不得读交互账号"))

    snapshot = client.get_send_bubble_snapshot("Synthetic", limit=5)

    assert len(snapshot) == 1
    assert snapshot[0].content == "合成文本"
    assert snapshot[0].runtime_id == "42.1"
    assert snapshot[0].direction == expected


def test_send_snapshot_never_guesses_direction_from_message_row(monkeypatch):
    client, _, _ = passive_client(monkeypatch, [message(bounds=(680, 140, 860, 200))])
    assert client.get_send_bubble_snapshot()[0].direction == "unknown"


def test_quote_descendants_do_not_prove_current_message_direction(monkeypatch):
    quoted_body = GeometryControl((680, 150, 860, 190), class_name="mmui::ChatTextBubble")
    quote = GeometryControl((600, 140, 880, 200), class_name="mmui::ChatBubbleReferItemView",
                            children=[quoted_body])
    client, _, _ = passive_client(monkeypatch, [message([quote])])
    assert client.get_send_bubble_snapshot()[0].direction == "unknown"


def test_conflicting_semantic_children_keep_direction_unknown(monkeypatch):
    left = GeometryControl((40, 150, 220, 190), class_name="mmui::ChatTextBubble")
    right = GeometryControl((820, 150, 860, 190), class_name="mmui::ChatAvatarView")
    client, _, _ = passive_client(monkeypatch, [message([left, right])])
    assert client.get_send_bubble_snapshot()[0].direction == "unknown"


def test_send_snapshot_refuses_other_chat_without_switching_it(monkeypatch):
    client, _, _ = passive_client(monkeypatch, [message()])
    assert client.get_send_bubble_snapshot("Another chat") == []


def test_full_uia_receive_history_retains_ocr_enrichment(monkeypatch):
    client, _, _ = passive_client(monkeypatch, [message()])
    calls = []

    def enrich(messages, bounds, **kwargs):
        calls.append(kwargs)
        return [replace(item, direction="incoming") for item in messages]

    monkeypatch.setattr(client._group_sender_ocr, "enrich", enrich)
    assert client.get_chat_history(ensure_conversation=False)[0].direction == "incoming"
    assert calls == [{"resolve_sender_names": False}]


def test_empty_passive_owner_does_not_open_profile_or_recover_window(monkeypatch):
    client, _, _ = passive_client(monkeypatch)
    assert client.get_owner_info_passive() == OwnerInfo("", source="unknown")
    assert client.get_owner_info_passive() == OwnerInfo("", source="unknown")


def test_passive_sidebar_name_does_not_claim_native_account_id(monkeypatch):
    sidebar = GeometryControl((30, 30, 80, 80), "合成账号", "mmui::AvatarView",
                              automation_id="self_avatar")
    client, _, _ = passive_client(monkeypatch, sidebar=[sidebar])
    assert client.get_owner_info_passive() == OwnerInfo("合成账号", source="uia")


def test_passive_config_name_remains_unverified(monkeypatch):
    client, _, _ = passive_client(monkeypatch, config={"self_display_name": "配置账号"})
    assert client.get_owner_info_passive() == OwnerInfo("配置账号", source="config")


@pytest.mark.parametrize("next_window", [(200, 42), (100, 43)])
def test_passive_owner_cache_is_scoped_to_window_handle_and_pid(monkeypatch, next_window):
    sidebar = GeometryControl((30, 30, 80, 80), "旧账号", automation_id="self_avatar")
    client, root, _ = passive_client(monkeypatch, sidebar=[sidebar])
    assert client.get_owner_info_passive().nick_name == "旧账号"
    root._children.remove(sidebar)
    monkeypatch.setattr(client, "_window", lambda: next_window)
    assert client.get_owner_info_passive() == OwnerInfo("", source="unknown")


def test_passive_owner_discards_identity_when_window_changes_during_read(monkeypatch):
    sidebar = GeometryControl((30, 30, 80, 80), "合成账号", automation_id="self_avatar")
    client, _, _ = passive_client(monkeypatch, sidebar=[sidebar])
    windows = iter([(100, 42), (200, 43)])
    monkeypatch.setattr(client, "_window", lambda: next(windows))
    assert client.get_owner_info_passive() == OwnerInfo("", source="unknown")
    assert client._owner_cache is None


def test_passive_owner_missing_window_does_not_reuse_verified_cache(monkeypatch):
    client, _, _ = passive_client(monkeypatch)
    client._owner_cache = OwnerInfo("旧账号", wx_id="synthetic_id", source="uia")
    client._owner_cache_window = (100, 42)

    def unavailable():
        raise RuntimeError("synthetic window unavailable")

    monkeypatch.setattr(client, "_window", unavailable)
    assert client.get_owner_info_passive() == OwnerInfo("", source="unknown")
    assert client._owner_cache is None
