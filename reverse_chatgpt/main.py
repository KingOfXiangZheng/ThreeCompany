#!/usr/bin/env python3
"""
Pure HTTP ChatGPT Web reverse client.

No browser, no Playwright, no Chrome CDP. Credentials are supplied explicitly
through environment variables or an ignored local config file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

import requests


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
LOCAL_CONFIG = CONFIG_DIR / "config.local.json"
EXAMPLE_CONFIG = CONFIG_DIR / "config.example.json"
PROTOCOL_HEADERS = CONFIG_DIR / "protocol_headers.local.json"

NON_TEXT_TYPES = {"thinking", "thoughts", "reasoning_recap", "model_editable_context"}
SENTINEL_HEADER_NAMES = {
    "openai-sentinel-chat-requirements-token",
    "openai-sentinel-turnstile-token",
    "openai-sentinel-proof-token",
    "x-conduit-token",
}


def safe_print(message: str = "", end: str = "\n") -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = str(message).encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, end=end, flush=True)


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    for path in (EXAMPLE_CONFIG, LOCAL_CONFIG):
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                config.update(json.load(f))
    return config


def load_dynamic_headers() -> dict[str, str]:
    headers: dict[str, str] = {}

    env_headers = os.environ.get("CHATGPT_PROTOCOL_HEADERS", "").strip()
    if env_headers:
        try:
            raw = json.loads(env_headers)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"CHATGPT_PROTOCOL_HEADERS is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise SystemExit("CHATGPT_PROTOCOL_HEADERS must be a JSON object.")
        headers.update({str(k): str(v) for k, v in raw.items() if v is not None})

    if PROTOCOL_HEADERS.exists():
        with PROTOCOL_HEADERS.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise SystemExit(f"{PROTOCOL_HEADERS} must contain a JSON object.")
        headers.update({str(k): str(v) for k, v in raw.items() if v is not None})

    return headers


def load_auth(required: bool = True) -> tuple[str, str | None, str | None, dict[str, str]]:
    cookie = os.environ.get("CHATGPT_COOKIE", "").strip()
    access_token = os.environ.get("CHATGPT_ACCESS_TOKEN", "").strip() or None
    user_agent = os.environ.get("CHATGPT_USER_AGENT", "").strip() or None
    dynamic_headers = load_dynamic_headers()

    auth_path = CONFIG_DIR / "auth.local.json"
    if auth_path.exists():
        with auth_path.open("r", encoding="utf-8") as f:
            auth = json.load(f)
        cookie = cookie or str(auth.get("cookie", "")).strip()
        access_token = access_token or str(auth.get("access_token", "")).strip() or None
        user_agent = user_agent or str(auth.get("user_agent", "")).strip() or None
        auth_headers = auth.get("protocol_headers")
        if isinstance(auth_headers, dict):
            dynamic_headers.update(
                {str(k): str(v) for k, v in auth_headers.items() if v is not None}
            )

    if required and not cookie:
        raise SystemExit(
            "Missing CHATGPT_COOKIE. Set it in the environment or create config/auth.local.json."
        )
    return cookie, access_token, user_agent, dynamic_headers


def cookie_value(cookie_header: str, name: str) -> str | None:
    prefix = f"{name}="
    for item in cookie_header.split(";"):
        item = item.strip()
        if item.startswith(prefix):
            return item[len(prefix):]
    return None


def get_device_id(session_info: dict[str, Any] | None, cookie: str) -> str:
    if isinstance(session_info, dict):
        device_id = session_info.get("oaiDeviceId")
        if isinstance(device_id, str) and device_id:
            return device_id

    device_id = cookie_value(cookie, "oai-did")
    if device_id:
        return device_id

    return str(uuid.uuid4())


def make_session(config: dict[str, Any], cookie: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": config.get("user_agent", "Mozilla/5.0"),
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": config.get("base_url", "https://chatgpt.com"),
            "Referer": config.get("base_url", "https://chatgpt.com") + "/",
            "Cookie": cookie,
        }
    )
    return session


def get_session_info(
    session: requests.Session,
    config: dict[str, Any],
    access_token: str | None,
) -> tuple[dict[str, Any] | None, str | None, str]:
    base_url = config.get("base_url", "https://chatgpt.com")
    resp = session.get(f"{base_url}/api/auth/session", timeout=30)
    if not resp.ok:
        return None, access_token, f"{resp.status_code} {resp.text[:300]}"
    data = resp.json()
    token = access_token or data.get("accessToken")
    user = data.get("user") if isinstance(data, dict) else None
    if not token and not user:
        return None, token, "session endpoint returned no user/accessToken"
    return data, token, ""


def warmup_chat_requirements(
    session: requests.Session,
    config: dict[str, Any],
    access_token: str | None,
    device_id: str,
    dynamic_headers: dict[str, str],
) -> None:
    """Best-effort browser-compatible warmup. This does not solve interactive challenges."""

    base_url = config.get("base_url", "https://chatgpt.com")
    headers = build_protocol_headers(
        config,
        access_token,
        device_id,
        dynamic_headers,
        accept="application/json",
    )
    headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )

    warmup_paths = (
        "/backend-api/f/conversation/prepare",
        "/backend-api/sentinel/heartbeat",
        "/backend-api/sentinel/chat-requirements/prepare",
        "/backend-api/sentinel/chat-requirements/finalize",
        "/backend-api/sentinel/req",
    )
    for path in warmup_paths:
        try:
            session.post(f"{base_url}{path}", headers=headers, data="{}", timeout=30)
        except requests.RequestException:
            continue


def build_protocol_headers(
    config: dict[str, Any],
    access_token: str | None,
    device_id: str,
    dynamic_headers: dict[str, str],
    *,
    accept: str = "text/event-stream",
) -> dict[str, str]:
    base_url = config.get("base_url", "https://chatgpt.com")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": accept,
        "oai-language": "en-US",
        "oai-device-id": device_id,
        "x-openai-target-path": "/backend-api/f/conversation",
        "x-openai-target-route": "/backend-api/f/conversation",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if "oai-client-version" in config:
        headers["oai-client-version"] = str(config["oai-client-version"])
    if "oai-client-build-number" in config:
        headers["oai-client-build-number"] = str(config["oai-client-build-number"])
    if "oai-session-id" not in dynamic_headers:
        headers["oai-session-id"] = str(uuid.uuid4())
    if "x-oai-turn-trace-id" not in dynamic_headers:
        headers["x-oai-turn-trace-id"] = str(uuid.uuid4())
    headers.update(dynamic_headers)
    if base_url:
        headers.setdefault("Referer", f"{base_url}/")
    return headers


def build_body(
    prompt: str,
    config: dict[str, Any],
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    model_id = model or config.get("model", "gpt-5-3")
    parent_id = parent_message_id or str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    body: dict[str, Any] = {
        "action": "next",
        "messages": [
            {
                "id": message_id,
                "author": {"role": "user"},
                "content": {
                    "content_type": "text",
                    "parts": [prompt],
                },
            }
        ],
        "conversation_id": conversation_id,
        "parent_message_id": parent_id,
        "model": model_id,
        "timezone_offset_min": int(config.get("timezone_offset_min", -480)),
        "timezone": config.get("timezone", "Asia/Shanghai"),
        "history_and_training_disabled": False,
        "conversation_mode": {"kind": "primary_assistant", "plugin_ids": None},
        "force_paragen": False,
        "force_paragen_model_slug": "",
        "force_rate_limit": False,
        "reset_rate_limits": False,
        "force_use_sse": True,
    }

    if model_id == "gpt-5-5-thinking":
        body.update(
            {
                "thinking_effort": "extended",
                "supports_buffering": True,
                "supported_encodings": ["v1"],
                "enable_message_followups": True,
                "paragen_cot_summary_display_override": "allow",
                "force_parallel_switch": "auto",
                "client_contextual_info": {
                    "is_dark_mode": False,
                    "time_since_loaded": 10,
                    "page_height": 900,
                    "page_width": 1440,
                    "pixel_ratio": 1,
                    "screen_height": 1080,
                    "screen_width": 1920,
                    "app_name": "chatgpt.com",
                },
            }
        )
    return body


def request_conversation(
    session: requests.Session,
    config: dict[str, Any],
    body: dict[str, Any],
    access_token: str | None,
    device_id: str,
    dynamic_headers: dict[str, str],
) -> requests.Response:
    base_url = config.get("base_url", "https://chatgpt.com")
    headers = build_protocol_headers(config, access_token, device_id, dynamic_headers)
    return session.post(
        f"{base_url}/backend-api/f/conversation",
        headers=headers,
        data=json.dumps(body, ensure_ascii=False),
        timeout=180,
        stream=True,
    )


def iter_sse_lines(resp: requests.Response) -> Iterable[str]:
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw.strip()
        if line.startswith("data: "):
            yield line[6:].strip()


def extract_visible_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        chunks: list[str] = []
        for item in value:
            chunks.extend(extract_visible_text(item))
        return chunks
    if not isinstance(value, dict):
        return []

    content_type = str(value.get("content_type") or value.get("type") or value.get("kind") or "")
    if content_type in NON_TEXT_TYPES:
        return []

    chunks = []
    for key in ("text", "value"):
        item = value.get(key)
        if isinstance(item, str) and item:
            chunks.append(item)

    nested = value.get("content")
    if isinstance(nested, str) and nested:
        chunks.append(nested)
    elif isinstance(nested, (dict, list)):
        chunks.extend(extract_visible_text(nested))

    if isinstance(value.get("parts"), list):
        chunks.extend(extract_visible_text(value["parts"]))

    return chunks


def extract_content(event: dict[str, Any]) -> str | None:
    message = event.get("message", {})
    if not isinstance(message, dict):
        return None
    content = message.get("content", {})
    if not isinstance(content, dict):
        return None
    if content.get("content_type") in NON_TEXT_TYPES:
        return None
    chunks = extract_visible_text(content.get("parts", []))
    if not chunks:
        chunks = extract_visible_text(content)
    return "".join(chunks) if chunks else None


def parse_sse_events(events: Iterable[str]) -> tuple[str, str | None, str | None, bool]:
    accumulated = ""
    current_message_id = None
    conversation_id = None
    assistant_message_id = None
    handoff = False

    for data in events:
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type", "")
        if event_type in ("resume_conversation_token", "stream_handoff"):
            handoff = True
            conversation_id = conversation_id or event.get("conversation_id")
            continue

        conversation_id = conversation_id or event.get("conversation_id")
        message = event.get("message", {})
        if isinstance(message, dict) and message.get("id"):
            if message.get("author", {}).get("role") == "assistant":
                assistant_message_id = message["id"]

        if not isinstance(message, dict) or message.get("author", {}).get("role") != "assistant":
            continue

        msg_id = message.get("id")
        if msg_id and msg_id != current_message_id:
            current_message_id = msg_id
            accumulated = ""

        content = extract_content(event)
        if isinstance(content, str) and content:
            accumulated = content

    return accumulated, conversation_id, assistant_message_id, handoff


def fetch_conversation(
    session: requests.Session,
    config: dict[str, Any],
    conversation_id: str,
    access_token: str | None,
) -> tuple[str | None, str | None]:
    base_url = config.get("base_url", "https://chatgpt.com")
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    resp = session.get(f"{base_url}/backend-api/conversation/{conversation_id}", headers=headers, timeout=60)
    if not resp.ok:
        return None, None
    data = resp.json()
    latest = None
    latest_time = -1
    for node in (data.get("mapping") or {}).values():
        msg = node.get("message") if isinstance(node, dict) else None
        if not isinstance(msg, dict):
            continue
        if msg.get("author", {}).get("role") != "assistant":
            continue
        content = msg.get("content") or {}
        if content.get("content_type") in NON_TEXT_TYPES:
            continue
        create_time = msg.get("create_time") or 0
        if create_time > latest_time:
            latest = msg
            latest_time = create_time
    if not latest:
        return None, None
    text = extract_content({"message": latest})
    return text, latest.get("id")


async def run_browser_backend(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(ROOT.parent))
    from reverse_chatgpt.chatgpt_web import ChatGPTWebClient

    user_data_dir = args.user_data_dir or str(ROOT.parent / "chrome_data")
    client = await ChatGPTWebClient.create(
        cdp_port=args.cdp_port,
        attach_only=args.attach_only,
        user_data_dir=user_data_dir,
    )
    try:
        chunks: list[str] = []
        conversation_id = None
        message_id = None
        async for delta, conv_id, msg_id in client.chat_completions(
            args.message,
            conversation_id=args.conversation_id,
            parent_message_id=args.parent_message_id,
            model=args.model,
        ):
            if delta:
                chunks.append(delta)
            conversation_id = conversation_id or conv_id
            message_id = message_id or msg_id

        safe_print("\n========== response ==========")
        safe_print("".join(chunks))
        safe_print("\n========== metadata ==========")
        safe_print(
            json.dumps(
                {
                    "backend": "browser",
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await client.close()


def run(args: argparse.Namespace) -> None:
    config = load_config()
    cookie, access_token, auth_user_agent, dynamic_headers = load_auth(required=not args.check)
    if auth_user_agent:
        config["user_agent"] = auth_user_agent
    if args.check and not cookie:
        safe_print(
            json.dumps(
                {
                    "session_ok": False,
                    "has_access_token": False,
                    "error": "missing CHATGPT_COOKIE",
                },
                indent=2,
            )
        )
        return
    session = make_session(config, cookie)
    session_info, access_token, auth_error = get_session_info(session, config, access_token)
    device_id = get_device_id(session_info, cookie)

    if args.check:
        safe_print(json.dumps(
            {
                "session_ok": bool(session_info),
                "has_access_token": bool(access_token),
                "has_user_agent": bool(config.get("user_agent")),
                "has_stable_device_id": bool(device_id),
                "dynamic_header_count": len(dynamic_headers),
                "has_sentinel_headers": any(
                    name in {k.lower() for k in dynamic_headers} for name in SENTINEL_HEADER_NAMES
                ),
                "error": auth_error,
            },
            indent=2,
        ))
        return

    if not session_info and not access_token:
        raise SystemExit(f"Authentication check failed: {auth_error}")

    body = build_body(
        args.message,
        config,
        conversation_id=args.conversation_id,
        parent_message_id=args.parent_message_id,
        model=args.model,
    )
    warmup_chat_requirements(session, config, access_token, device_id, dynamic_headers)
    resp = request_conversation(session, config, body, access_token, device_id, dynamic_headers)
    if not resp.ok:
        safe_print(f"HTTP {resp.status_code}")
        safe_print(resp.text[:2000])
        if resp.status_code == 403:
            safe_print(
                "403 risk-control boundary: auth is valid and the HTTP client now uses "
                "/backend-api/f/conversation, but this protocol also requires dynamic "
                "browser-side sentinel/proof/conduit headers. Provide fresh headers through "
                "config/protocol_headers.local.json or CHATGPT_PROTOCOL_HEADERS, or use the "
                "browser-backed chatgpt_web path."
            )
            if args.backend == "auto":
                safe_print("[auto] falling back to browser backend...")
                asyncio.run(run_browser_backend(args))
        return

    text, conversation_id, message_id, handoff = parse_sse_events(iter_sse_lines(resp))
    if not text and conversation_id:
        text, fetched_msg_id = fetch_conversation(session, config, conversation_id, access_token)
        message_id = message_id or fetched_msg_id

    safe_print("\n========== response ==========")
    safe_print(text or "")
    safe_print("\n========== metadata ==========")
    safe_print(
        json.dumps(
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "handoff_seen": handoff,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pure HTTP ChatGPT Web reverse client")
    parser.add_argument("message", nargs="?", default="")
    parser.add_argument("--backend", choices=["auto", "http", "browser"], default="auto")
    parser.add_argument("--model")
    parser.add_argument("--conversation-id")
    parser.add_argument("--parent-message-id")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--attach-only", action="store_true")
    parser.add_argument("--user-data-dir")
    parser.add_argument("--check", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.check and not args.message:
        parser.print_help()
        return
    if args.backend == "browser" and not args.check:
        asyncio.run(run_browser_backend(args))
        return
    run(args)


if __name__ == "__main__":
    main()
