#!/usr/bin/env python3
"""
Pure HTTP Gemini Web reverse client.

This file intentionally does not use Playwright, Chrome CDP, or any browser
automation. It keeps the old batchexecute probe for evidence, then adds a
protocol-only StreamGenerate attempt based on tokens discovered from the
Gemini bootstrap HTML.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

try:
    import curl_cffi.requests as curl_requests
except Exception:  # pragma: no cover - optional transport
    curl_requests = None


ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
STATE_CACHE_PATH = ROOT.parent / "conv_cache" / "gemini_state.json"


DEFAULT_STREAM_PATHS = [
    "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate",
    "/_/BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate?rt=c",
]


GEMINI_MODE_CONFIGS: dict[str, dict[str, Any]] = {
    "gemini-3-fast": {
        "mode_id": "56fdd199312815e2",
        "mode_code": 1,
        "body_mode": [[1]],
        "name": "Gemini 3 Fast",
    },
    "gemini-3-thinking": {
        "mode_id": "e051ce1aa80aa576",
        "mode_code": 5,
        "body_mode": [[2]],
        "name": "Gemini 3 Thinking",
    },
    "gemini-3-pro": {
        "mode_id": "e6fa609c3fa255c0",
        "mode_code": 3,
        "body_mode": [[0]],
        "name": "Gemini 3 Pro",
    },
}

GEMINI_MODE_ALIASES = {
    "gemini-web": "gemini-3-pro",
    "gemini": "gemini-3-pro",
    "gemini-pro": "gemini-3-pro",
    "gemini-2.5-pro": "gemini-3-pro",
    "gemini-3": "gemini-3-pro",
    "gemini-3-pro": "gemini-3-pro",
    "pro": "gemini-3-pro",
    "gemini-3-thinking": "gemini-3-thinking",
    "thinking": "gemini-3-thinking",
    "gemini-thinking": "gemini-3-thinking",
    "gemini-3-fast": "gemini-3-fast",
    "gemini-3-flash": "gemini-3-fast",
    "gemini-flash": "gemini-3-fast",
    "fast": "gemini-3-fast",
    "flash": "gemini-3-fast",
}


def normalize_gemini_mode(model: str | None) -> str:
    key = (model or "gemini-3-pro").strip().lower().replace("_", "-").replace(" ", "-")
    if key in GEMINI_MODE_ALIASES:
        return GEMINI_MODE_ALIASES[key]
    if key in GEMINI_MODE_CONFIGS:
        return key
    safe_print(f"[Gemini] WARNING: unknown model '{model}', falling back to gemini-3-pro")
    return "gemini-3-pro"


def gemini_mode_config(model: str | None) -> dict[str, Any]:
    return GEMINI_MODE_CONFIGS[normalize_gemini_mode(model)]


def safe_print(message: str, end: str = "\n") -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = str(message).encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, end=end, flush=True)


def load_config() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with (CONFIG_DIR / "cookies.json").open("r", encoding="utf-8-sig") as f:
        cookies = json.load(f)
    with (CONFIG_DIR / "headers.json").open("r", encoding="utf-8-sig") as f:
        headers = json.load(f)
    with (CONFIG_DIR / "config.json").open("r", encoding="utf-8-sig") as f:
        config = json.load(f)
    return cookies, headers, config


def generate_session_id() -> str:
    timestamp = int(time.time() * 1000)
    random_part = random.randint(0, 9_999_999_999_999)
    return str(timestamp * 10_000_000_000_000 + random_part)


def make_cookie_header(cookies: dict[str, Any]) -> str:
    normalized: dict[str, Any] = {}
    for name, value in cookies.items():
        if name == "note" or value in (None, ""):
            continue
        normalized[name] = value
    normalized.setdefault("CONSENT", "YES+cb.20220419-08-p0.zh-CN+FX+111")
    normalized.setdefault("SOCS", "CAISHAgBEhJnd3NfMjAyMjEyMDgtMF9SQzIaAmVuIAEaBgiA_LyaBg")

    parts: list[str] = []
    for name, value in normalized.items():
        cookie_value = str(value)
        if name == "__Secure-ENID" and not cookie_value.startswith("33.SE="):
            cookie_value = f"33.SE={cookie_value}"
        parts.append(f"{name}={cookie_value}")
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
    for key in ("GEMINI_PROXY", "HTTPS_PROXY", "ALL_PROXY", "https_proxy", "all_proxy"):
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
                    parts = dict(
                        part.split("=", 1)
                        for part in proxy_server.split(";")
                        if "=" in part
                    )
                    proxy_server = parts.get("https") or parts.get("http") or next(iter(parts.values()), "")
                return _normalize_proxy_url(proxy_server)
    except Exception:
        pass
    return ""


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
        kwargs.pop("stream", None)
        if self._proxy_url and "proxy" not in kwargs:
            kwargs["proxy"] = self._proxy_url
        kwargs.setdefault("allow_redirects", False)
        kwargs["headers"] = headers
        last_exc: BaseException | None = None
        for _ in range(2):
            try:
                response = curl_requests.request(method, url, **kwargs)
                return _ResponseContextAdapter(response)
            except Exception as exc:
                if not is_request_error(exc):
                    raise
                last_exc = exc
        if last_exc:
            raise last_exc
        raise RuntimeError("curl_cffi request failed without exception")

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._request("POST", url, **kwargs)

    def close(self) -> None:
        return None


def make_session(cookies: dict[str, Any], base_headers: dict[str, Any]) -> requests.Session:
    proxy_url = _system_proxy_url()
    if curl_requests is not None:
        session = _CurlCffiStatelessSession(proxy_url)
    else:
        session = requests.Session()
    headers = {
        "user-agent": base_headers.get(
            "user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
        ),
        "accept": "*/*",
        "accept-language": base_headers.get("accept-language", "zh-CN,zh;q=0.9,en;q=0.8"),
        "origin": "https://gemini.google.com",
        "referer": "https://gemini.google.com/",
        "x-same-domain": "1",
        "connection": "close",
    }
    session.headers.update(headers)
    cookie_header = make_cookie_header(cookies)
    if cookie_header:
        session.headers["cookie"] = cookie_header
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def is_request_error(exc: BaseException) -> bool:
    if isinstance(exc, requests.RequestException):
        return True
    return bool(curl_requests is not None and isinstance(exc, curl_requests.exceptions.RequestException))


def build_query_params(config: dict[str, Any], rpc_id: str | None = None, req_id: int | None = None) -> str:
    params: dict[str, Any] = {
        "source-path": "/app",
        "bl": config.get("version", ""),
        "f.sid": generate_session_id(),
        "hl": config.get("hl", "zh-CN"),
        "_reqid": req_id or random.randint(1_000_000, 9_000_000),
        "rt": "c",
    }
    if rpc_id:
        params = {"rpcids": rpc_id, **params}
    return urllib.parse.urlencode(params)


def build_batchexecute_body(rpc_id: str, params: list[Any]) -> str:
    req_data = [[[rpc_id, json.dumps(params, ensure_ascii=False), None, "generic"]]]
    return urllib.parse.urlencode({"f.req": json.dumps(req_data, ensure_ascii=False)})


def build_batchexecute_url(
    config: dict[str, Any],
    rpc_id: str,
    f_sid: str | None = None,
    source_path: str = "/app",
) -> str:
    params = {
        "rpcids": rpc_id,
        "source-path": source_path,
        "bl": config.get("version", ""),
        "f.sid": f_sid or generate_session_id(),
        "hl": config.get("hl", "zh-CN"),
        "_reqid": random.randint(1_000_000, 9_000_000),
        "rt": "c",
    }
    return f"{config['api_base']}{config['batchexecute_path']}?{urllib.parse.urlencode(params)}"


def build_batchexecute_headers(referer: str, model: str | None = None) -> dict[str, str]:
    mode_config = gemini_mode_config(model)
    return {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        "accept": "*/*",
        "x-same-domain": "1",
        "referer": referer,
        "origin": "https://gemini.google.com",
        "x-goog-ext-73010989-jspb": "[0]",
        "x-goog-ext-525001261-jspb": json.dumps(
            [
                1,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [4],
                None,
                None,
                None,
                None,
                None,
                mode_config["mode_code"],
                None,
                "2C05E3A0-5408-42E2-A019-F54D5A951907",
            ],
            separators=(",", ":"),
        ),
    }


def batchexecute_probe(session: requests.Session, config: dict[str, Any], message: str) -> dict[str, Any]:
    url = f"{config['api_base']}{config['batchexecute_path']}?{build_query_params(config, 'aPya6c')}"
    body = build_batchexecute_body("aPya6c", [None, message, None, None, [0]])
    headers = {
        "content-type": "application/x-www-form-urlencoded;charset=utf-8",
        "x-goog-ext-73010989-jspb": "[0]",
        "x-goog-ext-525001261-jspb": (
            '[1,null,null,null,null,null,null,null,[4],null,null,null,null,null,1,null,'
            '"225CDC15-DF99-4545-A619-74C720D374A7"]'
        ),
    }
    response = session.post(url, data=body, headers=headers, timeout=30, verify=False)
    return {
        "url": url,
        "status": response.status_code,
        "content_type": response.headers.get("content-type"),
        "text": response.text,
    }


def post_generation_ack(
    session: requests.Session,
    config: dict[str, Any],
    bootstrap: Bootstrap,
    response_id: str | None,
    model: str | None = None,
) -> dict[str, Any] | None:
    if not response_id:
        return None
    url = build_batchexecute_url(config, "PCck7e", bootstrap.f_sid)
    body_params = {"f.req": json.loads(urllib.parse.parse_qs(build_batchexecute_body("PCck7e", [response_id]))["f.req"][0])}
    encoded_body = {"f.req": json.dumps(body_params["f.req"], ensure_ascii=False)}
    if bootstrap.at:
        encoded_body["at"] = bootstrap.at
    body = urllib.parse.urlencode(encoded_body)
    headers = build_batchexecute_headers(bootstrap.url, model)
    last_error = ""
    for attempt in range(3):
        try:
            response = session.post(
                url,
                data=body,
                headers=headers,
                timeout=30,
                verify=False,
            )
            return {
                "url": url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "sample": response.text[:800],
                "attempt": attempt + 1,
            }
        except Exception as exc:
            if not is_request_error(exc):
                raise
            last_error = str(exc)
            safe_print(f"[Gemini Web] post-generation ack failed: attempt={attempt + 1}, error={exc}")
            try:
                session.close()
            except Exception:
                pass
    return {
        "url": url,
        "status": None,
        "content_type": None,
        "sample": "",
        "error": last_error,
        "attempt": 3,
    }


@dataclass
class Bootstrap:
    url: str
    status: int
    bl: str | None
    f_sid: str | None
    at: str | None
    stream_paths: list[str]
    html_sample: str


class ConsentFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name.lower(): value or "" for name, value in attrs}
        if tag.lower() == "form":
            self._current = {
                "action": attr.get("action", ""),
                "method": attr.get("method", "get").lower(),
                "inputs": {},
            }
            self.forms.append(self._current)
            return
        if tag.lower() == "input" and self._current is not None:
            name = attr.get("name")
            if name:
                self._current["inputs"][name] = attr.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self._current = None


def maybe_accept_consent(session: requests.Session, response: requests.Response) -> requests.Response:
    if "consent.google.com" not in response.url and "ConsentUi" not in response.text:
        return response

    parser = ConsentFormParser()
    parser.feed(response.text)
    candidates = [
        form
        for form in parser.forms
        if "save" in str(form.get("action", "")) or "consent" in str(form.get("action", ""))
    ]
    if not candidates:
        return response

    for form in candidates:
        action = urllib.parse.urljoin(response.url, str(form.get("action", "")))
        data = dict(form.get("inputs", {}))
        data.setdefault("set_eom", "true")
        data.setdefault("set_sc", "true")
        data.setdefault("set_aps", "true")
        data.setdefault("continue", "https://gemini.google.com/u/1/")

        try:
            submitted = session.post(
                action,
                data=data,
                headers={
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": "https://consent.google.com",
                    "referer": response.url,
                },
                timeout=30,
                verify=False,
                allow_redirects=True,
            )
        except Exception as exc:
            if not is_request_error(exc):
                raise
            continue
        if "consent.google.com" not in submitted.url:
            return submitted

    return response


def first_regex(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return bytes(match.group(1), "utf-8").decode("unicode_escape")
    return None


def discover_stream_paths(html: str) -> list[str]:
    found = set()
    for match in re.finditer(r'(/_/BardChatUi/data/[^"\'<>\\\s]*StreamGenerate[^"\'<>\\\s]*)', html):
        found.add(match.group(1).replace("\\u0026", "&"))
    return sorted(found)


AT_TOKEN_CACHE = CONFIG_DIR / "at_token.txt"
BOOTSTRAP_CACHE = CONFIG_DIR / "bootstrap.json"


def _read_cached_at_token() -> str | None:
    try:
        if AT_TOKEN_CACHE.exists():
            token = AT_TOKEN_CACHE.read_text("utf-8").strip()
            if token:
                return token
    except Exception:
        pass
    return None


def _write_cached_at_token(token: str | None) -> None:
    if not token:
        return
    try:
        AT_TOKEN_CACHE.write_text(token, "utf-8")
    except Exception:
        pass


def _read_cached_bootstrap() -> dict[str, Any]:
    try:
        if BOOTSTRAP_CACHE.exists():
            value = json.loads(BOOTSTRAP_CACHE.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
    except Exception:
        pass
    return {}


def _write_cached_bootstrap(bootstrap: Bootstrap) -> None:
    try:
        BOOTSTRAP_CACHE.write_text(
            json.dumps(
                {
                    "url": bootstrap.url,
                    "status": bootstrap.status,
                    "bl": bootstrap.bl,
                    "f_sid": bootstrap.f_sid,
                    "at": bootstrap.at,
                    "stream_paths": bootstrap.stream_paths,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
    except Exception:
        pass


def _bootstrap_url_candidates(gemini_url: str) -> list[str]:
    candidates = [gemini_url]
    parsed = urllib.parse.urlparse(gemini_url)
    if parsed.scheme and parsed.netloc:
        origin = f"{parsed.scheme}://{parsed.netloc}"
        candidates.extend([origin + "/", origin + "/app"])
    else:
        candidates.extend(["https://gemini.google.com/", "https://gemini.google.com/app"])
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def fetch_bootstrap(session: requests.Session, config: dict[str, Any], gemini_url: str) -> Bootstrap:
    response: requests.Response | None = None
    errors: list[str] = []
    for candidate in _bootstrap_url_candidates(gemini_url):
        try:
            candidate_response = session.get(candidate, timeout=30, verify=False, allow_redirects=True)
            candidate_response = maybe_accept_consent(session, candidate_response)
            if "consent.google.com" in candidate_response.url:
                errors.append(f"{candidate} -> consent redirect")
                continue
            response = candidate_response
            break
        except Exception as exc:
            if not is_request_error(exc):
                raise
            errors.append(f"{candidate} -> {exc}")

    if response is None:
        cached_bootstrap = _read_cached_bootstrap()
        cached_at = _read_cached_at_token()
        cached_bl = cached_bootstrap.get("bl") or config.get("version")
        if cached_bl:
            config["version"] = cached_bl
        safe_print("[Gemini] bootstrap failed, using cached protocol metadata: " + " | ".join(errors[-3:]))
        return Bootstrap(
            url=str(cached_bootstrap.get("url") or gemini_url),
            status=0,
            bl=cached_bl,
            f_sid=cached_bootstrap.get("f_sid"),
            at=cached_bootstrap.get("at") or cached_at,
            stream_paths=cached_bootstrap.get("stream_paths") or list(DEFAULT_STREAM_PATHS),
            html_sample="",
        )

    html = response.text
    bl = first_regex(
        [
            r'"cfb2h"\s*:\s*"([^"]+)"',
            r'"bl"\s*:\s*"([^"]+)"',
            r'(boq_assistant-bard-web-server_[^"\'<>\\,\]]+)',
        ],
        html,
    )
    at = first_regex(
        [
            r'"SNlM0e"\s*:\s*"([^"]+)"',
            r'\["SNlM0e"\s*,\s*"([^"]+)"\]',
            r'"SNlM0e"\s*,\s*"([^"]+)"',
        ],
        html,
    )
    if not at:
        at = _read_cached_at_token()
    else:
        _write_cached_at_token(at)
    f_sid = first_regex(
        [
            r'"FdrFJe"\s*:\s*"([^"]+)"',
            r'\["FdrFJe"\s*,\s*"([^"]+)"\]',
        ],
        html,
    )
    paths = discover_stream_paths(html)
    if not paths:
        paths = list(DEFAULT_STREAM_PATHS)
    if bl:
        config["version"] = bl
    bootstrap = Bootstrap(
        url=response.url,
        status=response.status_code,
        bl=bl,
        f_sid=f_sid,
        at=at,
        stream_paths=paths,
        html_sample=html[:500],
    )
    _write_cached_bootstrap(bootstrap)
    return bootstrap


def build_stream_inner(
    message: str,
    conversation_id: str | None = None,
    response_id: str | None = None,
    model: str | None = None,
    request_id: str | None = None,
    request_context_token: str | None = None,
    client_context_id: str | None = None,
    candidate_id: str | None = None,
    conversation_token: str | None = None,
) -> list[Any]:
    mode_config = gemini_mode_config(model)
    if request_id is None:
        request_id = str(uuid.uuid4()).upper()
    state = ["", "", "", None, None, None, None, None, None, ""]
    if conversation_id and response_id:
        state[0] = conversation_id
        state[1] = response_id
        if candidate_id:
            state[2] = candidate_id
        if conversation_token:
            state[9] = conversation_token
    inner = [None] * 80
    inner[0] = [message, 0, None, None, None, None, 0]
    inner[1] = ["zh-CN"]
    inner[2] = state
    if request_context_token:
        if client_context_id is None:
            client_context_id = uuid.uuid4().hex
        inner[3] = request_context_token
        inner[4] = client_context_id
        inner[57] = request_id
    inner[6] = [1]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = mode_config["body_mode"]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    inner[41] = [1]
    inner[53] = 0
    inner[61] = []
    inner[68] = 2
    inner[79] = mode_config["mode_code"]
    return inner


def build_stream_body(
    message: str,
    at: str | None,
    conversation_id: str | None = None,
    response_id: str | None = None,
    model: str | None = None,
    request_id: str | None = None,
    request_context_token: str | None = None,
    client_context_id: str | None = None,
    candidate_id: str | None = None,
    conversation_token: str | None = None,
) -> str:
    inner = build_stream_inner(
        message,
        conversation_id,
        response_id,
        model,
        request_id=request_id,
        request_context_token=request_context_token,
        client_context_id=client_context_id,
        candidate_id=candidate_id,
        conversation_token=conversation_token,
    )
    outer = [None, json.dumps(inner, ensure_ascii=False, separators=(",", ":"))]
    body = {"f.req": json.dumps(outer, ensure_ascii=False, separators=(",", ":"))}
    if at:
        body["at"] = at
    return urllib.parse.urlencode(body)


def build_stream_headers(
    referer: str,
    model: str | None = None,
    request_id: str | None = None,
) -> dict[str, str]:
    if request_id is None:
        request_id = str(uuid.uuid4()).upper()
    mode_config = gemini_mode_config(model)
    return {
        "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
        "accept": "*/*",
        "x-same-domain": "1",
        "referer": referer,
        "origin": "https://gemini.google.com",
        "x-goog-ext-73010989-jspb": "[0]",
        "x-goog-ext-73010990-jspb": "[0,0,0]",
        "x-goog-ext-525005358-jspb": json.dumps([request_id, 1], separators=(",", ":")),
        "x-goog-ext-525001261-jspb": json.dumps(
            [
                1,
                None,
                None,
                None,
                mode_config["mode_id"],
                None,
                None,
                0,
                [4],
                None,
                None,
                2,
                None,
                None,
                mode_config["mode_code"],
                None,
                "2C05E3A0-5408-42E2-A019-F54D5A951907",
            ],
            separators=(",", ":"),
        ),
    }


def with_query(path: str, config: dict[str, Any], f_sid: str | None = None) -> str:
    if "?" in path:
        base, existing = path.split("?", 1)
        params = urllib.parse.parse_qs(existing, keep_blank_values=True)
    else:
        base = path
        params = {}
    params.setdefault("source-path", ["/app"])
    params.setdefault("bl", [config.get("version", "")])
    params.setdefault("f.sid", [f_sid or generate_session_id()])
    params.setdefault("hl", [config.get("hl", "zh-CN")])
    params.setdefault("_reqid", [str(random.randint(1_000_000, 9_000_000))])
    params.setdefault("rt", ["c"])
    return f"{base}?{urllib.parse.urlencode(params, doseq=True)}"


def parse_stream_response(raw: str) -> list[tuple[str, str | None, str | None]]:
    events: list[tuple[str, str | None, str | None]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            batch = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in batch:
            if not isinstance(item, list) or len(item) < 3:
                continue
            payload_raw = item[2]
            if not isinstance(payload_raw, str) or not payload_raw.startswith("["):
                continue
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                continue
            conv_id, resp_id = extract_ids(payload)
            text = extract_response_text(payload)
            if text or conv_id or resp_id:
                events.append((text, conv_id, resp_id))
    return events


def _walk_strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_strings(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_walk_strings(item))
    return found


def extract_stream_state(raw: str) -> dict[str, str]:
    state: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            batch = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in batch:
            if not isinstance(item, list) or len(item) < 3:
                continue
            payload_raw = item[2]
            if not isinstance(payload_raw, str) or not payload_raw.startswith("["):
                continue
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                continue
            for text in _walk_strings(payload):
                if text.startswith("c_"):
                    state["conversation_id"] = text
                elif text.startswith("r_"):
                    state["response_id"] = text
                elif text.startswith("rc_"):
                    state["candidate_id"] = text
            if isinstance(payload, list) and len(payload) >= 3 and isinstance(payload[2], dict):
                meta = payload[2]
                token_values = meta.get("21")
                if isinstance(token_values, list) and token_values and isinstance(token_values[0], str):
                    state["conversation_token"] = token_values[0]
                response_id = meta.get("18")
                if isinstance(response_id, str) and response_id.startswith("r_"):
                    state["response_id"] = response_id
    return state


def _load_gemini_state_cache() -> dict[str, Any]:
    try:
        if STATE_CACHE_PATH.exists():
            return json.loads(STATE_CACHE_PATH.read_text("utf-8"))
    except Exception:
        pass
    return {}


def _save_gemini_state_cache(cache: dict[str, Any]) -> None:
    try:
        STATE_CACHE_PATH.parent.mkdir(exist_ok=True)
        STATE_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def save_gemini_stream_state(state: dict[str, str]) -> None:
    conversation_id = state.get("conversation_id")
    response_id = state.get("response_id")
    if not conversation_id or not response_id:
        return
    cache = _load_gemini_state_cache()
    value = {
        "conversation_id": conversation_id,
        "response_id": response_id,
        "candidate_id": state.get("candidate_id", ""),
        "conversation_token": state.get("conversation_token", ""),
    }
    cache[f"{conversation_id}:{response_id}"] = value
    cache[f"latest:{conversation_id}"] = value
    _save_gemini_state_cache(cache)


def load_gemini_stream_state(conversation_id: str | None, response_id: str | None) -> dict[str, str]:
    if not conversation_id or not response_id:
        return {}
    cache = _load_gemini_state_cache()
    value = cache.get(f"{conversation_id}:{response_id}")
    return value if isinstance(value, dict) else {}


def extract_bard_error_codes(raw: str) -> list[int]:
    codes: list[int] = []
    for match in re.finditer(r"BardErrorInfo\"(?:,\[([0-9]+)\])?", raw):
        if match.group(1):
            codes.append(int(match.group(1)))
    for match in re.finditer(r'\["er",null,null,null,null,([0-9]+)', raw):
        codes.append(int(match.group(1)))
    return codes


def extract_ids(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, list):
        for item in value:
            if (
                isinstance(item, list)
                and len(item) >= 2
                and isinstance(item[0], str)
                and item[0].startswith("c_")
                and isinstance(item[1], str)
                and item[1].startswith("r_")
            ):
                return item[0], item[1]
            conv_id, resp_id = extract_ids(item)
            if conv_id or resp_id:
                return conv_id, resp_id
    return None, None


def extract_response_text(value: Any) -> str:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            if (
                len(node) >= 2
                and isinstance(node[0], str)
                and node[0].startswith("rc_")
                and isinstance(node[1], list)
            ):
                parts = [part for part in node[1] if isinstance(part, str)]
                if parts:
                    found.append("".join(parts))
            for item in node:
                walk(item)

    walk(value)
    return found[-1] if found else ""


def stream_generate_attempt(
    session: requests.Session,
    config: dict[str, Any],
    bootstrap: Bootstrap,
    message: str,
    conversation_id: str | None = None,
    response_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    request_id = str(uuid.uuid4()).upper()
    body = build_stream_body(
        message,
        bootstrap.at,
        conversation_id,
        response_id,
        model,
        request_id=request_id,
        request_context_token=config.get("request_context_token"),
        client_context_id=config.get("client_context_id"),
    )
    headers = build_stream_headers(bootstrap.url, model, request_id=request_id)
    cached_state = load_gemini_stream_state(conversation_id, response_id)
    if cached_state:
        body = build_stream_body(
            message,
            bootstrap.at,
            conversation_id,
            response_id,
            model,
            request_id=request_id,
            request_context_token=config.get("request_context_token"),
            client_context_id=config.get("client_context_id"),
            candidate_id=cached_state.get("candidate_id"),
            conversation_token=cached_state.get("conversation_token"),
        )
    attempts = []

    for path in bootstrap.stream_paths:
        full_url = urllib.parse.urljoin(config["api_base"], with_query(path, config, bootstrap.f_sid))
        try:
            response = session.post(full_url, data=body, headers=headers, timeout=60, verify=False)
        except Exception as exc:
            if not is_request_error(exc):
                raise
            attempts.append(
                {
                    "url": full_url,
                    "status": None,
                    "content_type": None,
                    "events": [],
                    "post_generation_ack": None,
                    "sample": "",
                    "error": str(exc),
                }
            )
            continue
        events = parse_stream_response(response.text)
        ack = None
        if events:
            _, conversation_id, response_id = next(
                (event for event in reversed(events) if event[1] or event[2]),
                (None, None, None),
            )
            stream_state = extract_stream_state(response.text)
            if conversation_id:
                stream_state.setdefault("conversation_id", conversation_id)
            if response_id:
                stream_state.setdefault("response_id", response_id)
            save_gemini_stream_state(stream_state)
            ack = post_generation_ack(session, config, bootstrap, response_id, model)
        attempts.append(
            {
                "url": full_url,
                "status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "events": events,
                "post_generation_ack": ack,
                "sample": response.text[:1200],
            }
        )
        if events:
            return attempts[-1]

    return attempts[-1] if attempts else {"error": "no stream path candidates"}


def print_result(result: dict[str, Any], raw: bool = False) -> None:
    events = result.get("events") or []
    if events:
        text_event = next((event for event in reversed(events) if event[0]), events[-1])
        id_event = next((event for event in reversed(events) if event[1] or event[2]), text_event)
        text = text_event[0]
        metadata = {
            "conversation_id": id_event[1],
            "response_id": id_event[2],
            "continue_with": {
                "conversation_id": id_event[1],
                "response_id": id_event[2],
            },
        }
        safe_print("\n========== response ==========")
        safe_print(text)
        safe_print("\n========== metadata ==========")
        safe_print(json.dumps(metadata, ensure_ascii=False, indent=2))
        return
    safe_print("\n========== no parsed text ==========")
    safe_print(f"status: {result.get('status')}")
    safe_print(f"content-type: {result.get('content_type')}")
    safe_print(f"url: {result.get('url')}")
    safe_print("\n========== response sample ==========")
    safe_print(str(result.get("sample") or result.get("text") or "")[:4000] if raw else str(result.get("sample") or "")[:1200])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pure HTTP Gemini Web reverse client")
    parser.add_argument("message", nargs="?", help="message to send")
    parser.add_argument("--mode", choices=["stream", "batchexecute", "bootstrap"], default="stream")
    parser.add_argument("--gemini-url", default="https://gemini.google.com/u/1")
    parser.add_argument("--conversation-id")
    parser.add_argument("--response-id")
    parser.add_argument("--model", default="gemini-3-pro", choices=sorted(GEMINI_MODE_CONFIGS))
    parser.add_argument("--raw", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.message and args.mode != "bootstrap":
        parser.print_help()
        return
    if bool(args.conversation_id) != bool(args.response_id):
        raise SystemExit(
            "Continuation requires both --conversation-id and --response-id. "
            "Use the latest response_id returned by the previous request."
        )

    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    cookies, headers, config = load_config()
    session = make_session(cookies, headers)

    bootstrap = fetch_bootstrap(session, config, args.gemini_url)
    safe_print(
        json.dumps(
            {
                "bootstrap_status": bootstrap.status,
                "bootstrap_url": bootstrap.url,
                "bl": bootstrap.bl,
                "f_sid": bootstrap.f_sid,
                "has_at": bool(bootstrap.at),
                "stream_paths": bootstrap.stream_paths,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    if args.mode == "bootstrap":
        if args.raw:
            safe_print("\n========== html sample ==========")
            safe_print(bootstrap.html_sample)
        return

    if args.mode == "batchexecute":
        result = batchexecute_probe(session, config, args.message)
        safe_print("\n========== batchexecute ==========")
        safe_print(json.dumps({k: v for k, v in result.items() if k != "text"}, ensure_ascii=False, indent=2))
        safe_print("\n========== response sample ==========")
        safe_print(result["text"][:4000 if args.raw else 1200])
        return

    result = stream_generate_attempt(
        session,
        config,
        bootstrap,
        args.message,
        conversation_id=args.conversation_id,
        response_id=args.response_id,
        model=args.model,
    )
    print_result(result, raw=args.raw)


if __name__ == "__main__":
    main()
