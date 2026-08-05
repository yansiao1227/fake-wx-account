from agent.tools.base_tool import ToolResult
from agent.tools.doubao_search import doubao_search as module


def test_is_available_reads_env(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(module, "_tools_doubao_search_conf", lambda: {})

    import agent.tools.baidu_ai_search.baidu_ai_search as ai_mod

    monkeypatch.setattr(ai_mod.BaiduAiSearch, "is_available", staticmethod(lambda: False))
    assert module.DoubaoSearch.is_available() is False

    monkeypatch.setenv("WEB_SEARCH_API_KEY", "ark-test-key")
    assert module.DoubaoSearch.is_available() is True


def test_is_available_when_baidu_ai_search_can_serve(monkeypatch):
    monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("DOUBAO_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(module, "_tools_doubao_search_conf", lambda: {})
    monkeypatch.setattr(module, "_get_api_key", lambda: "")

    import agent.tools.baidu_ai_search.baidu_ai_search as ai_mod

    monkeypatch.setattr(ai_mod.BaiduAiSearch, "is_available", staticmethod(lambda: True))
    assert module.DoubaoSearch.is_available() is True


def test_map_time_range():
    assert module._map_time_range("oneWeek") == "OneWeek"
    assert module._map_time_range("noLimit") is None
    assert module._map_time_range("2025-01-01..2025-02-01") == "2025-01-01..2025-02-01"


def test_search_request_shape(monkeypatch):
    calls = {}

    class Response:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {
                "Result": {
                    "ResultCount": 1,
                    "WebResults": [
                        {
                            "Title": "示例标题",
                            "Url": "https://example.cn/a",
                            "Snippet": "摘要",
                            "Summary": "更长摘要",
                            "SiteName": "example",
                            "PublishTime": "2026-08-01",
                        }
                    ],
                }
            }

    def post(url, **kwargs):
        calls.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(module, "_get_api_key", lambda: "ark-secret")
    monkeypatch.setattr(module, "_tools_doubao_search_conf", lambda: {})
    monkeypatch.setattr(module.requests, "post", post)

    tool = module.DoubaoSearch()
    # Avoid touching real quota SQLite.
    tool._is_exhausted = lambda: False
    tool._mark_exhausted = lambda reason: None

    result = tool.execute(
        {
            "query": "北京天气",
            "count": 5,
            "freshness": "oneDay",
            "auth_level": 1,
            "query_rewrite": True,
        }
    )

    assert result.status == "success"
    assert result.result["backend"] == "doubao"
    assert result.result["count"] == 1
    assert result.result["results"][0]["url"] == "https://example.cn/a"
    assert "citationPolicy" in result.result

    assert calls["url"] == module.DOUBAO_SEARCH_URL
    assert calls["headers"]["Authorization"] == "Bearer ark-secret"
    body = __import__("json").loads(calls["data"].decode("utf-8"))
    assert body["Query"] == "北京天气"
    assert body["SearchType"] == "web"
    assert body["Count"] == 5
    assert body["NeedSummary"] is True
    assert body["TimeRange"] == "OneDay"
    assert body["Filter"]["AuthInfoLevel"] == 1
    assert body["QueryControl"]["QueryRewrite"] is True


def test_quota_business_code_falls_back_to_baidu_ai_search(monkeypatch):
    class Response:
        status_code = 200
        text = "{}"

        @staticmethod
        def json():
            return {
                "ResponseMetadata": {
                    "Error": {"Code": "10406", "Message": "free quota exhausted"}
                }
            }

    monkeypatch.setattr(module, "_get_api_key", lambda: "ark-secret")
    monkeypatch.setattr(module, "_tools_doubao_search_conf", lambda: {})
    monkeypatch.setattr(module.requests, "post", lambda *a, **k: Response())

    tool = module.DoubaoSearch()
    marked = []
    tool._is_exhausted = lambda: False
    tool._mark_exhausted = lambda reason: marked.append(reason)
    tool._fallback_to_baidu_ai_search = (
        lambda query, count, freshness, reason: ToolResult.success(
            {
                "backend": "baidu_ai_search",
                "fallback_from": "doubao_search",
                "fallback_reason": reason,
                "answer": "from ai",
            }
        )
    )

    result = tool.execute({"query": "test"})
    assert result.status == "success"
    assert result.result["backend"] == "baidu_ai_search"
    assert result.result["fallback_from"] == "doubao_search"
    assert "10406" in result.result["fallback_reason"]
    assert marked


def test_no_key_falls_back_to_baidu_ai_search(monkeypatch):
    monkeypatch.setattr(module, "_get_api_key", lambda: "")
    monkeypatch.setattr(module, "_tools_doubao_search_conf", lambda: {})

    tool = module.DoubaoSearch()
    called = {}

    def fake_fallback(query, count, freshness, reason):
        called["reason"] = reason
        return ToolResult.success(
            {
                "backend": "web_search",
                "fallback_from": "doubao_search",
                "results": [],
            }
        )

    tool._fallback_to_baidu_ai_search = fake_fallback
    result = tool.execute({"query": "hello", "count": 3})
    assert result.status == "success"
    assert result.result["backend"] == "web_search"
    assert "not configured" in called["reason"]