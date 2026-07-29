from agent.tools.web_search import web_search as module


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

    result = module.WebSearch().execute({"query": "example"})

    assert result.status == "success"
    assert result.result["citationPolicy"] == module.CITATION_POLICY
    assert "clickable source links" in result.result["citationPolicy"]
