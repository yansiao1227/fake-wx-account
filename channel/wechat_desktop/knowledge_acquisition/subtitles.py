"""Fetch official or AI captions for share-card video URLs.

The installed knowledge-acquisition skill only pulls Bilibili captions when
the video is 60 seconds or shorter. This module retries the public player
API without that cap, then falls back to the watch-page HTML.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

_BILIBILI_BV_RE = re.compile(r"(?:^|/)BV([A-Za-z0-9]+)", re.I)
_BILIBILI_AV_RE = re.compile(r"(?:^|/)av(\d+)", re.I)
_ALLOWED_HOSTS = ("bilibili.com", "b23.tv", "hdslb.com")
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com",
    "Origin": "https://www.bilibili.com",
}


def is_bilibili_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "b23.tv" or host.endswith(".b23.tv") or _host_allowed(url)


def has_useful_subtitles(payload: dict[str, Any]) -> bool:
    cues = payload.get("subtitles")
    if not isinstance(cues, list):
        return False
    return any(str(item.get("text") or item.get("content") or "").strip() for item in cues if isinstance(item, dict))


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_subtitle_text(cues: list[dict[str, Any]]) -> str:
    lines = []
    for cue in cues:
        text = str(cue.get("text") or "").strip()
        if not text:
            continue
        stamp = str(cue.get("timestamp") or "").strip()
        lines.append(f"[{stamp}] {text}" if stamp else text)
    return "\n".join(lines)


def parse_subtitle_document(raw: Any) -> list[dict[str, Any]]:
    data = raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        cleaned = re.sub(r"[\x00-\x1f\u200b-\u200d]", "", raw).strip()
        if not cleaned:
            return []
        data = json.loads(cleaned)
    if not isinstance(data, dict):
        return []
    body = data.get("body")
    if not isinstance(body, list):
        return []
    cues = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content") or item.get("text") or "").strip()
        if not text:
            continue
        start = float(item.get("from") or item.get("start") or 0)
        end = float(item.get("to") or item.get("end") or start)
        cues.append(
            {
                "timestamp": format_timestamp(start),
                "from": start,
                "to": end,
                "text": text,
            }
        )
    return cues


def pick_subtitle_track(tracks: list[dict[str, Any]]) -> dict[str, Any] | None:
    scored = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        url = str(track.get("subtitle_url") or track.get("url") or "").strip()
        if not url:
            continue
        lan = str(track.get("lan") or track.get("lang") or "").casefold()
        label = str(track.get("lan_doc") or track.get("label") or "")
        score = 0
        if "zh" in lan or "中文" in label:
            score += 40
        if lan in {"zh-cn", "zh-hans", "ai-zh", "zh"}:
            score += 10
        if "en" in lan or "英语" in label:
            score += 5
        if "ai" in lan or "自动" in label:
            score += 8
        else:
            score += 16
        scored.append((score, track))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def extract_bilibili_id(url: str) -> tuple[str, str]:
    bv_match = _BILIBILI_BV_RE.search(url)
    if bv_match:
        return "bvid", f"BV{bv_match.group(1)}"
    av_match = _BILIBILI_AV_RE.search(url)
    if av_match:
        return "aid", av_match.group(1)
    return "", ""


def page_index_from_url(url: str) -> int:
    values = parse_qs(urlparse(url).query).get("p") or []
    try:
        page = int(values[0])
    except (IndexError, TypeError, ValueError):
        return 1
    return page if page > 0 else 1


def enrich_payload_with_subtitles(
    url: str,
    payload: dict[str, Any] | None,
    *,
    enabled: bool = True,
    timeout: float = 12.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    merged = dict(payload or {})
    if not enabled or not is_bilibili_url(url):
        return merged
    if has_useful_subtitles(merged):
        if not str(merged.get("subtitle_text") or "").strip():
            merged["subtitle_text"] = format_subtitle_text(merged.get("subtitles") or [])
        return merged

    media = fetch_bilibili_media(url, timeout=timeout, session=session)
    if not media:
        return merged
    for key, value in media.items():
        if key in {"subtitles", "subtitle_text", "subtitle_source", "subtitle_language"}:
            merged[key] = value
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


def fetch_bilibili_media(
    url: str,
    *,
    timeout: float = 12.0,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    http = session or requests.Session()
    if session is None:
        http.headers.update(_BROWSER_HEADERS)
    resolved = _resolve_bilibili_url(url, http, timeout)
    kind, video_id = extract_bilibili_id(resolved)
    if not video_id:
        return {}
    view = _get_json(
        http,
        "https://api.bilibili.com/x/web-interface/view",
        {kind: video_id},
        timeout,
    )
    if not isinstance(view, dict) or view.get("code") not in (0, None):
        return {}
    data = view.get("data") if isinstance(view.get("data"), dict) else {}
    page = _select_page(data, page_index_from_url(resolved))
    cid = page.get("cid") or data.get("cid")
    bvid = str(data.get("bvid") or (video_id if kind == "bvid" else "")).strip()
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    stat = data.get("stat") if isinstance(data.get("stat"), dict) else {}
    cues, source, language = _fetch_cues(http, bvid, cid, timeout)
    result = {
        "type": "video",
        "platform": "bilibili",
        "title": data.get("title") or "",
        "author": owner.get("name") or "",
        "description": data.get("desc") or "",
        "duration": data.get("duration"),
        "cover": data.get("pic") or "",
        "viewCount": stat.get("view"),
        "likeCount": stat.get("like"),
        "cId": cid,
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else resolved,
    }
    if cues:
        result["subtitles"] = cues
        result["subtitle_text"] = format_subtitle_text(cues)
        result["subtitle_source"] = source
        result["subtitle_language"] = language
    return {key: value for key, value in result.items() if value not in (None, "")}


def _fetch_cues(
    http: requests.Session,
    bvid: str,
    cid: Any,
    timeout: float,
) -> tuple[list[dict[str, Any]], str, str]:
    tracks = []
    if cid:
        player = _get_json(
            http,
            "https://api.bilibili.com/x/player/v2",
            {"bvid": bvid, "cid": cid},
            timeout,
        )
        tracks = _tracks_from_player(player)
    source = "bilibili_player"
    if not tracks and bvid:
        html = _get_text(http, f"https://www.bilibili.com/video/{bvid}", timeout)
        tracks = _tracks_from_html(html)
        source = "bilibili_html"
    track = pick_subtitle_track(tracks)
    if not track:
        return [], "", ""
    subtitle_url = _absolute_url(str(track.get("subtitle_url") or track.get("url") or ""))
    if not subtitle_url or not _host_allowed(subtitle_url):
        return [], "", ""
    document = _get_json(http, subtitle_url, None, timeout)
    if document is None:
        raw = _get_text(http, subtitle_url, timeout)
        cues = parse_subtitle_document(raw)
    else:
        cues = parse_subtitle_document(document)
    language = str(track.get("lan") or track.get("lan_doc") or "")
    return cues, source, language


def _tracks_from_player(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    subtitle = data.get("subtitle") if isinstance(data, dict) else None
    if not isinstance(subtitle, dict):
        return []
    tracks = subtitle.get("subtitles") or subtitle.get("list") or []
    return tracks if isinstance(tracks, list) else []


def _tracks_from_html(html: str) -> list[dict[str, Any]]:
    if not html:
        return []
    match = re.search(
        r'"subtitle"\s*:\s*(\{(?:[^{}]|\{[^{}]*\})*\})',
        html,
    )
    if not match:
        urls = re.findall(r'"subtitle_url"\s*:\s*"([^"]+)"', html)
        return [{"subtitle_url": _unescape_js_string(item)} for item in urls]
    try:
        payload = json.loads(_unescape_js_string(match.group(1)))
    except json.JSONDecodeError:
        urls = re.findall(r'"subtitle_url"\s*:\s*"([^"]+)"', html)
        return [{"subtitle_url": _unescape_js_string(item)} for item in urls]
    tracks = payload.get("subtitles") or payload.get("list") or []
    return tracks if isinstance(tracks, list) else []


def _select_page(data: dict[str, Any], page_index: int) -> dict[str, Any]:
    pages = data.get("pages")
    if isinstance(pages, list) and pages:
        index = min(max(page_index, 1), len(pages)) - 1
        page = pages[index]
        return page if isinstance(page, dict) else {}
    return {}


def _resolve_bilibili_url(url: str, http: requests.Session, timeout: float) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "b23.tv" and not host.endswith(".b23.tv"):
        return url
    try:
        response = http.get(url, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        final = str(response.url or url)
        return final if _host_allowed(final) else url
    except requests.RequestException:
        return url


def _get_json(
    http: requests.Session,
    url: str,
    params: dict[str, Any] | None,
    timeout: float,
) -> Any:
    if not _host_allowed(url):
        return None
    try:
        response = http.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        if "json" in (response.headers.get("Content-Type") or "").casefold():
            return response.json()
        text = response.text.lstrip()
        if text.startswith("{") or text.startswith("["):
            return response.json()
    except (requests.RequestException, ValueError, json.JSONDecodeError):
        return None
    return None


def _get_text(http: requests.Session, url: str, timeout: float) -> str:
    if not _host_allowed(url):
        return ""
    try:
        response = http.get(url, timeout=timeout)
        response.raise_for_status()
        return response.text or ""
    except requests.RequestException:
        return ""


def _absolute_url(url: str) -> str:
    value = url.replace("\\u002F", "/").replace("\\/", "/").strip()
    if value.startswith("//"):
        return "https:" + value
    return value


def _unescape_js_string(value: str) -> str:
    return (
        value.replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("\\u0026", "&")
    )


def _host_allowed(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in _ALLOWED_HOSTS)
