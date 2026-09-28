from agent.tools.base_tool import ToolResult
from agent.tools.ai_web_search import ai_web_search as module


def test_is_available_ignores_unused_search_key(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "ark-test-key")
    monkeypatch.setattr(module, "_tools_ai_web_search_conf", lambda: {})

    import agent.tools.baidu_ai_search.baidu_ai_search as ai_mod

    monkeypatch.setattr(ai_mod.BaiduAiSearch, "is_available", staticmethod(lambda: False))
    assert module.AiWebSearch.is_available() is False


def test_is_available_when_baidu_ai_search_can_serve(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(module, "_tools_ai_web_search_conf", lambda: {})

    import agent.tools.baidu_ai_search.baidu_ai_search as ai_mod

    monkeypatch.setattr(ai_mod.BaiduAiSearch, "is_available", staticmethod(lambda: True))
    assert module.AiWebSearch.is_available() is True


def test_execute_routes_to_baidu(monkeypatch):
    monkeypatch.setattr(module, "_tools_ai_web_search_conf", lambda: {})
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "unused-search-key")

    import agent.tools.baidu_ai_search.baidu_ai_search as ai_mod

    calls = []

    class FakeBaidu:
        def __init__(self, config=None):
            self.config = config or {}

        @staticmethod
        def is_available():
            return True

        def execute(self, args):
            calls.append(args)
            return ToolResult.success(
                {
                    "backend": "baidu_ai_search",
                    "query": args["query"],
                    "answer": "from ai",
                }
            )

    monkeypatch.setattr(ai_mod, "BaiduAiSearch", FakeBaidu)
    tool = module.AiWebSearch()
    result = tool.execute({"query": "北京天气", "count": 5, "freshness": "oneDay"})
    assert result.status == "success"
    assert result.result["backend"] == "baidu_ai_search"
    assert result.result["fallback_from"] == "ai_web_search"
    assert "baidu_ai_search" in result.result["fallback_reason"]
    assert result.result["query"] == "北京天气"
    assert "citationPolicy" in result.result
    assert calls == [{"query": "北京天气", "count": 5, "freshness": "oneDay"}]


def test_missing_query(monkeypatch):
    monkeypatch.setattr(module, "_tools_ai_web_search_conf", lambda: {})
    tool = module.AiWebSearch()
    result = tool.execute({})
    assert result.status != "success"
    assert "query" in str(result.result).lower()


def test_fallback_disabled(monkeypatch):
    monkeypatch.setattr(
        module,
        "_tools_ai_web_search_conf",
        lambda: {"fallback_to_baidu_ai_search": False},
    )
    tool = module.AiWebSearch()
    result = tool.execute({"query": "hello"})
    assert result.status != "success"
    assert "fallback_to_baidu_ai_search is disabled" in str(result.result)


def test_reads_legacy_doubao_search_conf(monkeypatch):
    monkeypatch.setattr(
        module,
        "conf",
        lambda: {"tools": {"doubao_search": {"fallback_to_baidu_ai_search": False}}},
    )
    assert module._tools_ai_web_search_conf()["fallback_to_baidu_ai_search"] is False


def test_prefers_ai_web_search_conf_over_legacy(monkeypatch):
    monkeypatch.setattr(
        module,
        "conf",
        lambda: {
            "tools": {
                "doubao_search": {"fallback_to_baidu_ai_search": False},
                "ai_web_search": {"fallback_to_baidu_ai_search": True},
            }
        },
    )
    assert module._tools_ai_web_search_conf()["fallback_to_baidu_ai_search"] is True
