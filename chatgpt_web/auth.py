import asyncio
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional, Any
from playwright.async_api import async_playwright

from .types import ChatGPTWebAuth


@dataclass
class LoginResult:
    auth: ChatGPTWebAuth
    browser: Any
    playwright: Any
    browser_process: Any
    chatgpt_page: Any


async def login_chatgpt_web(
    on_progress: Optional[Callable[[str], None]] = None,
    cdp_port: Optional[int] = None,
    attach_only: bool = False,
    user_data_dir: Optional[str] = None
) -> LoginResult:
    """登录 ChatGPT 并获取认证信息

    Args:
        on_progress: 进度回调函数
        cdp_port: Chrome 调试端口，默认 9222
        attach_only: 是否只连接到已运行的 Chrome
        user_data_dir: Chrome 用户数据目录，默认使用项目目录下的 chrome_data

    Returns:
        LoginResult 包含认证信息和浏览器对象
    """
    if on_progress is None:
        def on_progress(message: str) -> None:
            print(message)

    cdp_port = cdp_port or 9222
    cdp_url = f"http://127.0.0.1:{cdp_port}"
    browser_process = None
    external_browser = False

    playwright = await async_playwright().start()

    try:
        browser = None
        try:
            print(f"[ChatGPT] 尝试连接到 Chrome (端口 {cdp_port})...")
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            print("[ChatGPT] 已连接到 Chrome")
            external_browser = True
        except Exception as e:
            if attach_only:
                raise Exception(f"无法连接到 Chrome (端口 {cdp_port})，请先启动 Chrome：\n"
                              f"chrome --remote-debugging-port={cdp_port}") from e

            print("[ChatGPT] 未找到运行的 Chrome，正在启动...")
            from pathlib import Path

            chrome_path = "chrome"
            if sys.platform == "win32":
                paths = [
                    Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
                    "C:/Program Files/Google/Chrome/Application/chrome.exe",
                    "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
                ]
                for p in paths:
                    if Path(p).exists():
                        chrome_path = str(p)
                        break

            if user_data_dir:
                data_dir = user_data_dir
            else:
                data_dir = str(Path.cwd() / "chrome_data")

            print(f"[ChatGPT] 使用 Chrome: {chrome_path}")
            print(f"[ChatGPT] 数据目录: {data_dir}")

            Path(data_dir).mkdir(parents=True, exist_ok=True)

            chrome_args = [
                chrome_path,
                f"--remote-debugging-port={cdp_port}",
                f"--user-data-dir={data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]

            browser_process = subprocess.Popen(chrome_args)

            await asyncio.sleep(2)

            max_attempts = 10
            for i in range(max_attempts):
                try:
                    browser = await playwright.chromium.connect_over_cdp(cdp_url)
                    print("[ChatGPT] Chrome 已启动并连接")
                    break
                except Exception:
                    if i == max_attempts - 1:
                        raise
                    await asyncio.sleep(1)

        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        pages = context.pages
        page = None
        for p in pages:
            if "chatgpt.com" in p.url:
                page = p
                break

        if not page:
            page = pages[0] if pages else await context.new_page()
            on_progress("Opening ChatGPT...")
            await page.goto("https://chatgpt.com", wait_until="domcontentloaded")
        else:
            await page.bring_to_front()

        user_agent = await page.evaluate("() => navigator.userAgent")

        captured_auth: Optional[ChatGPTWebAuth] = None
        handle_request = None
        handle_response = None

        async def try_resolve():
            nonlocal captured_auth
            if captured_auth:
                return True

            try:
                cookies = await context.cookies(["https://chatgpt.com", "https://chat.openai.com"])
                if not cookies:
                    return False

                session_cookie = next((c for c in cookies if c["name"] == "__Secure-next-auth.session-token"), None)

                split_token = ""
                if not session_cookie:
                    token0 = next((c for c in cookies if c["name"] == "__Secure-next-auth.session-token.0"), None)
                    token1 = next((c for c in cookies if c["name"] == "__Secure-next-auth.session-token.1"), None)
                    if token0 and token1:
                        split_token = token0["value"] + token1["value"]

                if session_cookie or split_token:
                    final_token = session_cookie["value"] if session_cookie else split_token

                    if final_token:
                        cookie_string = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

                        captured_auth = ChatGPTWebAuth(
                            access_token=final_token,
                            cookie=cookie_string,
                            user_agent=user_agent
                        )
                        return True
            except Exception as e:
                print(f"[ChatGPT] Failed to fetch cookies: {e}")
            return False

        def remove_listeners():
            if handle_request:
                page.remove_listener("request", handle_request)
            if handle_response:
                page.remove_listener("response", handle_response)

        async def monitor_requests():
            nonlocal handle_request, handle_response

            async def _handle_request(request):
                if "chatgpt.com" in request.url or "openai.com" in request.url:
                    if await try_resolve():
                        remove_listeners()

            async def _handle_response(response):
                url = response.url
                if ("chatgpt.com" in url or "openai.com" in url) and response.ok:
                    if await try_resolve():
                        remove_listeners()

            handle_request = _handle_request
            handle_response = _handle_response
            page.on("request", _handle_request)
            page.on("response", _handle_response)

        # 先立即尝试获取 cookie，如果已经登录了就直接返回
        if await try_resolve():
            print("[ChatGPT] 已找到登录信息，直接使用")
            return LoginResult(
                auth=captured_auth,
                browser=browser,
                playwright=playwright,
                browser_process=browser_process,
                chatgpt_page=page
            )

        on_progress("Please login to ChatGPT in the browser window...")
        login_timeout = 300

        await monitor_requests()

        start_time = asyncio.get_event_loop().time()
        while not captured_auth:
            await asyncio.sleep(2)
            if await try_resolve():
                remove_listeners()
                break

            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > login_timeout:
                raise Exception("Login timed out")

        return LoginResult(
            auth=captured_auth,
            browser=browser,
            playwright=playwright,
            browser_process=browser_process,
            chatgpt_page=page
        )

    except Exception:
        if browser_process and not external_browser:
            try:
                browser_process.terminate()
            except Exception:
                pass
        raise
