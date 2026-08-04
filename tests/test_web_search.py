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
    assert module.multi_source_providers() == ["tavily", "ddgs"]
    assert module.WebSearch()._resolve_provider(None) == "tavily"


def test_baidu_is_preferred_before_tavily_and_ddgs(monkeypatch):
    monkeypatch.setattr(
        module,
        "_get_api_key",
        lambda provider: "configured" if provider in {"qianfan", "tavily"} else "",
    )
    monkeypatch.setattr(module, "DDGS", object())

    assert module.configured_providers() == ["qianfan", "tavily", "ddgs"]
    assert module.multi_source_providers() == ["qianfan", "tavily", "ddgs"]
    # Auto strategy resolves the full multi-source trio.
    assert module.WebSearch()._resolve_search_providers(None) == [
        "qianfan",
        "tavily",
        "ddgs",
    ]


def test_auto_strategy_ignores_ddgs_hint_and_fans_out(monkeypatch):
    tool = module.WebSearch()
    monkeypatch.setattr(
        module, "configured_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(module, "multi_source_providers", lambda: ["qianfan", "tavily", "ddgs"])
    monkeypatch.setattr(module, "_configured_strategy", lambda: "auto")
    monkeypatch.setattr(
        tool,
        "_rewrite_query",
        lambda query: {
            "query": query,
            "altered_query": query,
            "decomposition_query": [],
            "rewritten": False,
        },
    )
    calls = []

    def execute_provider(provider, query, count, freshness, summary):
        calls.append(provider)
        return module.ToolResult.success(
            {
                "backend": provider,
                "results": [
                    {
                        "title": f"{provider} hit",
                        "url": f"https://example.cn/{provider}",
                        "snippet": "example",
                    }
                ],
            }
        )

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)
    monkeypatch.setattr(tool, "_reserve_provider", lambda provider: (True, 1, None))

    result = tool.execute({"query": "example", "provider": "ddgs"})

    assert result.status == "success"
    assert result.result["backend"] == "multi"
    assert set(result.result["backends"]) == {"qianfan", "tavily", "ddgs"}
    assert set(calls) == {"qianfan", "tavily", "ddgs"}
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


def test_query_rewrite_uses_qianfan_key_and_endpoint(monkeypatch):
    calls = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "altered_query": "北京今日天气 旅行计划",
                "code": 0,
                "decomposition_query": ["北京今日天气", "北京旅行建议"],
                "message": "success",
                "requestId": "req-1",
            }

    def post(url, **kwargs):
        calls.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(module, "_get_api_key", lambda provider: "qf-secret")
    monkeypatch.setattr(module.requests, "post", post)

    rewrite = module.WebSearch()._rewrite_query("北京的天气怎样，适合制定怎样的旅行计划")

    assert calls["url"] == "https://qianfan.baidubce.com/v2/tools/query_rewrite"
    assert calls["headers"]["Authorization"] == "Bearer qf-secret"
    assert calls["json"] == {
        "query": "北京的天气怎样，适合制定怎样的旅行计划"
    }
    assert rewrite["altered_query"] == "北京今日天气 旅行计划"
    assert rewrite["decomposition_query"] == ["北京今日天气", "北京旅行建议"]
    assert rewrite["rewritten"] is True
    assert rewrite["request_id"] == "req-1"


def test_query_rewrite_failure_falls_back_to_original(monkeypatch):
    monkeypatch.setattr(module, "_get_api_key", lambda provider: "qf-secret")

    def post(*args, **kwargs):
        raise module.requests.Timeout("boom")

    monkeypatch.setattr(module.requests, "post", post)
    rewrite = module.WebSearch()._rewrite_query("原始问题")

    assert rewrite["altered_query"] == "原始问题"
    assert rewrite["rewritten"] is False


def test_execute_rewrites_then_fans_out_to_baidu_tavily_ddgs(monkeypatch):
    tool = module.WebSearch()
    monkeypatch.setattr(
        module, "configured_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(
        module, "multi_source_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(
        tool,
        "_rewrite_query",
        lambda query: {
            "query": query,
            "altered_query": "optimized query",
            "decomposition_query": ["sub a", "sub b"],
            "rewritten": True,
            "request_id": "r1",
        },
    )
    seen = []

    def execute_provider(provider, query, count, freshness, summary):
        seen.append((provider, query))
        return module.ToolResult.success(
            {
                "backend": provider,
                "results": [
                    {
                        "title": f"{provider} about optimized",
                        "url": f"https://example.com/{provider}",
                        "snippet": f"{provider} snippet optimized query",
                        "score": 0.9 if provider == "tavily" else None,
                    }
                ],
            }
        )

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)
    monkeypatch.setattr(tool, "_reserve_provider", lambda provider: (True, 1, None))

    result = tool.execute({"query": "raw user query", "count": 5})

    assert result.status == "success"
    assert result.result["query"] == "raw user query"
    assert result.result["altered_query"] == "optimized query"
    assert result.result["decomposition_query"] == ["sub a", "sub b"]
    assert result.result["backend"] == "multi"
    assert set(result.result["backends"]) == {"qianfan", "tavily", "ddgs"}
    assert {p for p, q in seen} == {"qianfan", "tavily", "ddgs"}
    assert all(q == "optimized query" for _, q in seen)
    assert result.result["count"] == 3
    assert all("score" in item for item in result.result["results"])


def test_multi_source_partial_failure_still_returns_ranked_hits(monkeypatch):
    tool = module.WebSearch()
    monkeypatch.setattr(
        module, "configured_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(
        module, "multi_source_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(
        tool,
        "_rewrite_query",
        lambda query: {
            "query": query,
            "altered_query": query,
            "decomposition_query": [],
            "rewritten": False,
        },
    )

    def execute_provider(provider, query, count, freshness, summary):
        if provider == "qianfan":
            return module.ToolResult.fail("Error: qianfan unavailable")
        return module.ToolResult.success(
            {
                "backend": provider,
                "results": [
                    {
                        "title": f"{provider} title example",
                        "url": f"https://example.com/{provider}",
                        "snippet": "example snippet",
                    }
                ],
            }
        )

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)
    monkeypatch.setattr(tool, "_reserve_provider", lambda provider: (True, 1, None))

    result = tool.execute({"query": "example"})

    assert result.status == "success"
    assert "qianfan" not in result.result["backends"]
    assert set(result.result["backends"]) == {"tavily", "ddgs"}
    assert any("qianfan" in w.lower() or "Baidu" in w for w in result.result["provider_warnings"])


def test_merge_and_rank_prefers_more_relevant_and_multi_source_hits():
    payloads = [
        (
            "qianfan",
            [
                {
                    "title": "北京今日天气实况",
                    "url": "https://weather.example/bj",
                    "snippet": "北京今天晴，气温适宜出行",
                },
                {
                    "title": "无关新闻",
                    "url": "https://news.example/other",
                    "snippet": "股市波动",
                },
            ],
        ),
        (
            "tavily",
            [
                {
                    "title": "Beijing weather today",
                    "url": "https://weather.example/bj",
                    "snippet": "Sunny in Beijing",
                    "score": 0.95,
                }
            ],
        ),
        (
            "ddgs",
            [
                {
                    "title": "旅游攻略",
                    "url": "https://travel.example/guide",
                    "snippet": "通用旅行建议",
                }
            ],
        ),
    ]
    ranked = module.merge_and_rank_results(
        "北京天气", "北京今日天气", payloads, limit=3
    )

    assert ranked[0]["url"] == "https://weather.example/bj"
    assert set(ranked[0]["sources"]) == {"qianfan", "tavily"}
    assert ranked[0]["score"] >= ranked[-1]["score"]


def test_baidu_then_tavily_then_ddgs_fallback_when_primary_empty(monkeypatch):
    """If multi-source trio is empty of usable results, remaining backends are tried."""
    tool = module.WebSearch()
    # Simulate only the trio configured; all fail then nothing remaining.
    monkeypatch.setattr(
        module, "configured_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(
        module, "multi_source_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(
        tool,
        "_rewrite_query",
        lambda query: {
            "query": query,
            "altered_query": query,
            "decomposition_query": [],
            "rewritten": False,
        },
    )
    calls = []

    def execute_provider(provider, query, count, freshness, summary):
        calls.append(provider)
        if provider != "ddgs":
            return module.ToolResult.fail(f"Error: {provider} unavailable")
        return module.ToolResult.success(
            {
                "backend": "ddgs",
                "results": [
                    {
                        "title": "ddgs result example",
                        "url": "https://example.com/ddgs",
                        "snippet": "example",
                    }
                ],
            }
        )

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)
    monkeypatch.setattr(
        tool, "_reserve_provider", lambda provider: (True, 1, None)
    )

    result = tool.execute({"query": "example"})

    assert set(calls) == {"qianfan", "tavily", "ddgs"}
    assert result.status == "success"
    assert result.result["backend"] == "ddgs"
    assert result.result["results"][0]["url"] == "https://example.com/ddgs"


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
    monkeypatch.setattr(module, "multi_source_providers", lambda: ["tavily", "ddgs"])
    monkeypatch.setattr(
        tool,
        "_rewrite_query",
        lambda query: {
            "query": query,
            "altered_query": query,
            "decomposition_query": [],
            "rewritten": False,
        },
    )
    calls = []

    def execute_provider(provider, query, count, freshness, summary):
        calls.append(provider)
        if provider == "tavily":
            return module.ToolResult.fail("Error: Tavily API quota reached.")
        return module.ToolResult.success(
            {
                "backend": "ddgs",
                "results": [
                    {
                        "title": "ddgs hit",
                        "url": "https://example.com",
                        "snippet": "example",
                    }
                ],
            }
        )

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)
    monkeypatch.setattr(
        tool, "_reserve_provider", lambda provider: (True, 1, None)
    )

    result = tool.execute({"query": "example"})

    assert set(calls) == {"tavily", "ddgs"}
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
    monkeypatch.setattr(
        module.WebSearch,
        "_resolve_search_providers",
        lambda self, requested: ["ddgs"],
    )
    monkeypatch.setattr(
        module.WebSearch,
        "_rewrite_query",
        lambda self, query: {
            "query": query,
            "altered_query": query,
            "decomposition_query": [],
            "rewritten": False,
        },
    )
    monkeypatch.setattr(
        module.WebSearch,
        "_search_ddgs",
        lambda self, query, count, freshness: module.ToolResult.success(
            {
                "results": [
                    {
                        "title": "t",
                        "url": "https://example.com",
                        "snippet": "s",
                    }
                ]
            }
        ),
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


def test_exhausted_baidu_is_skipped_before_network_then_uses_others(
    monkeypatch, tmp_path
):
    tool = module.WebSearch({"usage_store_path": str(tmp_path / "usage.db")})
    monkeypatch.setattr(
        module, "configured_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(
        module, "multi_source_providers", lambda: ["qianfan", "tavily", "ddgs"]
    )
    monkeypatch.setattr(
        tool,
        "_rewrite_query",
        lambda query: {
            "query": query,
            "altered_query": query,
            "decomposition_query": [],
            "rewritten": False,
        },
    )
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
        return module.ToolResult.success(
            {
                "backend": provider,
                "results": [
                    {
                        "title": f"{provider} title",
                        "url": f"https://example.com/{provider}",
                        "snippet": "example",
                    }
                ],
            }
        )

    monkeypatch.setattr(tool, "_execute_provider", execute_provider)

    result = tool.execute({"query": "example"})

    assert result.status == "success"
    assert "qianfan" not in calls
    assert set(calls) == {"tavily", "ddgs"}
    assert tool._get_usage_store().count("tavily", "month") == 1
