#!/usr/bin/env python3
"""Pure HTTP Grok Web reverse client.

Browser tooling can populate ``config/`` files, but this runtime path does not
use Playwright or browser automation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import sys
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

DEFAULT_CONFIG: dict[str, Any] = {
    "api_base": "https://grok.com",
    "default_mode_id": "fast",
    "temporary": False,
    "disable_search": False,
    "enable_image_generation": True,
    "enable_image_streaming": True,
    "image_generation_count": 2,
    "timeout_seconds": 120,
    "transport": "curl_cffi",
    "statsig_meta": "ttepiAWVrA0BocY/fWZcfOjX29b/797ETCHsYcuQIF89KEQoKGFQMmfzAeiah5qR",
    "statsig_fingerprint": "f3de76100f5c28f5c28f5c00f5c28f5c28f5c100",
}

GROK_MODE_ALIASES = {
    "grok": "fast",
    "grok-web": "fast",
    "grok-fast": "fast",
    "grok-3": "fast",
    "grok-3-fast": "fast",
    "grok-4-mini": "fast",
    "grok-auto": "auto",
    "grok-expert": "expert",
    "grok-thinking": "expert",
    "grok-heavy": "heavy",
    "fast": "fast",
    "auto": "auto",
    "expert": "expert",
    "heavy": "heavy",
}


@dataclass
class GrokStreamEvent:
    delta: str = ""
    conversation_id: str | None = None
    response_id: str | None = None
    event_type: str | None = None


class GrokHTTPStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str = ""):
        message = f"Grok completion failed: status={status_code}, body={body[:800]}"
        super().__init__(message)
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
        explicit_impersonate = kwargs.pop("impersonate", None)
        impersonate_candidates = [explicit_impersonate] if explicit_impersonate else ["chrome", "chrome120", None]
        last_exc: BaseException | None = None
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
        raise FileNotFoundError(f"Missing Grok config file: {path}")
    with path.open("r", encoding="utf-8-sig") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"Grok config must be a JSON object: {path}")
    return value


def load_config() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    cookies = _read_json(CONFIG_DIR / "cookies.json")
    headers = _read_json(CONFIG_DIR / "headers.json", {})
    config = _read_json(CONFIG_DIR / "config.json", {})
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)
    return cookies, headers, config


def normalize_grok_model(model: str | None) -> str:
    key = (model or "grok-fast").strip().lower().replace("_", "-").replace(" ", "-")
    return GROK_MODE_ALIASES.get(key, key)


def mode_id_from_model(model: str | None, config: dict[str, Any]) -> str:
    normalized = normalize_grok_model(model)
    if normalized in {"fast", "auto", "expert", "heavy"}:
        return normalized
    configured = config.get("model_mode_map", {})
    if isinstance(configured, dict) and normalized in configured:
        return str(configured[normalized])
    return str(config.get("default_mode_id") or "fast")


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
    for key in ("GROK_PROXY", "HTTPS_PROXY", "ALL_PROXY", "https_proxy", "all_proxy"):
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
        "accept": "*/*",
        "accept-language": base_headers.get("accept-language", "zh-CN,zh;q=0.9"),
        "content-type": "application/json",
        "origin": "https://grok.com",
        "referer": base_headers.get("referer", "https://grok.com/"),
        "sec-ch-ua": base_headers.get("sec-ch-ua", '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"'),
        "sec-ch-ua-mobile": base_headers.get("sec-ch-ua-mobile", "?0"),
        "sec-ch-ua-platform": base_headers.get("sec-ch-ua-platform", '"Windows"'),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    for key in ("x-statsig-id", "x-challenge", "x-signature"):
        value = base_headers.get(key)
        if value:
            headers[key] = str(value)
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


def build_traceparent() -> str:
    return f"00-{uuid.uuid4().hex}-{random.getrandbits(64):016x}-00"


def build_statsig_id(path: str, method: str, config: dict[str, Any]) -> str:
    """Generate Grok's x-statsig-id header for app-chat requests.

    The browser signer builds:
      random_byte + gr meta bytes + time bucket bytes + sha256(input)[:16] + version
    then XORs every byte after the first with the random byte and base64 encodes
    without padding.
    """
    meta_value = str(config.get("statsig_meta") or "").strip()
    if not meta_value:
        raise RuntimeError("Missing Grok statsig_meta in config")
    meta = base64.b64decode(meta_value + "=" * (-len(meta_value) % 4))
    fingerprint = str(config.get("statsig_fingerprint") or "f3de76100f5c28f5c28f5c00f5c28f5c28f5c100")
    time_bucket = int(time.time()) - 0x644F6370
    time_bytes = time_bucket.to_bytes(4, "little", signed=False)
    digest_input = f"{method.upper()}!{path}!{time_bucket}obfiowerehiring{fingerprint}".encode("utf-8")
    digest = hashlib.sha256(digest_input).digest()[:16]
    nonce = random.randrange(0, 256)
    payload = bytes([nonce]) + meta + time_bytes + digest + b"\x03"
    encoded = bytes(byte if index == 0 else byte ^ nonce for index, byte in enumerate(payload))
    return base64.b64encode(encoded).decode("ascii").rstrip("=")


def build_conversation_body(
    message: str,
    model: str | None,
    config: dict[str, Any],
    conversation_id: str | None = None,
) -> dict[str, Any]:
    viewport = config.get("device_env_info")
    if not isinstance(viewport, dict):
        viewport = {
            "darkModeEnabled": True,
            "devicePixelRatio": 1.0,
            "screenWidth": 1920,
            "screenHeight": 1080,
            "viewportWidth": 1280,
            "viewportHeight": 720,
        }
    body: dict[str, Any] = {
        "temporary": bool(config.get("temporary", False)),
        "message": message,
        "fileAttachments": [],
        "imageAttachments": [],
        "disableSearch": bool(config.get("disable_search", False)),
        "enableImageGeneration": bool(config.get("enable_image_generation", True)),
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": bool(config.get("enable_image_streaming", True)),
        "imageGenerationCount": int(config.get("image_generation_count", 2) or 2),
        "forceConcise": False,
        "enableSideBySide": True,
        "sendFinalMetadata": True,
        "disableTextFollowUps": False,
        "responseMetadata": {},
        "disableMemory": bool(config.get("disable_memory", False)),
        "forceSideBySide": False,
        "isAsyncChat": bool(config.get("is_async_chat", False)),
        "disableSelfHarmShortCircuit": False,
        "collectionIds": config.get("collection_ids") or [],
        "disabledConnectorIds": config.get("disabled_connector_ids") or [],
        "deviceEnvInfo": viewport,
        "modeId": mode_id_from_model(model, config),
        "linkQuery": False,
    }
    if conversation_id:
        body["conversationId"] = conversation_id
    return body


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)


def _extract_ids(value: Any) -> tuple[str | None, str | None]:
    conversation_id = None
    response_id = None
    if isinstance(value, dict):
        for key, item in value.items():
            key_lower = str(key).lower()
            if key_lower in {"conversationid", "conversation_id", "conversationuuid", "conversation_uuid"} and isinstance(item, str):
                conversation_id = item
            elif key_lower in {"messageid", "message_id", "responseid", "response_id"} and isinstance(item, str):
                response_id = item
            child_conv, child_resp = _extract_ids(item)
            conversation_id = conversation_id or child_conv
            response_id = response_id or child_resp
    elif isinstance(value, list):
        for item in value:
            child_conv, child_resp = _extract_ids(item)
            conversation_id = conversation_id or child_conv
            response_id = response_id or child_resp
    return conversation_id, response_id


def _extract_text(value: Any) -> str:
    text_parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("text", "content", "message", "delta", "response"):
                item = node.get(key)
                if isinstance(item, str) and item:
                    text_parts.append(item)
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    if text_parts:
        return text_parts[-1]
    strings = [s for s in _walk_strings(value) if len(s) > 1]
    return strings[-1] if strings else ""


def _event_from_payload(payload: Any, event_type: str | None = None) -> GrokStreamEvent | None:
    conversation_id, response_id = _extract_ids(payload)
    text = _extract_text(payload)
    if not text and not conversation_id and not response_id:
        return None
    return GrokStreamEvent(
        delta=text,
        conversation_id=conversation_id,
        response_id=response_id,
        event_type=event_type,
    )


def parse_sse_events(raw: str) -> Iterator[GrokStreamEvent]:
    event_type: str | None = None
    data_lines: list[str] = []
    for line in raw.splitlines():
        line = line.rstrip("\r")
        if not line:
            if data_lines:
                data_raw = "\n".join(data_lines)
                if data_raw != "[DONE]":
                    try:
                        payload = json.loads(data_raw)
                    except json.JSONDecodeError:
                        yield GrokStreamEvent(delta=data_raw, event_type=event_type)
                    else:
                        event = _event_from_payload(payload, event_type)
                        if event:
                            yield event
            event_type = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    if data_lines:
        data_raw = "\n".join(data_lines)
        if data_raw != "[DONE]":
            try:
                payload = json.loads(data_raw)
            except json.JSONDecodeError:
                yield GrokStreamEvent(delta=data_raw, event_type=event_type)
            else:
                event = _event_from_payload(payload, event_type)
                if event:
                    yield event


def parse_json_response(raw: str) -> Iterator[GrokStreamEvent]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        for match in re.finditer(r'"(?:text|content|message|delta)"\s*:\s*"((?:\\.|[^"\\])*)"', raw):
            yield GrokStreamEvent(delta=bytes(match.group(1), "utf-8").decode("unicode_escape"))
        return
    if isinstance(payload, dict) and payload.get("error"):
        return
    event = _event_from_payload(payload, "json")
    if event:
        yield event


def parse_json_lines(raw: str) -> Iterator[GrokStreamEvent]:
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("error"):
            continue
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict):
            conversation = result.get("conversation")
            if isinstance(conversation, dict):
                conversation_id = conversation.get("conversationId")
                if isinstance(conversation_id, str):
                    yield GrokStreamEvent(conversation_id=conversation_id, event_type="jsonl")

            response = result.get("response") if isinstance(result.get("response"), dict) else result
            token = response.get("token") if isinstance(response, dict) else None
            is_thinking = bool(response.get("isThinking")) if isinstance(response, dict) else False
            message_tag = response.get("messageTag") if isinstance(response, dict) else None
            response_id = None
            if isinstance(response, dict):
                model_response = response.get("modelResponse")
                if isinstance(model_response, dict) and isinstance(model_response.get("responseId"), str):
                    response_id = model_response.get("responseId")
                elif "userResponse" not in response and isinstance(response.get("responseId"), str):
                    response_id = response.get("responseId")
            if response_id:
                yield GrokStreamEvent(response_id=response_id, event_type="jsonl")

            ignored_tags = {"header", "summary", "tool_usage_card", "tool_partial_output", "tool_status"}
            if isinstance(token, str) and token and not is_thinking and message_tag not in ignored_tags:
                yield GrokStreamEvent(
                    delta=token,
                    response_id=response_id,
                    event_type="jsonl",
                )


def stream_new_conversation(
    session: requests.Session,
    config: dict[str, Any],
    prompt: str,
    model: str | None = None,
    conversation_id: str | None = None,
    parent_response_id: str | None = None,
) -> Iterator[GrokStreamEvent]:
    base_url = str(config.get("api_base", "https://grok.com")).rstrip("/")
    if conversation_id:
        request_path = f"/rest/app-chat/conversations/{conversation_id}/responses"
        url = f"{base_url}{request_path}"
        body = build_conversation_body(prompt, model, config)
        body.pop("temporary", None)
        body.pop("linkQuery", None)
        body.update(
            {
                "parentResponseId": parent_response_id or config.get("parent_response_id") or "",
                "metadata": {"request_metadata": {}},
                "isFromGrokFiles": False,
                "skipCancelCurrentInflightRequests": False,
                "isRegenRequest": False,
            }
        )
        if not body["parentResponseId"]:
            body.pop("parentResponseId", None)
    else:
        request_path = "/rest/app-chat/conversations/new"
        url = f"{base_url}{request_path}"
        body = build_conversation_body(prompt, model, config, conversation_id)
    request_headers = {
        "x-xai-request-id": str(uuid.uuid4()),
        "x-statsig-id": build_statsig_id(request_path, "POST", config),
        "traceparent": build_traceparent(),
        "referer": "https://grok.com/?q=&reasoningMode=none&voice=false",
        "priority": "u=1, i",
    }
    timeout = int(config.get("timeout_seconds", 120) or 120)
    with session.post(
        url,
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        headers=request_headers,
        timeout=timeout,
    ) as response:
        text = response.text
        if response.status_code >= 400:
            raise GrokHTTPStatusError(response.status_code, text)
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type or "\ndata:" in text or text.startswith("data:"):
            yield from parse_sse_events(text)
            return
        if "\n{" in text or text.lstrip().startswith("{") and "\n" in text:
            yield from parse_json_lines(text)
            return
        yield from parse_json_response(text)


def print_result(events: Iterator[GrokStreamEvent]) -> None:
    saw_text = False
    conversation_id = None
    response_id = None
    for event in events:
        if event.conversation_id:
            conversation_id = event.conversation_id
        if event.response_id:
            response_id = event.response_id
        if event.delta:
            saw_text = True
            safe_print(event.delta, end="")
    safe_print("")
    if conversation_id or response_id:
        safe_print(
            json.dumps(
                {"conversation_id": conversation_id, "response_id": response_id},
                ensure_ascii=False,
                indent=2,
            )
        )
    if not saw_text:
        safe_print("[Grok Web] no parsed text events")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Grok Web pure HTTP client")
    parser.add_argument("message")
    parser.add_argument("--model", default="grok-fast")
    parser.add_argument("--conversation-id")
    parser.add_argument("--parent-response-id")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cookies, headers, config = load_config()
    session = make_session(cookies, headers, config)
    events = stream_new_conversation(
        session,
        config,
        args.message,
        args.model,
        conversation_id=args.conversation_id,
        parent_response_id=args.parent_response_id,
    )
    print_result(events)


if __name__ == "__main__":
    main()
