"""共享聊天通道的群准入与会话隔离回归；不启动消费线程或真实微信。"""

from types import SimpleNamespace

import pytest

import channel.chat_channel as chat_channel
from bridge.context import ContextType


GROUP_NAME = "萌新打怪躺平日记"
GROUP_ID = "group-1"


class ConfigStub(dict):
    def get_user_data(self, user_id):
        return {}


@pytest.fixture
def compose_group_context(monkeypatch):
    config = ConfigStub(
        group_chat_prefix=[],
        group_chat_keyword=[],
        group_at_off=False,
        image_create_prefix=[],
    )
    monkeypatch.setattr(chat_channel, "conf", lambda: config)
    plugin_manager = SimpleNamespace(emit_event=lambda event: event)
    monkeypatch.setattr(chat_channel, "PluginManager", lambda: plugin_manager)

    channel = object.__new__(chat_channel.ChatChannel)
    channel.name = "机器人"
    channel.user_id = "bot-1"

    def compose(settings=None, *, sender_id="member-1", content="@机器人\u2005你好", is_at=True):
        config.update(settings or {})
        message = SimpleNamespace(
            from_user_id=GROUP_ID,
            other_user_id=GROUP_ID,
            other_user_nickname=GROUP_NAME,
            actual_user_id=sender_id,
            actual_user_nickname="群成员",
            to_user_id=channel.user_id,
            is_at=is_at,
            at_list=[],
            self_display_name=channel.name,
        )
        return channel._compose_context(ContextType.TEXT, content, msg=message, isgroup=True)

    return compose


@pytest.mark.parametrize("legacy_names", [[], ["其他群"]])
def test_new_group_at_is_admitted_regardless_of_legacy_whitelists(compose_group_context, legacy_names):
    context = compose_group_context(
        {
            "group_name_white_list": legacy_names,
            "group_name_keyword_white_list": legacy_names,
        }
    )

    assert context is not None
    assert context.content == "你好"
    assert context["receiver"] == GROUP_ID
    assert context["session_id"] == GROUP_ID


@pytest.mark.parametrize(
    ("settings", "content", "is_at", "expected_content"),
    [
        ({}, "普通消息", False, None),
        ({"group_at_off": True}, "@机器人\u2005你好", True, None),
        ({"group_chat_prefix": ["#"]}, "# 你好", False, "你好"),
        ({"group_chat_keyword": ["请帮忙"]}, "请帮忙看一下", False, "请帮忙看一下"),
    ],
)
def test_group_admission_preserves_message_triggers(
    compose_group_context, settings, content, is_at, expected_content
):
    context = compose_group_context(settings, content=content, is_at=is_at)

    if expected_content is None:
        assert context is None
    else:
        assert context is not None
        assert context.content == expected_content


@pytest.mark.parametrize(
    ("shared_session", "group_sessions", "expected_sessions"),
    [
        (True, [], (GROUP_ID, GROUP_ID)),
        (False, [], ("member-1", "member-2")),
        (False, ["其他群"], ("member-1", "member-2")),
        (False, [GROUP_NAME], (GROUP_ID, GROUP_ID)),
        (False, ["ALL_GROUP"], (GROUP_ID, GROUP_ID)),
    ],
)
def test_group_session_sharing_is_independent_of_admission(
    compose_group_context, shared_session, group_sessions, expected_sessions
):
    settings = {
        "group_shared_session": shared_session,
        "group_chat_in_one_session": group_sessions,
        "group_name_white_list": [],
        "group_name_keyword_white_list": [],
    }
    first = compose_group_context(settings, sender_id="member-1")
    second = compose_group_context(sender_id="member-2")

    assert first is not None
    assert second is not None
    assert (first["session_id"], second["session_id"]) == expected_sessions
    assert first["receiver"] == second["receiver"] == GROUP_ID
