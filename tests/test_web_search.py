from datetime import datetime

from agent.tools.web_search import web_search as module


def test_tavily_is_preferred_before_ddgs(monkeypatch):
    monkeypatch.setattr(
        module,
        "_get_api_key",
        lambda provider: "configured" if provider == "tavily" else "",
    )
    monkeypatch.setattr(module, "DDGS", object())

    assert module.configured_providers() == ["tavily", "ddgs"]
    assert module.WebSearch()._resolve_provider(None) == "tavily"


def test_baidu_is_preferred_before_tavily_and_ddgs(monkeypatch):
    monkeypatch.setattr(
        module,
        "_get_api_key",
        lambda provider: "configured" if provider in {"qianfan", "tavily"} else "",
    )
    monkeypatch.setattr(module, "DDGS", object())

    assert module.configured_providers() == ["qianfan", "tavily", "ddgs"]
    assert module.WebSearch()._resolve_provider(None) == "qianfan"


def test_auto_strategy_ignores_ddgs_hint_and_keeps_provider_priority(monkeypatch):
    tool = module.WebSearch()
    monkeypatch.setattr(
        module, "configured_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(module, "_configured_strategy", lambda: "auto")
    calls = []

    def execute_provider(provider, query, count, freshness, summary):
        calls.append(provider)
        return module.ToolResult.success(
            {"backend": provider, "results": [{"url": "https://example.cn"}]}
        )

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)
    monkeypatch.setattr(tool, "_reserve_provider", lambda provider: (True, 1, None))

    result = tool.execute({"query": "example", "provider": "ddgs"})

    assert result.status == "success"
    assert result.result["backend"] == "qianfan"
    assert calls == ["qianfan"]
    assert "provider" not in module.WebSearch.get_json_schema()["parameters"]["properties"]


def test_baidu_request_matches_official_search_api(monkeypatch):
    calls = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "references": [
                    {
                        "title": "百度结果",
                        "url": "https://example.cn/result",
                        "content": "结果摘要",
                        "website": "example.cn",
                        "date": "2026-07-30",
                    }
                ]
            }

    def post(url, **kwargs):
        calls.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(module, "_get_api_key", lambda provider: "baidu-secret")
    monkeypatch.setattr(module.requests, "post", post)
    query = "中文" * 40

    result = module.WebSearch()._search_qianfan(query, 5, "oneWeek")

    assert result.status == "success"
    assert result.result["backend"] == "qianfan"
    assert calls["url"] == "https://qianfan.baidubce.com/v2/ai_search/web_search"
    assert calls["headers"]["Authorization"] == "Bearer baidu-secret"
    assert calls["json"]["edition"] == "standard"
    assert calls["json"]["search_source"] == "baidu_search_v2"
    assert calls["json"]["search_recency_filter"] == "week"
    assert len(calls["json"]["messages"][0]["content"]) == 36


def test_baidu_then_tavily_then_ddgs_fallback_order(monkeypatch):
    tool = module.WebSearch()
    monkeypatch.setattr(
        module, "configured_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(tool, "_resolve_provider", lambda requested: "qianfan")
    calls = []

    def execute_provider(provider, query, count, freshness, summary):
        calls.append(provider)
        if provider != "ddgs":
            return module.ToolResult.fail(f"Error: {provider} unavailable")
        return module.ToolResult.success({"backend": "ddgs", "results": []})

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)
    monkeypatch.setattr(
        tool, "_reserve_provider", lambda provider: (True, 1, None)
    )

    result = tool.execute({"query": "example"})

    assert calls == ["qianfan", "tavily", "ddgs"]
    assert result.status == "success"
    assert result.result["backend"] == "ddgs"


def test_tavily_returns_unified_results(monkeypatch):
    calls = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "answer": "A short answer",
                "usage": {"credits": 1},
                "results": [
                    {
                        "title": "Example result",
                        "url": "https://example.com/article",
                        "content": "Useful search snippet.",
                        "score": 0.9,
                    }
                ],
            }

    def post(url, **kwargs):
        calls.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(module, "_get_api_key", lambda provider: "secret")
    monkeypatch.setattr(module.requests, "post", post)

    result = module.WebSearch()._search_tavily(
        "example query", 5, "oneWeek", True
    )

    assert result.status == "success"
    assert result.result["backend"] == "tavily"
    assert result.result["summary"] == "A short answer"
    assert result.result["usage"] == {"credits": 1}
    assert result.result["results"][0]["url"] == "https://example.com/article"
    assert calls["url"] == "https://api.tavily.com/search"
    assert calls["headers"]["Authorization"] == "Bearer secret"
    assert calls["json"] == {
        "query": "example query",
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": True,
        "include_raw_content": False,
        "time_range": "week",
    }


def test_tavily_failure_falls_back_to_ddgs(monkeypatch):
    tool = module.WebSearch()
    monkeypatch.setattr(module, "configured_providers", lambda: ["tavily", "ddgs"])
    monkeypatch.setattr(tool, "_resolve_provider", lambda requested: "tavily")
    calls = []

    def execute_provider(provider, query, count, freshness, summary):
        calls.append(provider)
        if provider == "tavily":
            return module.ToolResult.fail("Error: Tavily API quota reached.")
        return module.ToolResult.success(
            {"backend": "ddgs", "results": [{"url": "https://example.com"}]}
        )

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)
    monkeypatch.setattr(
        tool, "_reserve_provider", lambda provider: (True, 1, None)
    )

    result = tool.execute({"query": "example"})

    assert calls == ["tavily", "ddgs"]
    assert result.status == "success"
    assert result.result["backend"] == "ddgs"


def test_ddgs_keeps_web_search_available_without_api_keys(monkeypatch):
    monkeypatch.setattr(module, "_get_api_key", lambda provider: "")
    monkeypatch.setattr(module, "DDGS", object())

    assert module.configured_providers() == ["ddgs"]
    assert module.WebSearch.is_available() is True
    assert module.WebSearch()._resolve_provider(None) == "ddgs"


def test_ddgs_returns_unified_results(monkeypatch):
    calls = {}

    class FakeDDGS:
        def __init__(self, timeout):
            calls["timeout"] = timeout

        def text(self, query, **kwargs):
            calls["query"] = query
            calls["kwargs"] = kwargs
            return [{
                "title": "Example result",
                "href": "https://example.com/article",
                "body": "Useful search snippet.",
            }]

    monkeypatch.setattr(module, "DDGS", FakeDDGS)
    result = module.WebSearch()._search_ddgs("example query", 5, "oneWeek")

    assert calls == {
        "timeout": module.DEFAULT_TIMEOUT,
        "query": "example query",
        "kwargs": {
            "region": "cn-zh",
            "safesearch": "moderate",
            "max_results": 5,
            "backend": "bing,brave",
            "timelimit": "w",
        },
    }
    assert result.status == "success"
    assert result.result["backend"] == "ddgs"
    assert result.result["count"] == 1
    assert result.result["results"] == [{
        "title": "Example result",
        "url": "https://example.com/article",
        "snippet": "Useful search snippet.",
        "siteName": "example.com",
        "datePublished": "",
    }]


def test_ddgs_no_results_exception_is_a_successful_empty_search(monkeypatch):
    class EmptyDDGS:
        def __init__(self, timeout):
            pass

        def text(self, query, **kwargs):
            raise module.DDGSException("No results found")

    monkeypatch.setattr(module, "DDGS", EmptyDDGS)

    result = module.WebSearch()._search_ddgs("missing query", 5, "noLimit")

    assert result.status == "success"
    assert result.result["backend"] == "ddgs"
    assert result.result["count"] == 0
    assert result.result["results"] == []


def test_ddgs_is_not_configured_when_dependency_is_missing(monkeypatch):
    monkeypatch.setattr(module, "_get_api_key", lambda provider: "")
    monkeypatch.setattr(module, "DDGS", None)

    assert module.configured_providers() == []
    assert module.WebSearch.is_available() is False


def test_tool_schema_requires_source_links():
    description = module.WebSearch.get_json_schema()["description"]

    assert "cite" in description.lower()
    assert "URLs" in description


def test_execute_attaches_citation_policy(monkeypatch):
    monkeypatch.setattr(module.WebSearch, "_resolve_provider", lambda self, requested: "ddgs")
    monkeypatch.setattr(
        module.WebSearch,
        "_search_ddgs",
        lambda self, query, count, freshness: module.ToolResult.success({"results": []}),
    )
    monkeypatch.setattr(
        module.WebSearch,
        "_reserve_provider",
        lambda self, provider: (True, 1, None),
    )

    result = module.WebSearch().execute({"query": "example"})

    assert result.status == "success"
    assert result.result["citationPolicy"] == module.CITATION_POLICY
    assert "clickable source links" in result.result["citationPolicy"]


def test_provider_quota_defaults_match_free_allowances():
    assert module.WebSearch._provider_quota("qianfan") == ("day", 50)
    assert module.WebSearch._provider_quota("tavily") == ("month", 1000)
    assert module.WebSearch._provider_quota("ddgs") == ("all", None)


def test_usage_store_persists_and_resets_by_period(tmp_path):
    path = tmp_path / "web-search-usage.db"
    first = module.WebSearchUsageStore(str(path))
    day_one = datetime(2026, 7, 30, 23, 59)
    day_two = datetime(2026, 7, 31, 0, 1)

    assert first.reserve("qianfan", "day", 2, day_one) == (True, 1)
    assert first.reserve("qianfan", "day", 2, day_one) == (True, 2)
    assert first.reserve("qianfan", "day", 2, day_one) == (False, 2)

    reopened = module.WebSearchUsageStore(str(path))
    assert reopened.count("qianfan", "day", day_one) == 2
    assert reopened.reserve("qianfan", "day", 2, day_two) == (True, 1)


def test_exhausted_baidu_is_skipped_before_network_then_uses_tavily(
    monkeypatch, tmp_path
):
    tool = module.WebSearch({"usage_store_path": str(tmp_path / "usage.db")})
    monkeypatch.setattr(
        module, "configured_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(tool, "_resolve_provider", lambda requested: "qianfan")
    monkeypatch.setattr(
        tool,
        "_provider_quota",
        lambda provider: ("day", 1)
        if provider == "qianfan"
        else (("month", 10) if provider == "tavily" else ("all", None)),
    )
    tool._get_usage_store().reserve("qianfan", "day", 1)
    calls = []

    def execute_provider(provider, query, count, freshness, summary):
        calls.append(provider)
        return module.ToolResult.success({"backend": provider, "results": []})

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)

    result = tool.execute({"query": "example"})

    assert result.status == "success"
    assert result.result["backend"] == "tavily"
    assert calls == ["tavily"]
    assert tool._get_usage_store().count("tavily", "month") == 1
