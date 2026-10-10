"""通道配置归属回归。"""

from channel.wechat_desktop.config import DEFAULT_CONFIG, load_wechat_desktop_config


def test_load_wechat_desktop_config_defaults_and_override():
    loaded = load_wechat_desktop_config({})
    assert loaded['auto_reply_groups'] == DEFAULT_CONFIG['auto_reply_groups']
    assert loaded['shadow_mode'] is DEFAULT_CONFIG['shadow_mode']
    overridden = load_wechat_desktop_config({'shadow_mode': not loaded['shadow_mode']})
    assert overridden['shadow_mode'] is not loaded['shadow_mode']


def test_global_config_drops_wechat_desktop_keys():
    from config import Config, _drop_wechat_desktop_keys_from_global

    loaded = Config(
        {
            "model": "keep-me",
            "shadow_mode": True,
            "auto_reply_groups": ["误放在外层的群"],
            "wechat_desktop": {"shadow_mode": True},
        }
    )
    dropped = _drop_wechat_desktop_keys_from_global(loaded)
    assert "shadow_mode" in dropped
    assert "auto_reply_groups" in dropped
    assert "wechat_desktop" in dropped
    assert "shadow_mode" not in loaded
    assert "auto_reply_groups" not in loaded
    assert "wechat_desktop" not in loaded
    assert loaded.get("model") == "keep-me"



def test_load_wechat_desktop_config_ignores_json_section(monkeypatch):
    import channel.wechat_desktop.config as desktop_config

    monkeypatch.setattr(desktop_config, 'conf', lambda: {
        'wechat_desktop': {'shadow_mode': not DEFAULT_CONFIG['shadow_mode']},
    })
    loaded = load_wechat_desktop_config()
    assert loaded['shadow_mode'] is DEFAULT_CONFIG['shadow_mode']
