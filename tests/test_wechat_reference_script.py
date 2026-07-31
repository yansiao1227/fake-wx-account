from scripts.inspect_wechat_reference import (
    _parse_reference_text,
    _pick_original_message,
    _reference_kind,
)


def test_parse_flattened_reference_keeps_current_and_quoted_messages_separate():
    parsed = _parse_reference_text(
        "测试引用引用 颜料盒 的消息 : 高考英语阅读核心单词(T) new.doc"
    )

    assert parsed == {
        "current_message": "测试引用",
        "quoted_sender": "颜料盒",
        "quoted_preview": "高考英语阅读核心单词(T) new.doc",
    }


def test_parse_reference_stops_after_one_level():
    parsed = _parse_reference_text(
        "看看这个引用 Alice 的消息 : Bob 引用 Carol 的消息 : 原文"
    )

    assert parsed["current_message"] == "看看这个"
    assert parsed["quoted_sender"] == "Alice"
    assert parsed["quoted_preview"] == "Bob 引用 Carol 的消息 : 原文"


def test_pick_original_text_prefers_full_preview_match_near_viewport_centre():
    messages = [
        {"class_name": "mmui::ChatTextItemView", "bounds": [0, 10, 100, 50], "name": "other"},
        {
            "class_name": "mmui::ChatTextItemView",
            "bounds": [0, 80, 100, 180],
            "name": "preview followed by the complete text",
        },
    ]

    selected = _pick_original_message(messages, [0, 0, 100, 240], "text", "preview")

    assert selected["name"] == "preview followed by the complete text"


def test_pick_original_file_prefers_file_card():
    messages = [
        {"class_name": "mmui::ChatTextItemView", "bounds": [0, 80, 100, 120], "name": "notes.doc"},
        {
            "class_name": "mmui::ChatBubbleItemView",
            "bounds": [0, 130, 100, 210],
            "name": "file\nnotes.doc\n108.5K",
        },
    ]

    selected = _pick_original_message(messages, [0, 0, 100, 280], "file", "notes.doc")

    assert selected["name"].endswith("notes.doc\n108.5K")


def test_reference_kind_covers_current_test_cases():
    assert _reference_kind("图片") == "image"
    assert _reference_kind("高考英语阅读核心单词(T) new.doc") == "file"
    assert _reference_kind("这是一段普通引用消息") == "text"
