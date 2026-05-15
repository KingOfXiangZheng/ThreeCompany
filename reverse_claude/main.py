#!/usr/bin/env python3
"""Pure HTTP Claude Web reverse client.

This module intentionally keeps browser automation out of the runtime path.
Capture/refresh tooling can populate ``config/`` files, then the service path
uses plain HTTP plus Claude Web's SSE completion endpoint.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import queue
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import requests

try:
    import curl_cffi.requests as curl_requests
except Exception:  # pragma: no cover - optional transport
    curl_requests = None


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"


CLAUDE_MODE_ALIASES = {
    "claude": "claude-sonnet-4-6",
    "claude-web": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus": "claude-opus-4-5",
    "claude-opus-4": "claude-opus-4-5",
    "claude-opus-4-5": "claude-opus-4-5",
}


@dataclass
class ClaudeStreamEvent:
    delta: str = ""
    assistant_message_uuid: str | None = None
    event_type: str | None = None


class ClaudeHTTPStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str = ""):
        super().__init__(f"Claude completion failed: status={status_code}, body={body[:800]}")
        self.status_code = status_code
        self.body = body


class _ResponseContextAdapter:
    def __init__(self, response: Any):
        self._response = response

    def __enter__(self) -> Any:
        return self._response

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        close = getattr(self._response, "close", None)
        if close:
            close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


class _CurlCffiStatelessSession:
    def __init__(self, proxy_url: str):
        self.headers: dict[str, str] = {}
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
        self._proxy_url = proxy_url

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}) or {})
        if self._proxy_url and "proxy" not in kwargs:
            kwargs["proxy"] = self._proxy_url
        kwargs["headers"] = headers
        last_exc: BaseException | None = None
        explicit_impersonate = kwargs.pop("impersonate", None)
        impersonate_candidates = [explicit_impersonate] if explicit_impersonate else ["chrome", None]
        for impersonate in impersonate_candidates:
            request_kwargs = dict(kwargs)
            if impersonate:
                request_kwargs["impersonate"] = impersonate
            for attempt in range(3):
                try:
                    return _ResponseContextAdapter(curl_requests.request(method, url, **request_kwargs))
                except Exception as exc:
                    if not is_request_error(exc):
                        raise
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(0.25 * (attempt + 1))
        if last_exc:
            raise last_exc
        raise RuntimeError("curl_cffi request failed without exception")

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._request("POST", url, **kwargs)


def safe_print(message: str, end: str = "\n") -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = str(message).encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, end=end, flush=True)


def _read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"Missing Claude config file: {path}")
    with path.open("r", encoding="utf-8-sig") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"Claude config must be a JSON object: {path}")
    return value


def load_config() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    cookies = _read_json(CONFIG_DIR / "cookies.json")
    headers = _read_json(CONFIG_DIR / "headers.json", {})
    config = _read_json(CONFIG_DIR / "config.json")
    return cookies, headers, config


def normalize_claude_model(model: str | None) -> str:
    key = (model or "claude-sonnet-4-6").strip().lower().replace("_", "-").replace(" ", "-")
    return CLAUDE_MODE_ALIASES.get(key, key)


def generate_uuid() -> str:
    return str(uuid.uuid4())


def generate_uuid7_like() -> str:
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = random.getrandbits(12)
    rand_b = random.getrandbits(62)
    value = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return str(uuid.UUID(int=value))


def make_cookie_header(cookies: dict[str, Any]) -> str:
    raw = cookies.get("cookie") or cookies.get("Cookie")
    if raw:
        return str(raw)
    parts = []
    for name, value in cookies.items():
        if name.lower() in {"note", "cookie"} or value in (None, ""):
            continue
        parts.append(f"{name}={value}")
    return "; ".join(parts)


def _normalize_proxy_url(proxy: str) -> str:
    proxy = proxy.strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    if proxy.startswith("127.") or proxy.startswith("localhost"):
        return f"socks5h://{proxy}"
    return f"http://{proxy}"


def _system_proxy_url() -> str:
    for key in ("CLAUDE_PROXY", "HTTPS_PROXY", "ALL_PROXY", "https_proxy", "all_proxy"):
        value = os.environ.get(key)
        if value:
            return _normalize_proxy_url(value)
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            proxy_enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            if proxy_enabled:
                proxy_server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
                if "=" in proxy_server:
                    parts = dict(part.split("=", 1) for part in proxy_server.split(";") if "=" in part)
                    proxy_server = parts.get("https") or parts.get("http") or next(iter(parts.values()), "")
                return _normalize_proxy_url(proxy_server)
    except Exception:
        pass
    return ""


def make_session(cookies: dict[str, Any], base_headers: dict[str, Any], config: dict[str, Any]) -> requests.Session:
    proxy_url = str(config.get("proxy") or _system_proxy_url() or "")
    transport = str(config.get("transport") or "curl_cffi").lower()
    if transport == "curl_cffi" and curl_requests is not None:
        session = _CurlCffiStatelessSession(proxy_url)
    else:
        session = requests.Session()

    user_agent = base_headers.get(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    )
    headers = {
        "user-agent": user_agent,
        "accept": "text/event-stream",
        "accept-language": base_headers.get("accept-language", "zh-CN,zh;q=0.9,en;q=0.8"),
        "content-type": "application/json",
        "origin": "https://claude.ai",
        "referer": base_headers.get("referer", "https://claude.ai/new"),
        "anthropic-client-platform": base_headers.get("anthropic-client-platform", "web_claude_ai"),
        "connection": "close",
    }
    device_id = base_headers.get("anthropic-device-id") or cookies.get("anthropic-device-id") or config.get("device_id")
    if device_id:
        headers["anthropic-device-id"] = str(device_id)
    cookie_header = make_cookie_header(cookies)
    if cookie_header:
        headers["cookie"] = cookie_header
    session.headers.update(headers)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def is_request_error(exc: BaseException) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    return bool(curl_requests is not None and isinstance(exc, curl_requests.exceptions.RequestException))


def organization_id_from_config(cookies: dict[str, Any], config: dict[str, Any]) -> str:
    organization_id = config.get("organization_id") or cookies.get("lastActiveOrg")
    if not organization_id:
        raise RuntimeError("Missing Claude organization_id. Set reverse_claude/config/config.json.")
    return str(organization_id)


def default_personalized_styles() -> list[dict[str, Any]]:
    return [
        {
            "type": "default",
            "key": "Default",
            "name": "Normal",
            "nameKey": "normal_style_name",
            "prompt": "Normal\n",
            "summary": "Default responses from Claude",
            "summaryKey": "normal_style_summary",
            "isDefault": True,
        }
    ]


def build_completion_body(
    prompt: str,
    model: str | None,
    config: dict[str, Any],
    parent_message_uuid: str | None = None,
    create_conversation: bool = False,
) -> dict[str, Any]:
    human_uuid = generate_uuid7_like()
    assistant_uuid = generate_uuid7_like()
    body: dict[str, Any] = {
        "prompt": prompt,
        "timezone": config.get("timezone", "Asia/Shanghai"),
        "personalized_styles": config.get("personalized_styles") or default_personalized_styles(),
        "locale": config.get("locale", "en-US"),
        "model": normalize_claude_model(model or config.get("model")),
        "tools": config.get("tools", []),
        "turn_message_uuids": {
            "human_message_uuid": human_uuid,
            "assistant_message_uuid": assistant_uuid,
        },
        "attachments": [],
        "files": [],
        "sync_sources": [],
        "rendering_mode": "messages",
    }
    if parent_message_uuid:
        body["parent_message_uuid"] = parent_message_uuid
    if create_conversation:
        body["create_conversation_params"] = {
            "name": "",
            "model": body["model"],
            "include_conversation_preferences": True,
            "paprika_mode": None,
            "compass_mode": None,
            "is_temporary": bool(config.get("temporary", False)),
            "enabled_imagine": bool(config.get("enabled_imagine", True)),
        }
    return body


def _event_from_sse_data(event_type: str | None, data_lines: list[str]) -> ClaudeStreamEvent | None:
    if not data_lines:
        return None
    data_raw = "\n".join(data_lines)
    try:
        payload = json.loads(data_raw)
    except json.JSONDecodeError:
        return ClaudeStreamEvent(event_type=event_type)
    if payload.get("type") == "message_start":
        message = payload.get("message") or {}
        return ClaudeStreamEvent(
            assistant_message_uuid=message.get("uuid"),
            event_type=payload.get("type"),
        )
    if payload.get("type") == "content_block_delta":
        delta = payload.get("delta") or {}
        return ClaudeStreamEvent(delta=str(delta.get("text") or ""), event_type=payload.get("type"))
    return ClaudeStreamEvent(event_type=payload.get("type") or event_type)


def parse_sse_lines(lines: Iterator[str | bytes]) -> Iterator[ClaudeStreamEvent]:
    event_type: str | None = None
    data_lines: list[str] = []

    for line in lines:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        line = line.rstrip("\r\n")
        if not line:
            event = _event_from_sse_data(event_type, data_lines)
            if event:
                yield event
            event_type = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())

    event = _event_from_sse_data(event_type, data_lines)
    if event:
        yield event


def parse_sse_events(raw: str) -> Iterator[ClaudeStreamEvent]:
    yield from parse_sse_lines(iter(raw.splitlines()))


def _iter_callback_lines(
    chunk_queue: "queue.Queue[bytes | BaseException | None]",
) -> Iterator[str]:
    buffer = ""
    while True:
        item = chunk_queue.get()
        if item is None:
            break
        if isinstance(item, BaseException):
            raise item
        buffer += item.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line.rstrip("\r")
    if buffer:
        yield buffer.rstrip("\r")


def _stream_with_curl_callback(
    session: _CurlCffiStatelessSession,
    url: str,
    body: dict[str, Any],
    *,
    max_attempts: int = 3,
) -> Iterator[ClaudeStreamEvent]:
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        chunk_queue: queue.Queue[bytes | BaseException | None] = queue.Queue()

        def on_chunk(chunk: bytes) -> None:
            if chunk:
                chunk_queue.put(chunk)

        def run_request() -> None:
            try:
                with session.post(
                    url,
                    data=json.dumps(body, ensure_ascii=False),
                    timeout=120,
                    content_callback=on_chunk,
                ) as response:
                    if response.status_code >= 400:
                        raise ClaudeHTTPStatusError(response.status_code, response.text)
            except BaseException as exc:
                chunk_queue.put(exc)
            finally:
                chunk_queue.put(None)

        worker = threading.Thread(target=run_request, daemon=True)
        worker.start()
        emitted_any_event = False
        try:
            for event in parse_sse_lines(_iter_callback_lines(chunk_queue)):
                emitted_any_event = True
                yield event
            return
        except BaseException as exc:
            last_exc = exc
            if isinstance(exc, ClaudeHTTPStatusError) and exc.status_code < 500:
                raise
            if emitted_any_event or attempt >= max_attempts:
                raise
            safe_print(
                "[Claude Web] stream attempt failed before SSE; retrying "
                f"({attempt}/{max_attempts}): {exc}"
            )
            time.sleep(0.5 * attempt)
        finally:
            worker.join(timeout=1)
    if last_exc:
        raise last_exc


def stream_completion(
    session: requests.Session,
    config: dict[str, Any],
    organization_id: str,
    conversation_id: str,
    prompt: str,
    model: str | None = None,
    parent_message_uuid: str | None = None,
) -> Iterator[ClaudeStreamEvent]:
    base_url = str(config.get("api_base", "https://claude.ai")).rstrip("/")
    url = (
        f"{base_url}/api/organizations/{urllib.parse.quote(organization_id, safe='')}"
        f"/chat_conversations/{urllib.parse.quote(conversation_id, safe='')}/completion"
    )
    body = build_completion_body(
        prompt,
        model,
        config,
        parent_message_uuid=parent_message_uuid,
        create_conversation=not bool(parent_message_uuid),
    )
    if isinstance(session, _CurlCffiStatelessSession):
        retry_count = int(config.get("stream_retries", 3) or 3)
        yield from _stream_with_curl_callback(session, url, body, max_attempts=max(1, retry_count))
        return

    with session.post(url, data=json.dumps(body, ensure_ascii=False), timeout=120, stream=True) as response:
        if response.status_code >= 400:
            raise ClaudeHTTPStatusError(response.status_code, response.text)
        for event in parse_sse_lines(response.iter_lines(decode_unicode=True)):
            yield event


def print_result(events: Iterator[ClaudeStreamEvent]) -> None:
    for event in events:
        if event.delta:
            safe_print(event.delta, end="")
    safe_print("")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude Web pure HTTP client")
    parser.add_argument("message")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--conversation-id")
    parser.add_argument("--parent-message-id")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cookies, headers, config = load_config()
    session = make_session(cookies, headers, config)
    organization_id = organization_id_from_config(cookies, config)
    conversation_id = args.conversation_id or generate_uuid()
    events = stream_completion(
        session,
        config,
        organization_id,
        conversation_id,
        args.message,
        args.model,
        args.parent_message_id,
    )
    print_result(events)


if __name__ == "__main__":
    main()
