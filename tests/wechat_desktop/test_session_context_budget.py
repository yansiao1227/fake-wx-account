"""小字符预算下必须保留最新连续轮次，不能回退注入更旧话题。"""

import pytest

from channel.wechat_desktop.pipeline.session_context import (
    compact_session_messages,
    text_of,
    user_message,
)


def _turn(user, reply):
    return [user_message(user), {"role": "assistant", "content": reply}]


@pytest.mark.parametrize(
    "user,reply,max_chars",
    [
        ("新" * 40, "答" * 40, 1),
        ("新" * 40, "答" * 40, 6),
        ("新" * 40, "答" * 40, 7),
        ("新" * 40, "答" * 40, 12),
        ("新" * 40, "答" * 40, 13),
        ("新", "答" * 40, 7),
        ("新" * 40, "答", 7),
        ("新" * 40, "答", 13),
    ],
    ids=["budget_1", "budget_6", "budget_7", "budget_12", "budget_13",
         "short_user_budget_7", "short_reply_budget_7", "short_reply_budget_13"],
)
def test_unrepresentable_latest_turn_does_not_fall_back_to_older_history(user, reply, max_chars):
    messages = _turn("旧问", "旧答") + _turn(user, reply)

    compact = compact_session_messages(messages, max_turns=2, max_chars=max_chars)

    assert sum(len(text_of(message)) for message in compact) <= max_chars
    assert compact == []


@pytest.mark.parametrize(
    "user,reply,max_chars,expected",
    [
        ("新" * 40, "答" * 40, 14, ["新…[已截断]", "答…[已截断]"]),
        ("新" * 40, "答" * 40, 15, ["新…[已截断]", "答答…[已截断]"]),
        ("新", "答" * 40, 8, ["新", "答…[已截断]"]),
        ("新", "答" * 40, 9, ["新", "答答…[已截断]"]),
        ("新" * 40, "答", 14, ["旧问", "旧答", "新…[已截断]", "答"]),
        ("新问", "新答", 4, ["新问", "新答"]),
        ("新问", "新答", 8, ["旧问", "旧答", "新问", "新答"]),
    ],
    ids=["two_markers_fit", "one_more_reply_character", "short_user_marker_fits",
         "short_user_one_more_reply_character", "short_reply_marker_fits",
         "complete_latest_pair_fits", "two_complete_pairs_fit"],
)
def test_budget_boundaries_keep_latest_turn_and_existing_truncation_marker(user, reply, max_chars, expected):
    messages = _turn("旧问", "旧答") + _turn(user, reply)

    compact = compact_session_messages(messages, max_turns=2, max_chars=max_chars)

    assert [text_of(message) for message in compact] == expected
    assert sum(len(text_of(message)) for message in compact) <= max_chars
    assert [message["role"] for message in compact] == ["user", "assistant"] * (len(expected) // 2)


@pytest.mark.parametrize("max_chars", [10, 11, 16, 17])
def test_unrepresentable_middle_turn_does_not_open_a_gap_in_retained_history(max_chars):
    messages = _turn("旧问", "旧答") + _turn("中" * 40, "间" * 40) + _turn("新问", "新答")

    compact = compact_session_messages(messages, max_turns=3, max_chars=max_chars)

    assert [text_of(message) for message in compact] == ["新问", "新答"]
    assert sum(len(text_of(message)) for message in compact) <= max_chars
