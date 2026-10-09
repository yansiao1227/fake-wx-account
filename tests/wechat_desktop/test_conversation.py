"""会话标题和稳定选择器回归。"""

from channel.wechat_desktop.conversation import conversation_titles_match, strip_member_count_suffix
from channel.wechat_desktop.models import ConversationInfo
from channel.wechat_desktop.uia.operations import resolve_conversation_selector


def test_conversation_titles_match_ignores_member_count_suffix():
    assert strip_member_count_suffix("小小地下联络站(9)") == "小小地下联络站"
    assert strip_member_count_suffix("小小地下联络站（12）") == "小小地下联络站"
    assert strip_member_count_suffix("小小地下联络站") == "小小地下联络站"
    assert conversation_titles_match("小小地下联络站", "小小地下联络站(9)")
    assert conversation_titles_match("小小地下联络站(9)", "小小地下联络站（10）")
    assert not conversation_titles_match("小小地下联络站", "测试群(3)")



def test_resolve_conversation_selector_matches_unique_title():
    row = ConversationInfo(
        conversation_title="小小地下联络站",
        runtime_id="42.1.2.3",
        row_index=2,
    )
    selectors = {"uia-session:42.1.2.3": row}
    selector = resolve_conversation_selector(selectors, "小小地下联络站")
    assert selector.title == "小小地下联络站"
    assert selector.runtime_id == "42.1.2.3"
    assert selector.row_index == 2



def test_resolve_conversation_selector_matches_title_with_member_suffix():
    row = ConversationInfo(
        conversation_title="小小地下联络站",
        runtime_id="42.1.2.3",
        row_index=0,
    )
    selectors = {"uia-session:42.1.2.3": row}
    selector = resolve_conversation_selector(selectors, "小小地下联络站(9)")
    assert selector.runtime_id == "42.1.2.3"



def test_resolve_conversation_selector_ignores_stale_uia_session_key():
    selector = resolve_conversation_selector({}, "uia-session:missing")
    assert selector.title == ""
    assert selector.runtime_id == ""
