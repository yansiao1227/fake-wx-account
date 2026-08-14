import json

import pytest
import requests

from channel.wechat_desktop.knowledge_acquisition import KnowledgeAcquisitionExtractor
from channel.wechat_desktop.knowledge_acquisition.subtitles import (
    enrich_payload_with_subtitles,
    extract_bilibili_id,
    format_subtitle_text,
    format_timestamp,
    has_useful_subtitles,
    parse_subtitle_document,
    pick_subtitle_track,
)


class FakeResponse:
    def __init__(self, payload=None, text="", url="", content_type="application/json", status=200):
        self._payload = payload
        self.text = text if text or payload is None else json.dumps(payload)
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        full = url
        if params:
            query = "&".join(f"{key}={value}" for key, value in params.items())
            full = f"{url}?{query}"
        for prefix, response in self.routes:
            if full.startswith(prefix) or prefix in full:
                return response
        raise AssertionError(f"unexpected request: {full}")


def test_parse_and_format_bilibili_bcc_subtitles():
    cues = parse_subtitle_document(
        {
            "body": [
                {"from": 0.2, "to": 1.8, "content": "开头"},
                {"from": 62, "to": 64, "content": "中间"},
                {"from": 3725, "to": 3728, "content": "超一小时"},
                {"from": 80, "to": 81, "content": "  "},
            ]
        }
    )

    assert [item["timestamp"] for item in cues] == ["0:00", "1:02", "1:02:05"]
    assert format_subtitle_text(cues) == (
        "[0:00] 开头\n[1:02] 中间\n[1:02:05] 超一小时"
    )
    assert format_timestamp(5) == "0:05"


def test_pick_subtitle_track_prefers_human_chinese_then_ai():
    chosen = pick_subtitle_track(
        [
            {"lan": "en", "lan_doc": "English", "subtitle_url": "https://i0.hdslb.com/en.json"},
            {
                "lan": "ai-zh",
                "lan_doc": "中文（自动生成）",
                "subtitle_url": "https://aisubtitle.hdslb.com/ai.json",
            },
            {
                "lan": "zh-CN",
                "lan_doc": "中文（中国）",
                "subtitle_url": "https://i0.hdslb.com/zh.json",
            },
        ]
    )

    assert chosen["subtitle_url"] == "https://i0.hdslb.com/zh.json"


def test_extract_bilibili_id_from_common_share_urls():
    assert extract_bilibili_id(
        "https://www.bilibili.com/video/BV1xx411c7mD?spm_id_from=333"
    ) == ("bvid", "BV1xx411c7mD")
    assert extract_bilibili_id("https://www.bilibili.com/video/av170001") == (
        "aid",
        "170001",
    )


def test_enrich_keeps_existing_skill_subtitles():
    payload = {
        "title": "已有标题",
        "subtitles": [{"timestamp": "0:01", "text": "skill 已给出"}],
    }

    result = enrich_payload_with_subtitles(
        "https://www.bilibili.com/video/BV1xx411c7mD",
        payload,
        session=FakeSession([]),
    )

    assert result["subtitles"][0]["text"] == "skill 已给出"
    assert "[0:01] skill 已给出" in result["subtitle_text"]


def test_enrich_fetches_long_video_ai_subtitles_when_skill_skips_them():
    session = FakeSession(
        [
            (
                "https://api.bilibili.com/x/web-interface/view",
                FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "bvid": "BV1xx411c7mD",
                            "title": "长视频",
                            "desc": "简介",
                            "duration": 1800,
                            "cid": 99,
                            "owner": {"name": "UP主"},
                            "stat": {"view": 10, "like": 1},
                            "pic": "https://i0.hdslb.com/cover.jpg",
                        },
                    }
                ),
            ),
            (
                "https://api.bilibili.com/x/player/v2",
                FakeResponse(
                    {
                        "code": 0,
                        "data": {
                            "subtitle": {
                                "subtitles": [
                                    {
                                        "lan": "ai-zh",
                                        "lan_doc": "中文（自动生成）",
                                        "subtitle_url": "//aisubtitle.hdslb.com/bfs/ai.json",
                                    }
                                ]
                            }
                        },
                    }
                ),
            ),
            (
                "https://aisubtitle.hdslb.com/bfs/ai.json",
                FakeResponse(
                    {"body": [{"from": 1.5, "to": 3.0, "content": "这是口播正文"}]},
                    content_type="text/plain",
                ),
            ),
        ]
    )

    result = enrich_payload_with_subtitles(
        "https://www.bilibili.com/video/BV1xx411c7mD",
        {"title": "长视频", "subtitles": []},
        session=session,
    )

    assert has_useful_subtitles(result)
    assert result["subtitle_source"] == "bilibili_player"
    assert result["subtitle_text"] == "[0:01] 这是口播正文"
    assert result["author"] == "UP主"


def test_extractor_still_returns_subtitles_when_skill_fails(monkeypatch):
    extractor = KnowledgeAcquisitionExtractor(
        {"knowledge_acquisition_fetch_subtitles": True}
    )

    def fail_skill(_url):
        raise RuntimeError("skill exploded")

    monkeypatch.setattr(extractor, "_invoke_skill", fail_skill)
    monkeypatch.setattr(
        "channel.wechat_desktop.knowledge_acquisition.extractor.enrich_payload_with_subtitles",
        lambda url, payload, **_kwargs: {
            "title": "补拉成功",
            "subtitles": [{"timestamp": "0:00", "text": "字幕"}],
            "subtitle_text": "[0:00] 字幕",
        },
    )

    result = json.loads(
        extractor.extract("https://www.bilibili.com/video/BV1xx411c7mD")
    )
    assert result["subtitle_text"] == "[0:00] 字幕"


def test_extractor_raises_when_skill_and_subtitles_both_empty(monkeypatch):
    extractor = KnowledgeAcquisitionExtractor(
        {"knowledge_acquisition_fetch_subtitles": True}
    )
    monkeypatch.setattr(
        extractor,
        "_invoke_skill",
        lambda _url: (_ for _ in ()).throw(RuntimeError("skill exploded")),
    )
    monkeypatch.setattr(
        "channel.wechat_desktop.knowledge_acquisition.extractor.enrich_payload_with_subtitles",
        lambda url, payload, **_kwargs: payload,
    )

    with pytest.raises(RuntimeError, match="skill exploded"):
        extractor.extract("https://www.bilibili.com/video/BV1xx411c7mD")
