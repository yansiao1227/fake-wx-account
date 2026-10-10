"""通道配置归属与旧准入设置移除回归。"""

import pytest

from channel.wechat_desktop.config import DEFAULT_CONFIG, load_wechat_desktop_config


REMOVED_REPLY_KEYS = (
    "auto_reply_private_all",
    "auto_reply_groups_all",
    "auto_reply_blacklist",
    "auto_reply_contacts",
    "auto_reply_groups",
)
BLACKLIST_KEYS = ("auto_reply_private_blacklist", "auto_reply_group_blacklist")


def test_load_wechat_desktop_config_defaults_and_override():
    loaded = load_wechat_desktop_config({})
    for key in BLACKLIST_KEYS:
        assert loaded[key] == DEFAULT_CONFIG[key] == []
    assert loaded["shadow_mode"] is DEFAULT_CONFIG["shadow_mode"]
    assert loaded["group_reply_mode"] == "at_only"
    overridden = load_wechat_desktop_config({"shadow_mode": not loaded["shadow_mode"]})
    assert overridden["shadow_mode"] is not loaded["shadow_mode"]


def test_removed_reply_keys_are_discarded_from_channel_overrides():
    raw = {
        "auto_reply_private_all": False,
        "auto_reply_groups_all": False,
        "auto_reply_blacklist": ["旧黑名单"],
        "auto_reply_contacts": ["旧私聊白名单"],
        "auto_reply_groups": ["旧群聊白名单"],
        "auto_reply_private_blacklist": ["新私聊黑名单"],
        "auto_reply_group_blacklist": ["新群聊黑名单"],
    }
    loaded = load_wechat_desktop_config(raw)
    assert not set(REMOVED_REPLY_KEYS).intersection(DEFAULT_CONFIG)
    assert not set(REMOVED_REPLY_KEYS).intersection(loaded)
    assert loaded["auto_reply_private_blacklist"] == ["新私聊黑名单"]
    assert loaded["auto_reply_group_blacklist"] == ["新群聊黑名单"]
    assert set(REMOVED_REPLY_KEYS).issubset(raw)


@pytest.mark.parametrize("key", BLACKLIST_KEYS)
@pytest.mark.parametrize("invalid", [None, "一个会话", True, {}, [123], ["有效名字", None]])
def test_blacklists_require_lists_of_strings(key, invalid):
    with pytest.raises(ValueError, match=key + " must be a list of strings"):
        load_wechat_desktop_config({key: invalid})


@pytest.mark.parametrize("key", BLACKLIST_KEYS)
def test_channel_blacklist_override_does_not_mutate_default_or_input(key):
    raw = {key: ["一个会话"]}
    loaded = load_wechat_desktop_config(raw)
    loaded[key].append("后来新增")
    assert raw[key] == ["一个会话"]
    assert DEFAULT_CONFIG[key] == []
    assert load_wechat_desktop_config({})[key] == []


def test_global_config_drops_wechat_desktop_and_removed_reply_keys():
    from config import Config, _drop_wechat_desktop_keys_from_global

    misplaced = {
        "shadow_mode": True,
        "wechat_desktop": {"auto_reply_groups": ["误放在外层的群"]},
        **{key: ["误放在外层的会话"] for key in BLACKLIST_KEYS + REMOVED_REPLY_KEYS + ("group_name_white_list", "group_name_keyword_white_list")},
    }
    loaded = Config({"model": "keep-me", **misplaced})
    dropped = _drop_wechat_desktop_keys_from_global(loaded)
    assert set(misplaced).issubset(dropped)
    assert not set(misplaced).intersection(loaded)
    assert loaded.get("model") == "keep-me"


def test_load_wechat_desktop_config_ignores_json_section(monkeypatch):
    import channel.wechat_desktop.config as desktop_config

    monkeypatch.setattr(desktop_config, "conf", lambda: {
        "wechat_desktop": {"shadow_mode": not DEFAULT_CONFIG["shadow_mode"]},
    })
    loaded = load_wechat_desktop_config()
    assert loaded["shadow_mode"] is DEFAULT_CONFIG["shadow_mode"]
