import pytest

from common.windows_hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    parse_hotkey,
)


def test_parse_emergency_hotkey():
    hotkey = parse_hotkey("ctrl+alt+shift+q")
    assert hotkey.display == "Ctrl+Alt+Shift+Q"
    assert hotkey.virtual_key == ord("Q")
    assert hotkey.modifiers == MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT


@pytest.mark.parametrize("value", ["", "ctrl+alt", "ctrl+q+x", "ctrl+mouse1"])
def test_reject_invalid_emergency_hotkey(value):
    with pytest.raises(ValueError):
        parse_hotkey(value)


def test_parse_function_key_hotkey():
    hotkey = parse_hotkey("ctrl+shift+f12")
    assert hotkey.display == "Ctrl+Shift+F12"
    assert hotkey.virtual_key == 0x7B
