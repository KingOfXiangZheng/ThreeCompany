#!/usr/bin/env python3
"""
Refresh reverse-client browser authentication through Chrome CDP.

The functions in this module connect to a Chrome instance with remote
debugging enabled, open the target web app, wait for an authenticated browser
session, then export cookies into the config files consumed by
reverse_chatgpt, reverse_gemini, and reverse_claude.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from playwright.async_api import Browser, BrowserContext, Page, async_playwright


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_USER_DATA_DIR = ROOT / "chrome_data"
CHATGPT_AUTH_PATH = ROOT / "reverse_chatgpt" / "config" / "auth.local.json"
GEMINI_COOKIES_PATH = ROOT / "reverse_gemini" / "config" / "cookies.json"
GEMINI_HEADERS_PATH = ROOT / "reverse_gemini" / "config" / "headers.json"
CLAUDE_COOKIES_PATH = ROOT / "reverse_claude" / "config" / "cookies.json"
CLAUDE_HEADERS_PATH = ROOT / "reverse_claude" / "config" / "headers.json"
CLAUDE_CONFIG_PATH = ROOT / "reverse_claude" / "config" / "config.json"

Progress = Callable[[str], None]
Target = Literal["chatgpt", "gemini", "claude", "all"]


@dataclass
class BrowserSession:
    playwright: Any
    browser: Browser
    context: BrowserContext
    process: subprocess.Popen[Any] | None
    external_browser: bool

    async def close(self, keep_browser_open: bool = True) -> None:
        try:
            await self.playwright.stop()
        finally:
            if self.process and not self.external_browser and not keep_browser_open:
                self.process.terminate()


@dataclass
class ChatGPTAuthSnapshot:
    cookie: str
    access_token: str | None
    user_agent: str
    cookie_count: int


@dataclass
class GeminiAuthSnapshot:
    cookies: dict[str, str]
    user_agent: str
    cookie_count: int
    has_enid: bool


@dataclass
class ClaudeAuthSnapshot:
    cookie: str
    user_agent: str
    cookie_count: int
    organization_id: str | None
    device_id: str | None
    has_session_key: bool


def _log(on_progress: Progress | None, message: str) -> None:
    if on_progress:
        on_progress(message)
    else:
        print(message, flush=True)


def _chrome_path(explicit_path: str | None = None) -> str:
    if explicit_path:
        return explicit_path

    if sys.platform == "win32":
        candidates = [
            Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        ]
        for path in candidates:
            if path.exists():
                return str(path)
    elif sys.platform == "darwin":
        path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if path.exists():
            return str(path)
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            try:
                subprocess.run([name, "--version"], capture_output=True, check=True)
                return name
            except Exception:
                continue

    return "chrome"


async def connect_cdp_browser(
    *,
    cdp_port: int = 9222,
    attach_only: bool = False,
    chrome_path: str | None = None,
    user_data_dir: str | Path | None = None,
    on_progress: Progress | None = None,
) -> BrowserSession:
    """Connect to Chrome CDP, starting Chrome when needed unless attach_only is set."""

    playwright = await async_playwright().start()
    cdp_url = f"http://127.0.0.1:{cdp_port}"
    process: subprocess.Popen[Any] | None = None
    external_browser = False

    try:
        _log(on_progress, f"[cdp] connecting to Chrome on {cdp_url}")
        browser = await playwright.chromium.connect_over_cdp(cdp_url)
        external_browser = True
    except Exception as exc:
        if attach_only:
            await playwright.stop()
            raise RuntimeError(
                f"Cannot connect to Chrome CDP on port {cdp_port}. Start Chrome with "
                f"--remote-debugging-port={cdp_port} first."
            ) from exc

        data_dir = Path(user_data_dir) if user_data_dir else DEFAULT_USER_DATA_DIR
        data_dir.mkdir(parents=True, exist_ok=True)
        exe = _chrome_path(chrome_path)
        _log(on_progress, f"[cdp] starting Chrome: {exe}")
        _log(on_progress, f"[cdp] profile: {data_dir}")
        process = subprocess.Popen(
            [
                exe,
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )

        last_error: Exception | None = None
        for _ in range(20):
            await asyncio.sleep(0.5)
            try:
                browser = await playwright.chromium.connect_over_cdp(cdp_url)
                break
            except Exception as connect_exc:
                last_error = connect_exc
        else:
            await playwright.stop()
            raise RuntimeError(f"Chrome started but CDP did not become ready on port {cdp_port}") from last_error

    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    return BrowserSession(
        playwright=playwright,
        browser=browser,
        context=context,
        process=process,
        external_browser=external_browser,
    )


async def _open_or_reuse_page(
    context: BrowserContext,
    url: str,
    host_marker: str,
    *,
    on_progress: Progress | None = None,
) -> Page:
    for page in context.pages:
        if host_marker in page.url:
            await page.bring_to_front()
            return page

    page = context.pages[0] if context.pages else await context.new_page()
    _log(on_progress, f"[browser] opening {url}")
    await page.goto(url, wait_until="domcontentloaded")
    await page.bring_to_front()
    return page


def _cookie_header(cookies: list[dict[str, Any]]) -> str:
    pairs = []
    seen: set[str] = set()
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name or name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


async def _wait_for_cookie(
    context: BrowserContext,
    urls: list[str],
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    timeout_seconds: int,
    on_progress: Progress | None = None,
    label: str,
) -> list[dict[str, Any]]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_notice = 0.0
    while True:
        cookies = await context.cookies(urls)
        if predicate(cookies):
            return cookies

        now = asyncio.get_running_loop().time()
        if now >= deadline:
            raise TimeoutError(f"Timed out waiting for {label} login cookies")
        if now - last_notice >= 10:
            _log(on_progress, f"[{label}] waiting for login cookies...")
            last_notice = now
        await asyncio.sleep(1)


async def _wait_for_user_confirmation(
    *,
    label: str,
    on_progress: Progress | None = None,
) -> None:
    _log(
        on_progress,
        f"[{label}] complete login in the browser, then press Enter here to export cookies...",
    )
    await asyncio.to_thread(sys.stdin.readline)


def _has_chatgpt_session(cookies: list[dict[str, Any]]) -> bool:
    names = {str(cookie.get("name") or "") for cookie in cookies}
    return (
        "__Secure-next-auth.session-token" in names
        or (
            "__Secure-next-auth.session-token.0" in names
            and "__Secure-next-auth.session-token.1" in names
        )
    )


def _has_gemini_session(cookies: list[dict[str, Any]]) -> bool:
    names = {str(cookie.get("name") or "") for cookie in cookies}
    return "__Secure-ENID" in names or "__Secure-1PSID" in names or "SID" in names


def _has_claude_session(cookies: list[dict[str, Any]]) -> bool:
    names = {str(cookie.get("name") or "") for cookie in cookies}
    return "sessionKey" in names and "lastActiveOrg" in names


async def refresh_chatgpt_auth(
    *,
    session: BrowserSession | None = None,
    cdp_port: int = 9222,
    attach_only: bool = False,
    chrome_path: str | None = None,
    user_data_dir: str | Path | None = None,
    output_path: str | Path = CHATGPT_AUTH_PATH,
    write: bool = True,
    timeout_seconds: int = 300,
    wait_for_login: bool = True,
    on_progress: Progress | None = None,
) -> ChatGPTAuthSnapshot:
    """Open ChatGPT in Chrome CDP and export Cookie plus /api/auth/session token."""

    owns_session = session is None
    session = session or await connect_cdp_browser(
        cdp_port=cdp_port,
        attach_only=attach_only,
        chrome_path=chrome_path,
        user_data_dir=user_data_dir,
        on_progress=on_progress,
    )
    try:
        page = await _open_or_reuse_page(
            session.context,
            "https://chatgpt.com/",
            "chatgpt.com",
            on_progress=on_progress,
        )
        user_agent = await page.evaluate("() => navigator.userAgent")
        if wait_for_login:
            await _wait_for_user_confirmation(label="chatgpt", on_progress=on_progress)
        cookies = await _wait_for_cookie(
            session.context,
            ["https://chatgpt.com", "https://chat.openai.com"],
            _has_chatgpt_session,
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
            label="chatgpt",
        )

        cookie_header = _cookie_header(cookies)
        session_info = await page.evaluate(
            """async () => {
                try {
                    const r = await fetch("https://chatgpt.com/api/auth/session", {
                        credentials: "include",
                    });
                    if (!r.ok) return null;
                    return await r.json();
                } catch {
                    return null;
                }
            }"""
        )
        access_token = None
        if isinstance(session_info, dict):
            access_token = session_info.get("accessToken") or None

        snapshot = ChatGPTAuthSnapshot(
            cookie=cookie_header,
            access_token=access_token,
            user_agent=user_agent,
            cookie_count=len(cookies),
        )
        if write:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "cookie": snapshot.cookie,
                        "access_token": snapshot.access_token,
                        "user_agent": snapshot.user_agent,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            _log(on_progress, f"[chatgpt] wrote auth to {path}")
        return snapshot
    finally:
        if owns_session:
            await session.close()


async def refresh_gemini_auth(
    *,
    session: BrowserSession | None = None,
    cdp_port: int = 9222,
    attach_only: bool = False,
    chrome_path: str | None = None,
    user_data_dir: str | Path | None = None,
    cookies_path: str | Path = GEMINI_COOKIES_PATH,
    headers_path: str | Path = GEMINI_HEADERS_PATH,
    write: bool = True,
    timeout_seconds: int = 300,
    wait_for_login: bool = True,
    on_progress: Progress | None = None,
) -> GeminiAuthSnapshot:
    """Open Gemini in Chrome CDP and export Google/Gemini cookies."""

    owns_session = session is None
    session = session or await connect_cdp_browser(
        cdp_port=cdp_port,
        attach_only=attach_only,
        chrome_path=chrome_path,
        user_data_dir=user_data_dir,
        on_progress=on_progress,
    )
    try:
        page = await _open_or_reuse_page(
            session.context,
            "https://gemini.google.com/u/1/",
            "gemini.google.com",
            on_progress=on_progress,
        )
        user_agent = await page.evaluate("() => navigator.userAgent")
        if wait_for_login:
            await _wait_for_user_confirmation(label="gemini", on_progress=on_progress)
        cookies = await _wait_for_cookie(
            session.context,
            ["https://gemini.google.com", "https://accounts.google.com", "https://google.com"],
            _has_gemini_session,
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
            label="gemini",
        )

        cookie_map: dict[str, str] = {}
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if name and value:
                cookie_map[name] = value

        snapshot = GeminiAuthSnapshot(
            cookies=cookie_map,
            user_agent=user_agent,
            cookie_count=len(cookie_map),
            has_enid=bool(cookie_map.get("__Secure-ENID")),
        )
        if write:
            cookies_file = Path(cookies_path)
            cookies_file.parent.mkdir(parents=True, exist_ok=True)
            cookie_payload = {
                "note": "Generated by tools/refresh_browser_auth.py from Chrome CDP.",
                **snapshot.cookies,
            }
            cookies_file.write_text(
                json.dumps(cookie_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            headers_file = Path(headers_path)
            headers: dict[str, Any] = {}
            if headers_file.exists():
                with headers_file.open("r", encoding="utf-8-sig") as f:
                    headers = json.load(f)
            headers["user-agent"] = snapshot.user_agent
            headers_file.write_text(
                json.dumps(headers, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _log(on_progress, f"[gemini] wrote cookies to {cookies_file}")
            _log(on_progress, f"[gemini] refreshed user-agent in {headers_file}")
        return snapshot
    finally:
        if owns_session:
            await session.close()


async def refresh_claude_auth(
    *,
    session: BrowserSession | None = None,
    cdp_port: int = 9222,
    attach_only: bool = False,
    chrome_path: str | None = None,
    user_data_dir: str | Path | None = None,
    cookies_path: str | Path = CLAUDE_COOKIES_PATH,
    headers_path: str | Path = CLAUDE_HEADERS_PATH,
    config_path: str | Path = CLAUDE_CONFIG_PATH,
    write: bool = True,
    timeout_seconds: int = 300,
    wait_for_login: bool = True,
    on_progress: Progress | None = None,
) -> ClaudeAuthSnapshot:
    """Open Claude in Chrome CDP and export Claude Web cookies/config."""

    owns_session = session is None
    session = session or await connect_cdp_browser(
        cdp_port=cdp_port,
        attach_only=attach_only,
        chrome_path=chrome_path,
        user_data_dir=user_data_dir,
        on_progress=on_progress,
    )
    try:
        page = await _open_or_reuse_page(
            session.context,
            "https://claude.ai/new",
            "claude.ai",
            on_progress=on_progress,
        )
        user_agent = await page.evaluate("() => navigator.userAgent")
        if wait_for_login:
            await _wait_for_user_confirmation(label="claude", on_progress=on_progress)
        cookies = await _wait_for_cookie(
            session.context,
            ["https://claude.ai", "https://api.anthropic.com", "https://a-api.anthropic.com"],
            _has_claude_session,
            timeout_seconds=timeout_seconds,
            on_progress=on_progress,
            label="claude",
        )

        cookie_map: dict[str, str] = {}
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if name and value:
                cookie_map[name] = value

        cookie_header = _cookie_header(cookies)
        organization_id = cookie_map.get("lastActiveOrg")
        device_id = cookie_map.get("anthropic-device-id")
        if not device_id:
            device_id = await page.evaluate(
                """() => {
                    try {
                        return localStorage.getItem("anthropic-device-id")
                            || document.cookie.match(/(?:^|; )anthropic-device-id=([^;]+)/)?.[1]
                            || null;
                    } catch {
                        return null;
                    }
                }"""
            )

        snapshot = ClaudeAuthSnapshot(
            cookie=cookie_header,
            user_agent=user_agent,
            cookie_count=len(cookie_map),
            organization_id=organization_id,
            device_id=device_id,
            has_session_key=bool(cookie_map.get("sessionKey")),
        )

        if write:
            cookies_file = Path(cookies_path)
            cookies_file.parent.mkdir(parents=True, exist_ok=True)
            cookies_file.write_text(
                json.dumps(
                    {
                        "cookie": snapshot.cookie,
                        "lastActiveOrg": snapshot.organization_id,
                        "anthropic-device-id": snapshot.device_id,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            headers_file = Path(headers_path)
            headers_file.parent.mkdir(parents=True, exist_ok=True)
            headers: dict[str, Any] = {}
            if headers_file.exists():
                with headers_file.open("r", encoding="utf-8-sig") as f:
                    headers = json.load(f)
            headers.update(
                {
                    "user-agent": snapshot.user_agent,
                    "accept-language": headers.get("accept-language", "zh-CN,zh;q=0.9,en;q=0.8"),
                    "referer": "https://claude.ai/new",
                    "anthropic-client-platform": "web_claude_ai",
                }
            )
            if snapshot.device_id:
                headers["anthropic-device-id"] = snapshot.device_id
            headers_file.write_text(
                json.dumps(headers, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            config_file = Path(config_path)
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config: dict[str, Any] = {}
            if config_file.exists():
                with config_file.open("r", encoding="utf-8-sig") as f:
                    config = json.load(f)
            config.setdefault("api_base", "https://claude.ai")
            config.setdefault("model", "claude-sonnet-4-6")
            config.setdefault("locale", "en-US")
            config.setdefault("timezone", "Asia/Shanghai")
            config.setdefault("enabled_imagine", True)
            config.setdefault("tools", [])
            if snapshot.organization_id:
                config["organization_id"] = snapshot.organization_id
            config_file.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            _log(on_progress, f"[claude] wrote cookies to {cookies_file}")
            _log(on_progress, f"[claude] refreshed headers in {headers_file}")
            _log(on_progress, f"[claude] refreshed config in {config_file}")
        return snapshot
    finally:
        if owns_session:
            await session.close()


async def refresh_all_auth(
    *,
    cdp_port: int = 9222,
    attach_only: bool = False,
    chrome_path: str | None = None,
    user_data_dir: str | Path | None = None,
    write: bool = True,
    timeout_seconds: int = 300,
    wait_for_login: bool = True,
    on_progress: Progress | None = None,
) -> tuple[ChatGPTAuthSnapshot, GeminiAuthSnapshot, ClaudeAuthSnapshot]:
    """Refresh ChatGPT, Gemini, and Claude auth using one CDP browser connection."""

    session = await connect_cdp_browser(
        cdp_port=cdp_port,
        attach_only=attach_only,
        chrome_path=chrome_path,
        user_data_dir=user_data_dir,
        on_progress=on_progress,
    )
    try:
        chatgpt = await refresh_chatgpt_auth(
            session=session,
            write=write,
            timeout_seconds=timeout_seconds,
            wait_for_login=wait_for_login,
            on_progress=on_progress,
        )
        gemini = await refresh_gemini_auth(
            session=session,
            write=write,
            timeout_seconds=timeout_seconds,
            wait_for_login=wait_for_login,
            on_progress=on_progress,
        )
        claude = await refresh_claude_auth(
            session=session,
            write=write,
            timeout_seconds=timeout_seconds,
            wait_for_login=wait_for_login,
            on_progress=on_progress,
        )
        return chatgpt, gemini, claude
    finally:
        await session.close()


async def _run_cli(args: argparse.Namespace) -> None:
    kwargs = {
        "cdp_port": args.cdp_port,
        "attach_only": args.attach_only,
        "chrome_path": args.chrome_path,
        "user_data_dir": args.user_data_dir,
        "write": not args.no_write,
        "timeout_seconds": args.timeout,
        "wait_for_login": not args.no_wait_for_login,
    }

    if args.target == "chatgpt":
        result = await refresh_chatgpt_auth(**kwargs)
        summary = {
            "target": "chatgpt",
            "cookie_count": result.cookie_count,
            "has_access_token": bool(result.access_token),
            "wrote": not args.no_write,
        }
    elif args.target == "gemini":
        result = await refresh_gemini_auth(**kwargs)
        summary = {
            "target": "gemini",
            "cookie_count": result.cookie_count,
            "has___Secure_ENID": result.has_enid,
            "wrote": not args.no_write,
        }
    elif args.target == "claude":
        result = await refresh_claude_auth(**kwargs)
        summary = {
            "target": "claude",
            "cookie_count": result.cookie_count,
            "has_sessionKey": result.has_session_key,
            "has_organization_id": bool(result.organization_id),
            "has_device_id": bool(result.device_id),
            "wrote": not args.no_write,
        }
    else:
        chatgpt, gemini, claude = await refresh_all_auth(**kwargs)
        summary = {
            "target": "all",
            "chatgpt_cookie_count": chatgpt.cookie_count,
            "chatgpt_has_access_token": bool(chatgpt.access_token),
            "gemini_cookie_count": gemini.cookie_count,
            "gemini_has___Secure_ENID": gemini.has_enid,
            "claude_cookie_count": claude.cookie_count,
            "claude_has_sessionKey": claude.has_session_key,
            "claude_has_organization_id": bool(claude.organization_id),
            "claude_has_device_id": bool(claude.device_id),
            "wrote": not args.no_write,
        }

    print(json.dumps(summary, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh ChatGPT/Gemini/Claude reverse auth from Chrome CDP")
    parser.add_argument("--target", choices=["chatgpt", "gemini", "claude", "all"], default="all")
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument("--attach-only", action="store_true")
    parser.add_argument("--chrome-path")
    parser.add_argument("--user-data-dir")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--no-wait-for-login",
        action="store_true",
        help="Read cookies as soon as a valid browser session is detected",
    )
    parser.add_argument("--no-write", action="store_true", help="Only read and validate cookies; do not update files")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(_run_cli(args))


if __name__ == "__main__":
    main()
