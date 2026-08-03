"""Replay requests captured in tmp/openai_failed_requests.jsonl.

The failure log intentionally does not contain authorization headers.  This
script loads the API key from config.json at replay time and never prints it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = REPO_ROOT / "tmp" / "openai_failed_requests.jsonl"
DEFAULT_CONFIG = REPO_ROOT / "config.json"


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return value


def load_records(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Failure log not found: {path}") from exc

    records = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            print(
                f"warning: skipping invalid JSONL line {line_number}: {exc}",
                file=sys.stderr,
            )
            continue
        if not isinstance(record, dict) or not isinstance(
            record.get("request_body"), dict
        ):
            print(
                f"warning: skipping incomplete JSONL line {line_number}",
                file=sys.stderr,
            )
            continue
        record = dict(record)
        record["_line_number"] = line_number
        records.append(record)
    if not records:
        raise RuntimeError(f"No replayable records found in {path}")
    return records


def select_record(records: list[dict], selector: str) -> dict:
    if selector.casefold() == "latest":
        return records[-1]
    try:
        index = int(selector)
    except ValueError as exc:
        raise RuntimeError("--record must be 'latest' or a non-zero integer") from exc
    if index == 0:
        raise RuntimeError("--record 0 is invalid; use 1 for the first record")
    resolved = index - 1 if index > 0 else index
    try:
        return records[resolved]
    except IndexError as exc:
        raise RuntimeError(
            f"Record {index} is out of range; log contains {len(records)} records"
        ) from exc


def list_records(records: list[dict]) -> None:
    print("index  line  recorded_at                       status  model          stream  messages  tools")
    for index, record in enumerate(records, 1):
        payload = record["request_body"]
        response = record.get("response") or {}
        print(
            f"{index:>5}  {record['_line_number']:>4}  "
            f"{str(record.get('recorded_at') or '-'):32.32}  "
            f"{str(response.get('status_code', '-')):>6}  "
            f"{str(payload.get('model') or '-'):13.13}  "
            f"{str(bool(payload.get('stream'))):>6}  "
            f"{len(payload.get('messages') or []):>8}  "
            f"{len(payload.get('tools') or []):>5}"
        )


def _configured_endpoint(config: dict) -> str:
    base = str(
        config.get("custom_api_base")
        or config.get("open_ai_api_base")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    return base + "/chat/completions"


def _configured_key(config: dict) -> str:
    return str(config.get("custom_api_key") or config.get("open_ai_api_key") or "")


def _error_summary(response: requests.Response) -> str:
    try:
        body: Any = response.json()
    except ValueError:
        return response.text[:1000]
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            return json.dumps(
                {
                    "message": error.get("message", ""),
                    "code": error.get("code", ""),
                    "type": error.get("type", ""),
                },
                ensure_ascii=False,
            )
    return json.dumps(body, ensure_ascii=False)[:1000]


def _consume_stream(response: requests.Response, raw: bool) -> dict:
    chunks = 0
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = ""

    for line in response.iter_lines(decode_unicode=False):
        if not line or not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            break
        try:
            text = data.decode("utf-8")
            chunk = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if raw:
            print(text)
        chunks += 1
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                content_parts.append(str(content))
            reasoning = delta.get("reasoning_content")
            if reasoning:
                reasoning_parts.append(str(reasoning))
            for call in delta.get("tool_calls") or []:
                index = int(call.get("index", 0) or 0)
                buffered = tool_calls.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if call.get("id"):
                    buffered["id"] = call["id"]
                function = call.get("function") or {}
                if function.get("name"):
                    buffered["name"] += str(function["name"])
                if function.get("arguments"):
                    buffered["arguments"] += str(function["arguments"])
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])

    return {
        "chunks": chunks,
        "content": "".join(content_parts),
        "reasoning_content": "".join(reasoning_parts),
        "tool_calls": [tool_calls[index] for index in sorted(tool_calls)],
        "finish_reason": finish_reason,
    }


def replay_once(
    record: dict,
    config: dict,
    *,
    timeout: float,
    allow_endpoint_mismatch: bool,
    raw: bool,
) -> dict:
    url = str(record.get("url") or "")
    expected_url = _configured_endpoint(config)
    if not url:
        raise RuntimeError("Selected record has no URL")
    if url != expected_url and not allow_endpoint_mismatch:
        raise RuntimeError(
            "Logged endpoint does not match current configuration: "
            f"logged={url!r}, configured={expected_url!r}. "
            "Use --allow-endpoint-mismatch only if this is intentional."
        )

    api_key = _configured_key(config)
    if not api_key:
        raise RuntimeError("No custom_api_key or open_ai_api_key found in config")

    payload = record["request_body"]
    query = record.get("query") or {}
    response = requests.post(
        url,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        params=query,
        json=payload,
        stream=bool(payload.get("stream")),
        timeout=timeout,
    )
    request_id = (
        response.headers.get("x-request-id")
        or response.headers.get("request-id")
        or response.headers.get("cf-ray")
        or ""
    )
    result = {
        "status_code": response.status_code,
        "request_id": request_id,
        "url": url,
    }
    if response.status_code >= 400:
        result["error"] = _error_summary(response)
        response.close()
        return result

    if payload.get("stream"):
        result.update(_consume_stream(response, raw=raw))
    else:
        try:
            result["response"] = response.json()
        except ValueError:
            result["response_text"] = response.text
    response.close()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay an OpenAI-compatible request from the local failure log."
    )
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--record",
        default="latest",
        help="Record index (1-based), negative index, or 'latest' (default).",
    )
    parser.add_argument("--list", action="store_true", help="List records and exit.")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Print every decoded SSE data chunk in addition to the summary.",
    )
    parser.add_argument(
        "--allow-endpoint-mismatch",
        action="store_true",
        help="Allow replay when the logged URL differs from the configured endpoint.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        records = load_records(args.log.resolve())
        if args.list:
            list_records(records)
            return 0
        if args.repeat < 1:
            raise RuntimeError("--repeat must be at least 1")
        record = select_record(records, args.record)
        config = _load_json(args.config.resolve())
        payload = record["request_body"]
        print(
            f"replaying record={args.record} line={record['_line_number']} "
            f"recorded_at={record.get('recorded_at', '-')} "
            f"model={payload.get('model', '-')} "
            f"messages={len(payload.get('messages') or [])} "
            f"tools={len(payload.get('tools') or [])} "
            f"stream={bool(payload.get('stream'))}"
        )
        exit_code = 0
        for attempt in range(1, args.repeat + 1):
            result = replay_once(
                record,
                config,
                timeout=args.timeout,
                allow_endpoint_mismatch=args.allow_endpoint_mismatch,
                raw=args.raw,
            )
            print(f"\n--- attempt {attempt}/{args.repeat} ---")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if int(result.get("status_code", 0)) >= 400:
                exit_code = 1
        return exit_code
    except (RuntimeError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
