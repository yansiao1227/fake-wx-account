from agent.tools.baidu_ai_search import baidu_ai_search as module
from agent.tools.search_quota import CAP_BAIDU_AI_SEARCH


def test_is_available_with_qianfan_key(monkeypatch):
    monkeypatch.setattr(module, "_get_qianfan_api_key", lambda: "qf-secret")
    assert module.BaiduAiSearch.is_available() is True


def test_is_available_falls_back_to_web_search(monkeypatch):
    monkeypatch.setattr(module, "_get_qianfan_api_key", lambda: "")

    class FakeWebSearch:
        @staticmethod
        def is_available():
            return True

    monkeypatch.setitem(
        __import__("sys").modules,
        "agent.tools.web_search.web_search",
        type("M", (), {"WebSearch": FakeWebSearch})(),
    )
    # Patch import path used inside is_available
    import agent.tools.web_search.web_search as ws

    monkeypatch.setattr(ws.WebSearch, "is_available", staticmethod(lambda: True))
    assert module.BaiduAiSearch.is_available() is True


def test_baidu_ai_search_request_matches_official_api(monkeypatch, tmp_path):
    calls = {}

    class Response:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "油价近期上调，详见参考资料^[1]^。",
                        },
                    }
                ],
                "is_safe": True,
                "references": [
                    {
                        "id": 1,
                        "title": "油价调整",
                        "url": "https://example.cn/oil",
                        "content": "国内成品油价格上调",
                        "web_anchor": "example.cn",
                        "date": "2026-08-01",
                        "type": "web",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
                "request_Id": "req-ai-1",
            }

    def post(url, **kwargs):
        calls.update({"url": url, **kwargs})
        return Response()

    tool = module.BaiduAiSearch({"usage_store_path": str(tmp_path / "usage.db")})
    monkeypatch.setattr(module, "_get_qianfan_api_key", lambda: "qf-secret")
    monkeypatch.setattr(module.requests, "post", post)
    monkeypatch.setattr(module, "_tools_baidu_ai_search_conf", lambda: {"model": "ernie-4.5-turbo-32k"})

    result = tool.execute({"query": "近日油价调整消息", "count": 5, "freshness": "oneWeek"})

    assert result.status == "success"
    assert result.result["backend"] == "baidu_ai_search"
    assert "油价" in result.result["answer"]
    assert result.result["references"][0]["url"] == "https://example.cn/oil"
    assert result.result["results"][0]["title"] == "油价调整"
    assert result.result["request_id"] == "req-ai-1"
    # Same agent-summary flow as doubao_search: no direct-final short-circuit.
    assert "direct_final_answer" not in result.result
    assert "final_answer_text" not in result.result
    assert calls["url"] == "https://qianfan.baidubce.com/v2/ai_search/chat/completions"
    assert calls["headers"]["Authorization"] == "Bearer qf-secret"
    body = calls["json"]
    assert body["model"] == "ernie-4.5-turbo-32k"
    assert body["stream"] is False
    assert body["search_source"] == "baidu_search_v2"
    assert body["search_mode"] == "required"
    assert body["messages"][0]["content"] == "近日油价调整消息"
    assert body["resource_type_filter"] == [{"type": "web", "top_k": 5}]
    assert body["search_recency_filter"] == "week"
    assert body["enable_corner_markers"] is True


def test_quota_exhausted_falls_back_to_web_search(monkeypatch, tmp_path):
    tool = module.BaiduAiSearch({"usage_store_path": str(tmp_path / "usage.db")})
    monkeypatch.setattr(module, "_get_qianfan_api_key", lambda: "qf-secret")
    monkeypatch.setattr(tool, "_reserve_quota", lambda: (False, 100, 100))

    class FakeWebSearch:
        def __init__(self, config=None):
            self.config = config or {}

        @staticmethod
        def is_available():
            return True

        def execute(self, args):
            return module.ToolResult.success(
                {
                    "query": args["query"],
                    "backend": "multi",
                    "backends": ["tavily", "ddgs"],
                    "results": [
                        {
                            "title": "fallback hit",
                            "url": "https://example.com/f",
                            "snippet": "from web_search",
                        }
                    ],
                    "total": 1,
                    "count": 1,
                }
            )

    import agent.tools.web_search.web_search as ws

    monkeypatch.setattr(ws, "WebSearch", FakeWebSearch)

    result = tool.execute({"query": "今日热点"})

    assert result.status == "success"
    assert result.result["fallback_from"] == "baidu_ai_search"
    assert "quota" in result.result["fallback_reason"].lower()
    assert result.result["results"][0]["title"] == "fallback hit"
    assert "direct_final_answer" not in result.result
    assert tool._get_capability_state().is_exhausted(CAP_BAIDU_AI_SEARCH) is True


def test_exhaustion_flag_skips_remote_baidu_ai_search(monkeypatch, tmp_path):
    tool = module.BaiduAiSearch({"usage_store_path": str(tmp_path / "usage.db")})
    monkeypatch.setattr(module, "_get_qianfan_api_key", lambda: "qf-secret")
    tool._get_capability_state().mark_exhausted(CAP_BAIDU_AI_SEARCH, "already out")
    posts = {"n": 0}

    def post(*a, **k):
        posts["n"] += 1
        raise AssertionError("should not call baidu_ai_search when exhausted")

    monkeypatch.setattr(module.requests, "post", post)

    class FakeWebSearch:
        def __init__(self, config=None):
            self.config = config or {}

        @staticmethod
        def is_available():
            return True

        def execute(self, args):
            return module.ToolResult.success(
                {
                    "query": args["query"],
                    "backend": "ddgs",
                    "results": [
                        {
                            "title": "ddgs",
                            "url": "https://example.com/d",
                            "snippet": "ok",
                        }
                    ],
                    "total": 1,
                    "count": 1,
                }
            )

    import agent.tools.web_search.web_search as ws

    monkeypatch.setattr(ws, "WebSearch", FakeWebSearch)

    result = tool.execute({"query": "天气"})
    assert result.status == "success"
    assert result.result["fallback_from"] == "baidu_ai_search"
    assert posts["n"] == 0


def test_remote_quota_error_falls_back_to_web_search(monkeypatch, tmp_path):
    tool = module.BaiduAiSearch({"usage_store_path": str(tmp_path / "usage.db")})
    monkeypatch.setattr(module, "_get_qianfan_api_key", lambda: "qf-secret")
    monkeypatch.setattr(tool, "_reserve_quota", lambda: (True, 1, 100))

    class Response:
        status_code = 429
        text = "rate limit exceeded free quota"

        @staticmethod
        def json():
            return {"code": "429", "message": "rate limit"}

    monkeypatch.setattr(module.requests, "post", lambda *a, **k: Response())

    class FakeWebSearch:
        def __init__(self, config=None):
            self.config = config or {}

        @staticmethod
        def is_available():
            return True

        def execute(self, args):
            return module.ToolResult.success(
                {
                    "query": args["query"],
                    "backend": "ddgs",
                    "results": [
                        {
                            "title": "ddgs",
                            "url": "https://example.com/d",
                            "snippet": "ok",
                        }
                    ],
                    "total": 1,
                    "count": 1,
                }
            )

    import agent.tools.web_search.web_search as ws

    monkeypatch.setattr(ws, "WebSearch", FakeWebSearch)

    result = tool.execute({"query": "天气"})

    assert result.status == "success"
    assert result.result["fallback_from"] == "baidu_ai_search"
    assert result.result["results"][0]["title"] == "ddgs"
    assert tool._get_capability_state().is_exhausted(CAP_BAIDU_AI_SEARCH) is True


def test_missing_key_falls_back_when_web_search_available(monkeypatch):
    tool = module.BaiduAiSearch()
    monkeypatch.setattr(module, "_get_qianfan_api_key", lambda: "")

    class FakeWebSearch:
        def __init__(self, config=None):
            self.config = config or {}

        @staticmethod
        def is_available():
            return True

        def execute(self, args):
            return module.ToolResult.success(
                {
                    "query": args["query"],
                    "backend": "tavily",
                    "results": [
                        {
                            "title": "tavily",
                            "url": "https://example.com/t",
                            "snippet": "ok",
                        }
                    ],
                    "total": 1,
                    "count": 1,
                }
            )

    import agent.tools.web_search.web_search as ws

    monkeypatch.setattr(ws, "WebSearch", FakeWebSearch)

    result = tool.execute({"query": "无密钥搜索"})
    assert result.status == "success"
    assert result.result["fallback_from"] == "baidu_ai_search"
    assert "QIANFAN_API_KEY" in result.result["fallback_reason"]


def test_daily_limit_default_is_free_tier():
    tool = module.BaiduAiSearch()
    assert tool._daily_limit() == 100


def test_provider_schema_hides_model_routing():
    schema = module.BaiduAiSearch.get_json_schema()
    assert schema["name"] == "baidu_ai_search"
    assert "query" in schema["parameters"]["properties"]
    assert "model" not in schema["parameters"]["properties"]


def test_extract_direct_final_answer_generic_opt_in():
    """agent_stream short-circuit remains available, but baidu_ai_search does not set it."""
    from agent.protocol.agent_stream import AgentStreamExecutor

    ok = {
        "status": "success",
        "result": {
            "direct_final_answer": True,
            "final_answer_text": "直出答案",
            "backend": "other_tool",
        },
    }
    assert (
        AgentStreamExecutor._extract_direct_final_answer(
            [{"name": "other_tool"}], [ok]
        )
        == "直出答案"
    )
    assert (
        AgentStreamExecutor._extract_direct_final_answer(
            [{"name": "other_tool"}, {"name": "web_fetch"}], [ok, ok]
        )
        is None
    )
    # Normal baidu_ai_search success payload (no direct_final_answer) never short-circuits.
    baidu_ok = {
        "status": "success",
        "result": {
            "backend": "baidu_ai_search",
            "answer": "草稿回答",
            "results": [],
        },
    }
    assert (
        AgentStreamExecutor._extract_direct_final_answer(
            [{"name": "baidu_ai_search"}], [baidu_ok]
        )
        is None
    )
    fallback = {
        "status": "success",
        "result": {
            "direct_final_answer": True,
            "final_answer_text": "不应直出",
            "fallback_from": "baidu_ai_search",
        },
    }
    assert (
        AgentStreamExecutor._extract_direct_final_answer(
            [{"name": "baidu_ai_search"}], [fallback]
        )
        is None
    )
