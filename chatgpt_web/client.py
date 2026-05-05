import asyncio
import json
import uuid
import subprocess
import sys
import ssl
import websockets

from typing import Optional, AsyncGenerator, Any, Tuple
from pathlib import Path
from playwright.async_api import async_playwright, BrowserContext, Page

from .types import ChatGPTWebClientOptions, ModelDefinitionConfig
from .auth import login_chatgpt_web

try:
    from core.models import CHATGPT_WEB_MODELS, DEFAULT_MODEL, normalize_model
except ImportError:
    CHATGPT_WEB_MODELS = []
    DEFAULT_MODEL = "gpt-5-3"

    def normalize_model(model: str | None) -> str:
        return model or DEFAULT_MODEL


def safe_print(msg: str) -> None:
    """安全打印，避免编码问题"""
    try:
        print(msg.encode("utf-8", errors="backslashreplace").decode("utf-8"))
    except Exception:
        try:
            print(msg)
        except Exception:
            pass


class ChatGPTWebClient:
    def __init__(
        self,
        options: ChatGPTWebClientOptions,
        cdp_port: Optional[int] = None,
        attach_only: bool = False,
        chrome_path: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        browser: Any = None,
        playwright: Any = None,
        browser_process: Any = None,
        chatgpt_page: Any = None,
    ):
        self.access_token = options.access_token
        self.cookie = options.cookie or f"__Secure-next-auth.session-token={options.access_token}"
        self.user_agent = options.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        self.base_url = "https://chatgpt.com"
        self.cdp_port = cdp_port or 9222
        self.attach_only = attach_only
        self.chrome_path = chrome_path
        self.user_data_dir = user_data_dir

        self._browser = browser
        self._playwright = playwright
        self._browser_process = browser_process
        self._chatgpt_page = chatgpt_page
        self.browser: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._stream_binding_installed = False
        self._stream_queues: dict[str, asyncio.Queue] = {}
        self._fetch_created_conversation = False

    async def _get_session_ws_auth(self, page: Page) -> tuple[Optional[str], Optional[str]]:
        try:
            result = await page.evaluate(
                """async () => {
                    const r = await fetch("https://chatgpt.com/api/auth/session", {credentials: "include"});
                    const s = r.ok ? await r.json() : null;

                    const candidates = [
                        s?.user?.id,
                        s?.user?.user_id,
                        s?.user_id,
                        s?.account?.id,
                    ];

                    let userId = null;
                    for (const c of candidates) {
                        if (typeof c === "string" && c) {
                            userId = c.startsWith("user-") ? c : ("user-" + c);
                            break;
                        }
                    }

                    return {
                        userId,
                        accessToken: s?.accessToken || null,
                    };
                }"""
            )
            return result.get("userId"), result.get("accessToken")
        except Exception as e:
            safe_print(f"[ChatGPT Web] Failed to get session ws auth: {e}")
            return None, None

    async def _build_cookie_header_for_ws(self) -> str:
        cookies = []
        if self.browser:
            try:
                cookie_list = await self.browser.cookies(["https://chatgpt.com", "https://ws.chatgpt.com"])
                for c in cookie_list:
                    name = c.get("name")
                    value = c.get("value")
                    if name and value:
                        cookies.append(f"{name}={value}")
            except Exception as e:
                safe_print(f"[ChatGPT Web] Failed to read browser cookies for WS: {e}")

        # 回退：至少带上初始化时已有 cookie
        if not cookies and self.cookie:
            raw = []
            for c in self.cookie.split(";"):
                c = c.strip()
                if c:
                    raw.append(c)
            cookies = raw

        return "; ".join(cookies)

    async def _get_session_user_id(self, page: Page) -> Optional[str]:
        try:
            user_id = await page.evaluate(
                """async () => {
                    const r = await fetch("https://chatgpt.com/api/auth/session", {credentials: "include"});
                    const s = r.ok ? await r.json() : null;
                    const candidates = [
                        s?.user?.id,
                        s?.user?.user_id,
                        s?.user_id,
                        s?.account?.id,
                    ];
                    for (const c of candidates) {
                        if (typeof c === "string" && c) {
                            return c.startsWith("user-") ? c : ("user-" + c);
                        }
                    }
                    return null;
                }"""
            )
            return user_id
        except Exception as e:
            safe_print(f"[ChatGPT Web] Failed to get session user id: {e}")
            return None

    async def _chat_completions_via_handoff_ws_python(
            self,
            page: Page,
            handoff_token: str,
            topic_id: str,
    ) -> AsyncGenerator[Tuple[str, Optional[str], Optional[str]], None]:
        """
        使用 Python 侧 WebSocket 消费 thinking handoff。
        """
        user_id, session_access_token = await self._get_session_ws_auth(page)
        if not user_id:
            safe_print("[ChatGPT Web] Python WS: no session user id")
            return

        access_token = session_access_token or self.access_token
        if not access_token:
            safe_print("[ChatGPT Web] Python WS: no access token")
            return

        ws_url = f"wss://ws.chatgpt.com/p4/ws/user/{user_id}?verify={handoff_token}"
        cookie_header = await self._build_cookie_header_for_ws()

        safe_print(f"[ChatGPT Web] Python WS url: wss://ws.chatgpt.com/p4/ws/user/{user_id}?verify=<redacted>")
        safe_print(f"[ChatGPT Web] Python WS user_id: {user_id}")
        safe_print(
            f"[ChatGPT Web] Python WS auth: bearer={'yes' if access_token else 'no'}, cookies={'yes' if bool(cookie_header) else 'no'}")

        additional_headers = {
            "Authorization": f"Bearer {access_token}",
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
        }
        if cookie_header:
            additional_headers["Cookie"] = cookie_header

        accumulated_content = ""
        current_msg_id: Optional[str] = None
        yielded_ids = False
        found_conversation_id: Optional[str] = None
        found_parent_message_id: Optional[str] = None

        ssl_ctx = ssl.create_default_context()

        try:
            async with websockets.connect(
                    ws_url,
                    origin="https://chatgpt.com",
                    user_agent_header=self.user_agent,
                    additional_headers=additional_headers,
                    open_timeout=20,
                    close_timeout=5,
                    ssl=ssl_ctx,
                    max_size=16 * 1024 * 1024,
            ) as ws:
                safe_print("[ChatGPT Web] Python WS opened")

                sub_msg = json.dumps(
                    [
                        {
                            "id": 1,
                            "command": {
                                "type": "subscribe",
                                "topic_id": topic_id,
                            },
                        }
                    ]
                )
                await ws.send(sub_msg)
                safe_print(f"[ChatGPT Web] Python WS subscribed: {topic_id}")

                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=180)

                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")

                    safe_print(f"[ChatGPT Web] Python WS frame: {raw[:500]}")

                    events = self._collect_conversation_events_from_ws_payload(raw)
                    for event_data in events:
                        if not isinstance(event_data, dict):
                            continue

                        evt_type = event_data.get("type", "")
                        if evt_type in ("resume_conversation_token", "stream_handoff"):
                            if found_conversation_id is None and event_data.get("conversation_id"):
                                found_conversation_id = event_data["conversation_id"]
                            continue

                        if found_conversation_id is None and event_data.get("conversation_id"):
                            found_conversation_id = event_data["conversation_id"]

                        if found_parent_message_id is None and event_data.get("message", {}).get("id"):
                            found_parent_message_id = event_data["message"]["id"]

                        author_role = event_data.get("message", {}).get("author", {}).get("role")
                        if author_role != "assistant":
                            continue

                        msg_id = event_data.get("message", {}).get("id")
                        if msg_id and msg_id != current_msg_id:
                            current_msg_id = msg_id
                            accumulated_content = ""

                        content = self._extract_content(event_data)
                        if isinstance(content, str) and content:
                            delta = content[len(accumulated_content):]
                            if delta:
                                accumulated_content = content
                                if not yielded_ids:
                                    yielded_ids = True
                                    yield delta, found_conversation_id, found_parent_message_id
                                else:
                                    yield delta, None, None

                        if self._is_finish_event(event_data):
                            safe_print("[ChatGPT Web] Python WS finish event detected")
                            return

        except Exception as e:
            safe_print(f"[ChatGPT Web] Python WS failed: {type(e).__name__}: {e}")
            return

    def _collect_conversation_events_from_ws_payload(self, raw: str) -> list[dict]:
        out: list[dict] = []

        def parse_json_maybe(v):
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except Exception:
                    return v
            return v

        def looks_like_event(v):
            if not isinstance(v, dict):
                return False
            if isinstance(v.get("type"), str):
                return True
            if isinstance(v.get("conversation_id"), str):
                return True
            msg = v.get("message")
            if isinstance(msg, dict) and "author" in msg:
                return True
            return False

        def walk(v, depth=0):
            if depth > 8 or v is None:
                return
            v = parse_json_maybe(v)

            if isinstance(v, list):
                for item in v:
                    walk(item, depth + 1)
                return

            if not isinstance(v, dict):
                return

            if looks_like_event(v):
                out.append(v)

            for key in ("event", "payload", "data", "body", "message", "msg"):
                if key in v and v[key] is not v:
                    walk(v[key], depth + 1)

        walk(raw)
        return out

    def _is_finish_event(self, evt: dict) -> bool:
        t = evt.get("type", "")
        if t in (
                "done",
                "complete",
                "message_stream_complete",
                "conversation_turn_complete",
                "conversation_turn_finished",
        ):
            return True

        msg = evt.get("message", {})
        if isinstance(msg, dict):
            if msg.get("status") == "finished_successfully":
                return True
            if msg.get("end_turn") is True:
                return True
            if isinstance(msg.get("metadata"), dict) and msg["metadata"].get("finish_details"):
                return True

        return False

    def _get_chrome_path(self) -> str:
        """获取 Chrome 可执行文件路径"""
        if self.chrome_path:
            return self.chrome_path

        if sys.platform == "win32":
            paths = [
                Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
                "C:/Program Files/Google/Chrome/Application/chrome.exe",
                "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
            ]
            for p in paths:
                if Path(p).exists():
                    return str(p)

        elif sys.platform == "darwin":
            p = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if Path(p).exists():
                return p

        else:
            paths = ["google-chrome", "google-chrome-stable", "chromium"]
            for p in paths:
                try:
                    subprocess.run([p, "--version"], capture_output=True, check=True)
                    return p
                except Exception:
                    continue

        return "chrome"

    def _get_user_data_dir(self) -> str:
        """获取 Chrome 用户数据目录"""
        if self.user_data_dir:
            return self.user_data_dir
        default_dir = Path.cwd() / "chrome_data"
        return str(default_dir)

    async def _is_usable_chatgpt_page(self, page: Page) -> bool:
        if "chatgpt.com" not in page.url:
            return False
        try:
            title = await page.title()
            if "请稍候" in title or "Just a moment" in title:
                return False
            return bool(
                await page.evaluate(
                    """() => {
                        const text = document.body?.innerText || "";
                        if (text.includes("Enable JavaScript and cookies to continue")) return false;
                        return !!document.querySelector(
                            '#prompt-textarea, textarea[placeholder], [data-message-author-role], [data-message-model-slug]'
                        );
                    }"""
                )
            )
        except Exception:
            return False

    async def _select_chatgpt_page(self, pages: list[Page]) -> Optional[Page]:
        candidates = [p for p in pages if "chatgpt.com" in p.url]
        for page in candidates:
            if await self._is_usable_chatgpt_page(page):
                return page
        return candidates[0] if candidates else None

    async def _ensure_browser(self):
        if self.browser and self.page:
            return {"browser": self.browser, "page": self.page}

        if self._browser:
            self.browser = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            pages = self.browser.pages
            if self._chatgpt_page and await self._is_usable_chatgpt_page(self._chatgpt_page):
                self.page = self._chatgpt_page
            else:
                chatgpt_page = await self._select_chatgpt_page(pages)
                if chatgpt_page:
                    self.page = chatgpt_page
                else:
                    self.page = pages[0] if pages else await self.browser.new_page()
                    await self.page.goto(self.base_url, wait_until="load")
            await self._ensure_chatgpt_page_ready()
            return {"browser": self.browser, "page": self.page}

        self._playwright = await async_playwright().start()
        cdp_url = f"http://127.0.0.1:{self.cdp_port}"

        try:
            safe_print(f"[ChatGPT Web] 尝试连接到 Chrome (端口 {self.cdp_port})...")
            browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
            self.browser = browser.contexts[0] if browser.contexts else await browser.new_context()
            safe_print("[ChatGPT Web] 已连接到 Chrome")
        except Exception as e:
            if self.attach_only:
                raise Exception(
                    f"无法连接到 Chrome (端口 {self.cdp_port})，请先启动 Chrome：\n"
                    f"chrome --remote-debugging-port={self.cdp_port}"
                ) from e

            safe_print("[ChatGPT Web] 未找到运行的 Chrome，正在启动...")
            chrome_path = self._get_chrome_path()
            user_data_dir = self._get_user_data_dir()

            safe_print(f"[ChatGPT Web] 使用 Chrome: {chrome_path}")
            safe_print(f"[ChatGPT Web] 数据目录: {user_data_dir}")

            Path(user_data_dir).mkdir(parents=True, exist_ok=True)

            chrome_args = [
                chrome_path,
                f"--remote-debugging-port={self.cdp_port}",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
            ]

            self._browser_process = subprocess.Popen(chrome_args)
            await asyncio.sleep(2)

            max_attempts = 10
            for i in range(max_attempts):
                try:
                    browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
                    self.browser = browser.contexts[0] if browser.contexts else await browser.new_context()
                    safe_print("[ChatGPT Web] Chrome 已启动并连接")
                    break
                except Exception:
                    if i == max_attempts - 1:
                        raise
                    await asyncio.sleep(1)

        pages = self.browser.pages
        chatgpt_page = await self._select_chatgpt_page(pages)

        if chatgpt_page:
            safe_print("[ChatGPT Web] 找到已打开的 ChatGPT 页面")
            self.page = chatgpt_page
        else:
            self.page = pages[0] if pages else await self.browser.new_page()
            await self.page.goto(self.base_url, wait_until="load")

        await self._ensure_chatgpt_page_ready()

        if self.cookie:
            await self._add_cookies()

        return {"browser": self.browser, "page": self.page}

    async def _add_cookies(self):
        cookie_str = self.cookie.strip()
        if cookie_str and not cookie_str.startswith("{"):
            raw_cookies = []
            for c in cookie_str.split(";"):
                parts = c.strip().split("=", 1)
                if len(parts) == 2:
                    name, value = parts
                    raw_cookies.append(
                        {
                            "name": name.strip(),
                            "value": value.strip(),
                            "domain": ".chatgpt.com",
                            "path": "/",
                        }
                    )
            cookies = [c for c in raw_cookies if c["name"]]
            if cookies:
                try:
                    await self.browser.add_cookies(cookies)
                except Exception as e:
                    safe_print(f"[ChatGPT Web] addCookies failed: {e}")

    async def _ensure_chatgpt_page_ready(self):
        if not self.page:
            return
        if "chatgpt.com" not in self.page.url:
            await self.page.goto(self.base_url, wait_until="load")

    @staticmethod
    def _extract_content(msg_data: dict) -> Optional[str]:
        """从 SSE / WS 事件中提取可见回复文本。

        Thinking 模型可能把一次助手回复拆成多个可见文本片段，并在中间夹杂
        thinking / reasoning 片段，例如：
            ["msg_part_1", {"content_type": "thinking", ...}, "msg_part_2"]

        旧逻辑遇到第一个字符串 part 就立即 return，导致 msg_part_2 永远不会
        进入 accumulated_content，流式输出只返回 msg_part_1。这里改为遍历并
        拼接所有可见文本片段，同时跳过思考/推理/上下文类片段。
        """
        non_text_types = {"thinking", "thoughts", "reasoning_recap", "model_editable_context"}

        def part_content_type(value: dict) -> str:
            raw = value.get("content_type") or value.get("type") or value.get("kind") or ""
            return str(raw)

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

            if part_content_type(value) in non_text_types:
                return []

            chunks: list[str] = []

            for key in ("text", "value"):
                item = value.get(key)
                if isinstance(item, str) and item:
                    chunks.append(item)

            nested_content = value.get("content")
            if isinstance(nested_content, str) and nested_content:
                chunks.append(nested_content)
            elif isinstance(nested_content, (dict, list)):
                chunks.extend(extract_visible_text(nested_content))

            if isinstance(value.get("parts"), list):
                chunks.extend(extract_visible_text(value["parts"]))

            return chunks

        message = msg_data.get("message", {})
        content = message.get("content", {})
        if not isinstance(content, dict):
            return None

        content_type = content.get("content_type", "")
        if content_type in non_text_types:
            return None

        parts = content.get("parts", [])
        chunks = extract_visible_text(parts)
        if chunks:
            return "".join(chunks)

        # Some event shapes put text directly on content instead of content.parts.
        chunks = extract_visible_text(content)
        if chunks:
            return "".join(chunks)

        # If no content extracted and content_type is unknown, log for debugging
        if content_type and content_type not in ("text",):
            safe_print(
                f"[ChatGPT Web] Unknown content_type: {content_type}, "
                f"parts: {json.dumps(parts, ensure_ascii=False)[:200]}"
            )

        return None

    _SENTINEL_JS = """
        const baseHeaders = (accessToken, deviceId) => ({
            "Content-Type": "application/json",
            Accept: "text/event-stream",
            "oai-device-id": deviceId,
            "oai-language": "en-US",
            Referer: pageUrl || "https://chatgpt.com/",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            ...(accessToken ? { Authorization: "Bearer " + accessToken } : {}),
        });

        async function warmupSentinel(accessToken, deviceId) {
            const h = baseHeaders(accessToken, deviceId);
            await fetch("https://chatgpt.com/backend-api/conversation/init", {
                method: "POST", headers: h, body: "{}", credentials: "include",
            }).catch(() => {});
            await fetch("https://chatgpt.com/backend-api/sentinel/chat-requirements/prepare", {
                method: "POST", headers: h, body: "{}", credentials: "include",
            }).catch(() => {});
            await fetch("https://chatgpt.com/backend-api/sentinel/chat-requirements/finalize", {
                method: "POST", headers: h, body: "{}", credentials: "include",
            }).catch(() => {});
        }

        async function getSession() {
            const r = await fetch("https://chatgpt.com/api/auth/session", { credentials: "include" });
            return r.ok ? r.json() : null;
        }

        async function tryFetchWithSentinel(accessToken, deviceId) {
            await warmupSentinel(accessToken, deviceId);
            const scripts = Array.from(document.scripts);
            const assetSrc = scripts
                .map((s) => s.src)
                .find((s) => s && s.includes("oaistatic.com") && s.endsWith(".js"));
            const assetUrl = assetSrc || "https://cdn.oaistatic.com/assets/i5bamk05qmvsi6c3.js";
            try {
                const g = await import(assetUrl);
                if (typeof g.bk !== "function" || typeof g.fX !== "function") {
                    return { error: "Sentinel asset missing bk/fX (asset: " + assetUrl + ")" };
                }
                const z = await g.bk();
                const turnstileKey = z && z.turnstile ? (z.turnstile.bx || z.turnstile.dx) : null;
                if (!turnstileKey) {
                    return { error: "Sentinel chat-requirements missing turnstile" };
                }
                const r = await g.bi(turnstileKey);
                let arkose = null;
                try { arkose = await g.bl.getEnforcementToken(z); } catch(e) {}
                let p = null;
                try { p = await g.bm.getEnforcementToken(z); } catch(e) {}
                const extraHeaders = await g.fX(z, arkose, r, p, null);
                const headers = Object.assign({}, baseHeaders(accessToken, deviceId),
                    typeof extraHeaders === "object" ? extraHeaders : {});
                const res = await fetch("https://chatgpt.com/backend-api/conversation", {
                    method: "POST", headers, body: JSON.stringify(body), credentials: "include",
                });
                return { res };
            } catch (e) {
                const msg = e instanceof Error ? e.message : String(e);
                return { error: "Sentinel token failed: " + msg };
            }
        }

        const session = await getSession();
        const accessToken = session && session.accessToken ? session.accessToken : undefined;
        const deviceId = (session && session.oaiDeviceId)
            ? session.oaiDeviceId
            : (globalThis.crypto && globalThis.crypto.randomUUID
                ? globalThis.crypto.randomUUID()
                : Math.random().toString(36).slice(2));

        let sentinelError = undefined;
        let res = await fetch("https://chatgpt.com/backend-api/conversation", {
            method: "POST",
            headers: baseHeaders(accessToken, deviceId),
            body: JSON.stringify(body),
            credentials: "include",
        });

        if (res.status === 403) {
            const sentinelResult = await tryFetchWithSentinel(accessToken, deviceId);
            if (sentinelResult.res) {
                res = sentinelResult.res;
            }
            sentinelError = sentinelResult.error ? sentinelResult.error : undefined;
        }
    """

    _SSE_PARSER_JS = r"""
        function parseSseLines(buffer) {
            const lines = buffer.split("\n");
            const remaining = lines.pop() || "";
            const events = [];

            for (const line of lines) {
                if (!line.startsWith("data: ")) continue;
                const d = line.slice(6).trim();
                if (d === "[DONE]" || !d) continue;

                try {
                    events.push(JSON.parse(d));
                } catch(e) {
                    try {
                        events.push(d);
                    } catch(_) {}
                }
            }

            return { remaining, events };
        }

        function maskToken(token) {
            if (!token) return null;
            return token.slice(0, 24) + "...";
        }

        function parseJsonMaybe(v) {
            if (typeof v !== "string") return v;
            try {
                return JSON.parse(v);
            } catch(e) {
                return v;
            }
        }

        function normalizeUserId(v) {
            if (!v || typeof v !== "string") return null;
            return v.startsWith("user-") ? v : ("user-" + v);
        }

        function getUserIdFromSession(session) {
            const candidates = [
                session?.user?.id,
                session?.user?.user_id,
                session?.user_id,
                session?.account?.id,
            ];

            for (const c of candidates) {
                const id = normalizeUserId(c);
                if (id) return id;
            }
            return null;
        }

        function looksLikeConversationEvent(v) {
            if (!v || typeof v !== "object") return false;
            if (typeof v.type === "string") return true;
            if (typeof v.conversation_id === "string") return true;
            const msg = v.message;
            if (msg && typeof msg === "object" && msg.author) return true;
            return false;
        }

        function collectConversationEvents(input, out = [], depth = 0) {
            if (depth > 8 || input == null) return out;

            const v = parseJsonMaybe(input);

            if (Array.isArray(v)) {
                for (const item of v) {
                    collectConversationEvents(item, out, depth + 1);
                }
                return out;
            }

            if (!v || typeof v !== "object") return out;

            if (looksLikeConversationEvent(v)) {
                out.push(v);
            }

            for (const key of ["event", "payload", "data", "body", "message", "msg"]) {
                if (Object.prototype.hasOwnProperty.call(v, key)) {
                    const nested = v[key];
                    if (nested && nested !== v) {
                        collectConversationEvents(nested, out, depth + 1);
                    }
                }
            }

            return out;
        }

        function isFinishEvent(evt) {
            if (!evt || typeof evt !== "object") return false;

            const t = evt.type || "";
            if (
                t === "done" ||
                t === "complete" ||
                t === "message_stream_complete" ||
                t === "conversation_turn_complete" ||
                t === "conversation_turn_finished"
            ) {
                return true;
            }

            const msg = evt.message;
            if (msg && typeof msg === "object") {
                if (msg.status === "finished_successfully") return true;
                if (msg.end_turn === true) return true;
                if (msg.metadata && msg.metadata.finish_details) return true;
            }

            return false;
        }

        async function consumeSseResponseToStream(res) {
            const reader = res.body ? res.body.getReader() : null;
            if (!reader) {
                throw new Error("No response body");
            }

            const decoder = new TextDecoder();
            let buffer = "";
            let eventCount = 0;

            while (true) {
                const { done, value } = await reader.read();

                if (done) {
                    if (buffer.startsWith("data: ")) {
                        const d = buffer.slice(6).trim();
                        if (d && d !== "[DONE]") {
                            try {
                                const evt = JSON.parse(d);
                                window._cgStream.events.push(evt);
                                eventCount++;
                            } catch(e) {
                                window._cgStream.events.push(d);
                                eventCount++;
                            }
                        }
                    }
                    break;
                }

                buffer += decoder.decode(value, { stream: true });
                const parsed = parseSseLines(buffer);
                buffer = parsed.remaining;

                for (const evt of parsed.events) {
                    window._cgStream.events.push(evt);
                    eventCount++;

                    if (eventCount <= 5) {
                        if (typeof evt === "string") {
                            window._cgStream.rawFirstEvents.push(JSON.stringify(evt).slice(0, 300));
                        } else {
                            window._cgStream.rawFirstEvents.push(JSON.stringify(evt).slice(0, 300));
                        }
                    }
                }
            }

            return eventCount;
        }

        async function subscribeHandoffWs({ session, token, topicId, timeoutMs = 300000 }) {
            // Step 1: Get WS URL from /backend-api/celsius/ws/user
            // (Rosetta reverse-engineered: the WS URL is obtained via this API,
            //  not manually constructed with the resume_conversation_token)
            let wsUrl = null;
            try {
                const wsResp = await fetch("https://chatgpt.com/backend-api/celsius/ws/user", {
                    headers: {
                        "Authorization": "Bearer " + (session.accessToken || ""),
                        "Content-Type": "application/json",
                    },
                    credentials: "include",
                });
                if (wsResp.ok) {
                    const wsData = await wsResp.json();
                    wsUrl = wsData.websocket_url || null;
                } else {
                    window._cgStream.wsDebug = {
                        celsiusStatus: wsResp.status,
                        error: "celsius/ws/user failed",
                    };
                }
            } catch(e) {
                window._cgStream.wsDebug = {
                    celsiusError: e instanceof Error ? e.message : String(e),
                };
            }

            if (!wsUrl) {
                throw new Error("Failed to get WS URL from /backend-api/celsius/ws/user");
            }

            window._cgStream.wsDebug = {
                wsUrlHost: new URL(wsUrl).host,
                topicId: topicId || null,
                opened: false,
                connected: false,
                subscribed: false,
                closed: false,
                closeCode: null,
                closeReason: null,
                errored: false,
                messageCount: 0,
                frameCount: 0,
                streamItemCount: 0,
            };

            window._cgStream.rawWsEvents = window._cgStream.rawWsEvents || [];

            // Step 2: Open WebSocket and follow the correct protocol:
            //   a) connect → wait for connect reply
            //   b) subscribe → wait for subscribe reply (with offset: "0")
            //   c) receive stream-item frames → parse encoded_item as SSE
            return await new Promise((resolve, reject) => {
                let ws = null;
                let settled = false;
                let eventCount = 0;
                let idleTimer = null;
                let hardTimer = null;
                let finishDelayTimer = null;

                const cleanup = () => {
                    if (idleTimer) clearTimeout(idleTimer);
                    if (hardTimer) clearTimeout(hardTimer);
                    if (finishDelayTimer) clearTimeout(finishDelayTimer);
                };

                const finish = (reason) => {
                    if (settled) return;
                    settled = true;
                    cleanup();

                    try {
                        if (ws && ws.readyState === WebSocket.OPEN) {
                            ws.send(JSON.stringify([{
                                id: 3,
                                command: { type: "unsubscribe", topic_id: topicId },
                            }]));
                        }
                    } catch(e) {}

                    try {
                        if (ws) ws.close();
                    } catch(e) {}

                    resolve({
                        reason,
                        eventCount,
                        debug: window._cgStream.wsDebug,
                    });
                };

                const scheduleFinish = (reason) => {
                    if (settled) return;
                    if (finishDelayTimer) clearTimeout(finishDelayTimer);
                    finishDelayTimer = setTimeout(() => {
                        finish(reason);
                    }, 600);
                };

                const fail = (err) => {
                    if (settled) return;
                    settled = true;
                    cleanup();

                    try {
                        if (ws) ws.close();
                    } catch(e) {}

                    reject(err);
                };

                const resetIdle = () => {
                    if (idleTimer) clearTimeout(idleTimer);
                    idleTimer = setTimeout(() => {
                        finish("idle_timeout");
                    }, 90000);  // 90s idle timeout (server sends heartbeats every few seconds)
                };

                hardTimer = setTimeout(() => {
                    finish("hard_timeout");
                }, timeoutMs);

                try {
                    ws = new WebSocket(wsUrl);
                    ws.binaryType = "arraybuffer";
                } catch(e) {
                    fail(e);
                    return;
                }

                ws.onerror = () => {
                    window._cgStream.wsDebug.errored = true;
                    fail(new Error("Handoff WebSocket connection error"));
                };

                ws.onclose = (ev) => {
                    window._cgStream.wsDebug.closed = true;
                    window._cgStream.wsDebug.closeCode = ev.code;
                    window._cgStream.wsDebug.closeReason = ev.reason || "";
                    finish("ws_close");
                };

                ws.onopen = () => {
                    window._cgStream.wsDebug.opened = true;

                    // Send connect command
                    try {
                        ws.send(JSON.stringify([{ id: 0, command: { type: "connect" } }]));
                    } catch(e) {
                        fail(e);
                        return;
                    }
                };

                ws.onmessage = async (ev) => {
                    let raw = "";
                    if (typeof ev.data === "string") {
                        raw = ev.data;
                    } else if (ev.data instanceof ArrayBuffer) {
                        raw = new TextDecoder().decode(new Uint8Array(ev.data));
                    } else if (typeof Blob !== "undefined" && ev.data instanceof Blob) {
                        raw = await ev.data.text();
                    } else {
                        raw = String(ev.data || "");
                    }

                    window._cgStream.wsDebug.frameCount += 1;
                    // Reset idle timer on ANY frame (including heartbeats)
                    resetIdle();

                    if (window._cgStream.rawWsEvents.length < 20) {
                        window._cgStream.rawWsEvents.push(raw.slice(0, 800));
                    }

                    let parsed;
                    try {
                        parsed = JSON.parse(raw);
                    } catch(e) {
                        return;
                    }

                    const items = Array.isArray(parsed) ? parsed : [parsed];
                    let shouldFinishLive = false;
                    for (const item of items) {
                        if (!item || typeof item !== "object") continue;

                        // Handle connect reply
                        if (item.type === "reply" && item.reply && item.reply.type === "connect") {
                            window._cgStream.wsDebug.connected = true;
                            // Now subscribe to the handoff topic
                            try {
                                ws.send(JSON.stringify([{
                                    id: 1,
                                    command: {
                                        type: "subscribe",
                                        topic_id: topicId,
                                        offset: "0",
                                    },
                                }]));
                            } catch(e) {
                                fail(e);
                                return;
                            }
                            continue;
                        }

                        // Handle subscribe reply
                        if (item.type === "reply" && item.reply && item.reply.type === "subscribe") {
                            window._cgStream.wsDebug.subscribed = true;
                            // Process catchup messages in the reply.
                            // skipFinish=true: don't finish mid-loop.
                            // finish is only called when end_turn=true AND
                            // hasStreamTextContent() — so we won't close prematurely
                            // if the assistant message has empty parts.
                            let shouldFinish = false;
                            if (Array.isArray(item.reply.catchups)) {
                                for (const catchup of item.reply.catchups) {
                                    if (processWsMessage(catchup, true)) {
                                        shouldFinish = true;
                                    }
                                }
                            }
                            if (shouldFinish) {
                                scheduleFinish("finish_event");
                                return;
                            }
                            continue;
                        }

                        // Handle live messages
                        if (item.type === "message") {
                            if (processWsMessage(item, true)) {
                                shouldFinishLive = true;
                            }
                            continue;
                        }
                    }
                    if (shouldFinishLive) {
                        // Delay finish to allow any queued frames with more
                        // complete text to arrive (thinking models may send
                        // partial text first, then complete text in a later frame).
                        scheduleFinish("finish_event");
                    }
                };

                function parseEncodedItemAsSse(enc) {
                    // encoded_item is SSE-formatted text, e.g.:
                    //   event: delta_encoding\ndata: "v1"\n\n
                    //   event: delta\ndata: {"p": "...", "o": "append", "v": "..."}\n\n
                    //   data: {"type": "message_marker", ...}\n\n
                    // We need to extract data: lines, parse JSON, and produce events.
                    const events = [];
                    const lines = enc.split("\n");
                    for (const line of lines) {
                        if (!line.startsWith("data: ")) continue;
                        const d = line.slice(6).trim();
                        if (!d || d === "[DONE]") continue;
                        try {
                            const parsed = JSON.parse(d);
                            events.push(parsed);
                        } catch(e) {
                            // Non-JSON data line (e.g. "v1"), treat as raw event
                            events.push(d);
                        }
                    }
                    return events;
                }

                // --- Delta encoding message accumulator ---
                // The celsius WS sends events in delta_encoding format (JSON Patch style).
                // We need to accumulate message state and push events in the format
                // Python expects: {message: {...}, conversation_id: "..."}
                const msgAccum = {};   // id -> message object (deep copy)
                let currentStreamMsgId = null;  // id of the assistant text message being streamed
                let pushCount = 0;  // debug: count of pushed events

                function pushMsgEvent(m, convId) {
                    window._cgStream.events.push({
                        message: JSON.parse(JSON.stringify(m)),
                        conversation_id: convId || null,
                    });
                    eventCount++;
                    pushCount++;
                    // Debug: log first 8 events
                    if (pushCount <= 8) {
                        const role = m.author?.role || "?";
                        const ct = m.content?.content_type || "?";
                        const parts0 = (m.content?.parts?.[0] || "");
                        const textPreview = typeof parts0 === "string" ? parts0.slice(0, 80) : JSON.stringify(parts0).slice(0, 80);
                        const et = m.end_turn === true ? " end_turn" : "";
                        window._cgStream.rawWsEvents.push(
                            `[push#${pushCount}] role=${role} ct=${ct} status=${m.status}${et} parts0="${textPreview}"`
                        );
                    }
                }

                function hasStreamTextContent() {
                    // Check if we have accumulated any actual text content in the
                    // current streaming message. For thinking models, the assistant
                    // message arrives with empty parts — text comes via append deltas.
                    // We must NOT finish until the text has actually arrived.
                    if (!currentStreamMsgId) return false;
                    const m = msgAccum[currentStreamMsgId];
                    if (!m) return false;
                    const parts = m.content?.parts || [];
                    return parts.some(p =>
                        (typeof p === "string" && p.length > 0) ||
                        (typeof p === "object" && p && (p.text || p.value || p.content))
                    );
                }

                function stripMessagePrefix(pointer) {
                    // Delta encoding JSON Pointers are relative to {message: {...}},
                    // e.g. "/message/content/parts/0", "/message/status".
                    // But msgAccum stores the message object directly (no wrapper).
                    // Strip the "/message" prefix so pointers work on the raw message object.
                    if (pointer.startsWith("/message/")) {
                        return pointer.slice("/message".length);  // "/content/parts/0"
                    }
                    if (pointer === "/message") {
                        return "";
                    }
                    return pointer;
                }

                function applyJsonPointerPatch(obj, pointer, value) {
                    // Very minimal JSON Pointer support for /content/parts/N etc.
                    const effectivePointer = stripMessagePrefix(pointer);
                    if (!effectivePointer) return;  // can't replace the whole message
                    const segments = effectivePointer.split("/").filter(Boolean);
                    let cur = obj;
                    for (let i = 0; i < segments.length - 1; i++) {
                        const key = segments[i];
                        if (cur == null || typeof cur !== "object") return;
                        cur = Array.isArray(cur) ? cur[parseInt(key)] : cur[key];
                    }
                    if (cur == null || typeof cur !== "object") return;
                    const lastKey = segments[segments.length - 1];
                    const idx = parseInt(lastKey);
                    if (Array.isArray(cur) && !isNaN(idx) && idx >= 0) {
                        cur[idx] = value;
                    } else if (typeof cur === "object" && !Array.isArray(cur)) {
                        cur[lastKey] = value;
                    }
                }

                function applyJsonPointerAppend(obj, pointer, value) {
                    const effectivePointer = stripMessagePrefix(pointer);
                    if (!effectivePointer) return;
                    const segments = effectivePointer.split("/").filter(Boolean);
                    let cur = obj;
                    for (let i = 0; i < segments.length - 1; i++) {
                        const key = segments[i];
                        if (cur == null || typeof cur !== "object") return;
                        cur = Array.isArray(cur) ? cur[parseInt(key)] : cur[key];
                    }
                    if (cur == null || typeof cur !== "object") return;
                    const lastKey = segments[segments.length - 1];
                    const idx = parseInt(lastKey);
                    if (Array.isArray(cur) && !isNaN(idx) && idx >= 0) {
                        // If index equals length, we need to push (extend the array)
                        if (idx === cur.length) {
                            if (typeof value === "string") {
                                cur.push(value);
                            } else {
                                cur.push(value);
                            }
                        } else if (idx < cur.length) {
                            if (typeof cur[idx] === "string" && typeof value === "string") {
                                cur[idx] += value;
                            }
                        }
                    }
                }

                function processWsMessage(msg, skipFinish) {
                    if (!msg || msg.topic_id !== topicId) return false;
                    if (finishDelayTimer) {
                        clearTimeout(finishDelayTimer);
                        finishDelayTimer = null;
                    }

                    const inner = msg.payload && msg.payload.payload;
                    if (!inner) return false;

                    // Skip heartbeats
                    if (inner.type === "heartbeat") return false;

                    const enc = inner.encoded_item;
                    if (!enc) return false;

                    window._cgStream.wsDebug.streamItemCount += 1;

                    const convId = inner.conversation_id || null;
                    const sseParsed = parseEncodedItemAsSse(enc);

                    for (const dataVal of sseParsed) {
                        if (dataVal == null) continue;
                        // String values (like "v1" from delta_encoding) — skip
                        if (typeof dataVal !== "object") continue;

                        // --- o: "add" → new message created ---
                        if (dataVal.o === "add" && dataVal.v && dataVal.v.message) {
                            const m = dataVal.v.message;
                            msgAccum[m.id] = JSON.parse(JSON.stringify(m));

                            // Track the streaming assistant text message
                            const ct = m.content?.content_type || "";
                            const nonTextTypes = ["thinking", "thoughts", "reasoning_recap", "model_editable_context"];
                            if (m.author?.role === "assistant" && m.status === "in_progress" && !nonTextTypes.includes(ct)) {
                                currentStreamMsgId = m.id;
                                if (window._cgStream.rawWsEvents.length < 30) {
                                    window._cgStream.rawWsEvents.push(
                                        `[add] Set currentStreamMsgId=${m.id.slice(0,8)} ct=${ct}`
                                    );
                                }
                            }

                            pushMsgEvent(m, convId);

                            // Only finish if the message has reached terminal status
                            // (finished_successfully) AND we have text content.
                            // For thinking models, the backend may send end_turn=true
                            // with partial text first, then update with complete text.
                            // We must NOT finish until the message is truly complete.
                            if (m.author?.role === "assistant" && m.status === "finished_successfully") {
                                if (hasStreamTextContent()) {
                                    if (window._cgStream.rawWsEvents.length < 30) {
                                        window._cgStream.rawWsEvents.push(
                                            `[add] finished, finishing. role=${m.author?.role} ct=${m.content?.content_type}`
                                        );
                                    }
                                    if (skipFinish) return true;
                                    scheduleFinish("finish_event");
                                    return true;
                                } else if (window._cgStream.rawWsEvents.length < 30) {
                                    window._cgStream.rawWsEvents.push(
                                        `[add] finished but no text content yet, waiting`
                                    );
                                }
                            }
                            continue;
                        }

                        // --- v.message without "o" field (catchup format) ---
                        // Some catchup messages arrive as {v: {message: {...}}, c: ...}
                        // without an "o" field — treat as equivalent to o:"add"
                        if (!dataVal.o && dataVal.v && dataVal.v.message) {
                            const m = dataVal.v.message;
                            msgAccum[m.id] = JSON.parse(JSON.stringify(m));

                            const ct = m.content?.content_type || "";
                            const nonTextTypes = ["thinking", "thoughts", "reasoning_recap", "model_editable_context"];
                            // For catchup messages, don't require status === "in_progress"
                            // because catchup messages can arrive in any state (including
                            // "finished_successfully"). We need to set currentStreamMsgId
                            // so that subsequent append deltas can deliver the text content.
                            if (m.author?.role === "assistant" && !nonTextTypes.includes(ct)) {
                                currentStreamMsgId = m.id;
                                if (window._cgStream.rawWsEvents.length < 30) {
                                    window._cgStream.rawWsEvents.push(
                                        `[add-fb] Set currentStreamMsgId=${m.id.slice(0,8)} ct=${ct} status=${m.status}`
                                    );
                                }
                            }

                            pushMsgEvent(m, convId);

                            if (m.author?.role === "assistant" && m.status === "finished_successfully") {
                                if (hasStreamTextContent()) {
                                    if (window._cgStream.rawWsEvents.length < 30) {
                                        window._cgStream.rawWsEvents.push(
                                            `[add-fb] finished, finishing. role=${m.author?.role} ct=${m.content?.content_type}`
                                        );
                                    }
                                    if (skipFinish) return true;
                                    scheduleFinish("finish_event");
                                    return true;
                                } else if (window._cgStream.rawWsEvents.length < 30) {
                                    window._cgStream.rawWsEvents.push(
                                        `[add-fb] finished but no text content yet, waiting`
                                    );
                                }
                            }
                            continue;
                        }

                        // --- o: "append" → append value to a JSON Pointer path ---
                        if (dataVal.o === "append" && dataVal.p && dataVal.v !== undefined) {
                            // Apply to current streaming assistant message
                            const target = currentStreamMsgId ? msgAccum[currentStreamMsgId] : null;
                            if (target) {
                                applyJsonPointerAppend(target, dataVal.p, dataVal.v);
                                pushMsgEvent(target, convId);
                                // Finish if the message is already terminal and has text
                                if (target.author?.role === "assistant" && target.status === "finished_successfully" && hasStreamTextContent()) {
                                    if (window._cgStream.rawWsEvents.length < 30) {
                                        window._cgStream.rawWsEvents.push(
                                            `[append] finished, finishing`
                                        );
                                    }
                                    if (skipFinish) return true;
                                    scheduleFinish("finish_event");
                                    return true;
                                }
                            } else if (window._cgStream.rawWsEvents.length < 30) {
                                window._cgStream.rawWsEvents.push(
                                    `[append] SKIP: no currentStreamMsgId, p=${dataVal.p} v=${JSON.stringify(dataVal.v).slice(0,40)}`
                                );
                            }
                            continue;
                        }

                        // --- o: "patch" → array of JSON Patch operations ---
                        if (dataVal.o === "patch" && Array.isArray(dataVal.v)) {
                            const target = currentStreamMsgId ? msgAccum[currentStreamMsgId] : null;
                            if (target) {
                                for (const patch of dataVal.v) {
                                    if (patch.o === "replace" || patch.o === "add") {
                                        applyJsonPointerPatch(target, patch.p, patch.v);
                                    } else if (patch.o === "append") {
                                        applyJsonPointerAppend(target, patch.p, patch.v);
                                    }
                                }
                                pushMsgEvent(target, convId);
                                if (target.author?.role === "assistant" && target.status === "finished_successfully" && hasStreamTextContent()) {
                                    if (window._cgStream.rawWsEvents.length < 30) {
                                        window._cgStream.rawWsEvents.push(
                                            `[patch] finished, finishing`
                                        );
                                    }
                                    if (skipFinish) return true;
                                    scheduleFinish("finish_event");
                                    return true;
                                }
                            } else if (window._cgStream.rawWsEvents.length < 30) {
                                window._cgStream.rawWsEvents.push(
                                    `[patch] SKIP: no currentStreamMsgId, ops=${dataVal.v.length}`
                                );
                            }
                            continue;
                        }

                        // --- o: "replace" → single replace ---
                        if (dataVal.o === "replace" && dataVal.p && dataVal.v !== undefined) {
                            const target = currentStreamMsgId ? msgAccum[currentStreamMsgId] : null;
                            if (target) {
                                applyJsonPointerPatch(target, dataVal.p, dataVal.v);
                                pushMsgEvent(target, convId);
                                if (target.author?.role === "assistant" && target.status === "finished_successfully" && hasStreamTextContent()) {
                                    if (window._cgStream.rawWsEvents.length < 30) {
                                        window._cgStream.rawWsEvents.push(
                                            `[replace] finished, finishing`
                                        );
                                    }
                                    if (skipFinish) return true;
                                    scheduleFinish("finish_event");
                                    return true;
                                }
                            } else if (window._cgStream.rawWsEvents.length < 30) {
                                window._cgStream.rawWsEvents.push(
                                    `[replace] SKIP: no currentStreamMsgId, p=${dataVal.p} v=${JSON.stringify(dataVal.v).slice(0,40)}`
                                );
                            }
                            continue;
                        }

                        // --- Bare text delta: {v: "text_chunk"} without o/p fields ---
                        // Some delta encodings send text appends as just the value
                        // without operation metadata. Treat as append to current
                        // streaming message's parts[0].
                        if (typeof dataVal.v === "string" && dataVal.v.length > 0 && !dataVal.o) {
                            const target = currentStreamMsgId ? msgAccum[currentStreamMsgId] : null;
                            if (target) {
                                if (target.content && Array.isArray(target.content.parts)) {
                                    if (target.content.parts.length === 0) {
                                        target.content.parts.push(dataVal.v);
                                    } else if (typeof target.content.parts[0] === "string") {
                                        target.content.parts[0] += dataVal.v;
                                    } else {
                                        target.content.parts[0] = dataVal.v;
                                    }
                                }
                                pushMsgEvent(target, convId);
                                if (target.author?.role === "assistant" && target.status === "finished_successfully" && hasStreamTextContent()) {
                                    if (window._cgStream.rawWsEvents.length < 30) {
                                        window._cgStream.rawWsEvents.push(
                                            `[text-delta] finished, finishing`
                                        );
                                    }
                                    if (skipFinish) return true;
                                    scheduleFinish("finish_event");
                                    return true;
                                }
                            } else if (window._cgStream.rawWsEvents.length < 30) {
                                window._cgStream.rawWsEvents.push(
                                    `[text-delta] SKIP: no currentStreamMsgId, v=${JSON.stringify(dataVal.v).slice(0,40)}`
                                );
                            }
                            continue;
                        }

                        // --- Other typed events (message_marker, title_generation, etc.) ---
                        if (dataVal.type) {
                            // Push metadata events for conversation_id extraction
                            window._cgStream.events.push(dataVal);
                            eventCount++;

                            if (dataVal.type === "conversation_turn_complete" ||
                                dataVal.type === "conversation_turn_finished") {
                                if (window._cgStream.rawWsEvents.length < 30) {
                                    window._cgStream.rawWsEvents.push(
                                        `[type] ${dataVal.type}, finishing`
                                    );
                                }
                                if (skipFinish) return true;
                                scheduleFinish("finish_event");
                                return true;
                            }
                            continue;
                        }

                        // Unknown delta format — log for debugging
                        if (window._cgStream.rawWsEvents.length < 30) {
                            const keys = Object.keys(dataVal).join(",");
                            window._cgStream.rawWsEvents.push(
                                `[unknown] keys=${keys} json=${JSON.stringify(dataVal).slice(0,120)}`
                            );
                        }
                    }
                    return false;
                }
            });
        }
    """

    async def _chat_completions_via_stream(
        self,
        page: Page,
        body: dict[str, Any],
        page_url: str,
        model: str,
    ) -> AsyncGenerator[Tuple[str, Optional[str], Optional[str]], None]:
        """真正的流式：JS 读取 SSE / WS 并解析事件写入 window._cgStream，Python 轮询获取。"""
        try:
            await page.evaluate(
                """async ({ body, pageUrl }) => {
                    window._cgStream = {
                        events: [],
                        done: false,
                        error: null,
                        status: 0,
                        sentinelError: null,
                        rawFirstEvents: [],
                        rawWsEvents: [],
                        handoff: null,
                        wsDebug: null,
                        wsUrl: null,
                    };

                    %s

                    (async () => {
                        try {
                            %s

                            if (!res.ok) {
                                const errorText = await res.text();
                                window._cgStream.status = res.status;
                                window._cgStream.sentinelError = sentinelError;
                                window._cgStream.error = errorText;
                                window._cgStream.done = true;
                                return;
                            }

                            await consumeSseResponseToStream(res);

                            const handoffEvt = window._cgStream.events.find(
                                e => e && typeof e === "object" && e.type === "stream_handoff"
                            );

                            if (handoffEvt) {
                                const resumeTokenEvt = window._cgStream.events.find(
                                    e => e && typeof e === "object" && e.type === "resume_conversation_token"
                                );

                                const options = handoffEvt.options || [];
                                const wsOption = options.find(o => o.type === "subscribe_ws_topic");

                                const topicId = wsOption ? wsOption.topic_id : null;

                                window._cgStream.handoff = {
                                    token: null,
                                    topicId,
                                    hasWsOption: !!wsOption,
                                    hasSseOption: !!options.find(o => o.type === "resume_sse_endpoint"),
                                    conversationId: handoffEvt.conversation_id,
                                    transport: null,
                                };

                                // 使用 celsius WS 连接（从 /backend-api/celsius/ws/user 获取 URL）
                                // 协议：connect → subscribe(topic_id, offset:"0") → 接收 stream-item
                                if (topicId) {
                                    window._cgStream.handoff.transport = "celsius_ws";
                                    try {
                                        const wsResult = await subscribeHandoffWs({
                                            session,
                                            token: null,
                                            topicId,
                                            timeoutMs: 300000,
                                        });
                                        window._cgStream.handoff.wsResult = {
                                            reason: wsResult.reason,
                                            eventCount: wsResult.eventCount,
                                        };
                                    } catch(e) {
                                        const wsErr = e instanceof Error ? e.message : String(e);
                                        window._cgStream.handoff.wsError = wsErr;
                                    }
                                }
                            }
                        } catch(e) {
                            window._cgStream.error = e instanceof Error ? e.message : String(e);
                        }
                        window._cgStream.done = true;
                    })();
                    return "streaming";
                }"""
                % (self._SSE_PARSER_JS, self._SENTINEL_JS),
                {"body": body, "pageUrl": page_url},
            )
        except Exception as e:
            safe_print(f"[ChatGPT Web] Stream start failed: {e}")
            return

        last_index = 0
        accumulated_content = ""
        current_msg_id: Optional[str] = None
        yielded_ids = False
        found_conversation_id: Optional[str] = None
        found_parent_message_id: Optional[str] = None
        poll_interval = 0.05
        idle_rounds = 0
        max_idle_rounds = 2400  # 120s default
        max_idle_rounds_with_handoff = 6000  # 300s when handoff detected (thinking models)
        handoff_logged = False

        while True:
            try:
                result = await page.evaluate(
                    """({ lastIndex }) => {
                        const s = window._cgStream;
                        const newEvents = s.events.slice(lastIndex);
                        return {
                            events: newEvents,
                            done: s.done,
                            error: s.error,
                            total: s.events.length,
                            status: s.status,
                            sentinelError: s.sentinelError,
                            rawFirstEvents: s.rawFirstEvents,
                            rawWsEvents: s.rawWsEvents,
                            handoff: s.handoff,
                            wsDebug: s.wsDebug,
                            wsUrl: s.wsUrl,
                        };
                    }""",
                    {"lastIndex": last_index},
                )
            except Exception as e:
                safe_print(f"[ChatGPT Web] Stream poll error: {e}")
                break
            handoff_info = result.get("handoff")
            if handoff_info and not handoff_logged:
                handoff_logged = True
                safe_print(f"[ChatGPT Web] Handoff detected: {handoff_info}")

            new_events = result.get("events", [])
            has_new = len(new_events) > 0
            for event_data in new_events:

                if isinstance(event_data, str):
                    continue
                if not event_data or not isinstance(event_data, dict):
                    continue

                evt_type = event_data.get("type", "")
                if evt_type in ("resume_conversation_token", "stream_handoff"):
                    if found_conversation_id is None and event_data.get("conversation_id"):
                        found_conversation_id = event_data["conversation_id"]
                        safe_print(f"[ChatGPT Web] conversation_id from handoff: {found_conversation_id}")
                    continue

                if found_conversation_id is None:
                    if event_data.get("conversation_id"):
                        found_conversation_id = event_data["conversation_id"]
                        safe_print(f"[ChatGPT Web] conversation_id: {found_conversation_id}")
                    if event_data.get("message", {}).get("id"):
                        found_parent_message_id = event_data["message"]["id"]

                author_role = event_data.get("message", {}).get("author", {}).get("role")
                if author_role != "assistant":
                    continue

                msg_id = event_data.get("message", {}).get("id")
                if msg_id and msg_id != current_msg_id:
                    current_msg_id = msg_id
                    accumulated_content = ""

                content = self._extract_content(event_data)
                if isinstance(content, str) and content:
                    delta = content[len(accumulated_content):]
                    if delta:
                        accumulated_content = content
                        if not yielded_ids:
                            yielded_ids = True
                            yield delta, found_conversation_id, found_parent_message_id
                        else:
                            yield delta, None, None

            last_index = result.get("total", last_index)

            if result.get("error"):
                error = result["error"]
                status = result.get("status", 0)
                sentinel_error = result.get("sentinelError", "")
                raw_first = result.get("rawFirstEvents", [])
                raw_ws = result.get("rawWsEvents", [])
                ws_debug = result.get("wsDebug")
                ws_url = result.get("wsUrl")

                if raw_first:
                    safe_print(f"[ChatGPT Web] Stream first events: {raw_first}")
                if raw_ws:
                    safe_print(f"[ChatGPT Web] WS first events: {raw_ws}")
                if ws_debug:
                    safe_print(f"[ChatGPT Web] WS debug: {ws_debug}")
                if ws_url:
                    safe_print(f"[ChatGPT Web] WS url: {ws_url}")

                if status == 403:
                    raise Exception(f"403:{sentinel_error}")
                if status == 401:
                    raise Exception("ChatGPT authentication failed")
                raise Exception(f"ChatGPT API error {status}: {error[:500]}")

            if result.get("done"):
                raw_first = result.get("rawFirstEvents", [])
                raw_ws = result.get("rawWsEvents", [])
                ws_debug = result.get("wsDebug")
                ws_url = result.get("wsUrl")
                final_handoff = result.get("handoff")

                if final_handoff:
                    safe_print(f"[ChatGPT Web] Final handoff state: {final_handoff}")
                if ws_debug:
                    safe_print(f"[ChatGPT Web] WS debug: {ws_debug}")
                if ws_url:
                    safe_print(f"[ChatGPT Web] WS url: {ws_url}")

                if not accumulated_content:
                    if raw_first:
                        safe_print(f"[ChatGPT Web] No content from SSE. First events: {raw_first}")
                    else:
                        safe_print(f"[ChatGPT Web] No content from SSE (total events: {last_index})")

                    if raw_ws:
                        safe_print(f"[ChatGPT Web] WS first events: {raw_ws}")

                if found_conversation_id:
                    self._fetch_created_conversation = True
                break

            if has_new:
                idle_rounds = 0
            else:
                idle_rounds += 1
                current_max = max_idle_rounds_with_handoff if handoff_logged else max_idle_rounds
                if idle_rounds >= current_max:
                    timeout_s = int(current_max * poll_interval)
                    safe_print(f"[ChatGPT Web] Stream idle timeout ({timeout_s}s with no events)")
                    if found_conversation_id:
                        self._fetch_created_conversation = True
                    break

            await asyncio.sleep(poll_interval)

    async def _chat_completions_via_fetch(
        self,
        page: Page,
        body: dict[str, Any],
        page_url: str,
    ) -> dict[str, Any]:
        """非流式：在浏览器中执行 fetch，获取完整 SSE 文本后返回。"""
        try:
            response_data = await asyncio.wait_for(
                page.evaluate(
                    """async ({ body, pageUrl }) => {
                        %s

                        if (!res.ok) {
                            const errorText = await res.text();
                            return { ok: false, status: res.status, error: errorText, sentinelError };
                        }

                        const reader = res.body ? res.body.getReader() : null;
                        if (!reader) {
                            return { ok: false, status: 500, error: "No response body", sentinelError };
                        }

                        const decoder = new TextDecoder();
                        let fullText = "";
                        while (true) {
                            const result = await reader.read();
                            if (result.done) break;
                            fullText += decoder.decode(result.value, { stream: true });
                        }
                        return { ok: true, data: fullText };
                    }"""
                    % self._SENTINEL_JS,
                    {"body": body, "pageUrl": page_url},
                ),
                timeout=120,
            )
        except asyncio.TimeoutError:
            raise Exception("Fetch request timed out after 120s")

        return response_data

    def _parse_sse_response(self, data: str) -> AsyncGenerator[Tuple[str, Optional[str], Optional[str]], None]:
        """将完整的 SSE 文本解析为 (delta, conversation_id, parent_message_id) 的生成器。"""
        accumulated_content = ""
        current_msg_id: Optional[str] = None
        yielded_ids = False
        found_conversation_id: Optional[str] = None
        found_parent_message_id: Optional[str] = None
        event_count = 0

        for line in data.split("\n"):
            if not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]" or not data_str:
                continue

            try:
                msg_data = json.loads(data_str)
            except json.JSONDecodeError:
                if event_count < 3:
                    safe_print(f"[ChatGPT Web] Non-JSON SSE data: {data_str[:200]}")
                continue

            if not msg_data or not isinstance(msg_data, dict):
                continue

            evt_type = msg_data.get("type", "")
            if evt_type in ("resume_conversation_token", "stream_handoff"):
                event_count += 1
                if event_count <= 3:
                    safe_print(f"[ChatGPT Web] SSE event #{event_count}: type={evt_type}")
                if found_conversation_id is None and msg_data.get("conversation_id"):
                    found_conversation_id = msg_data["conversation_id"]
                    safe_print(f"[ChatGPT Web] conversation_id from handoff: {found_conversation_id}")
                continue

            event_count += 1

            if event_count <= 2:
                ct = msg_data.get("message", {}).get("content", {}).get("content_type", "")
                parts = msg_data.get("message", {}).get("content", {}).get("parts", [])
                author = msg_data.get("message", {}).get("author", {}).get("role", "")
                safe_print(
                    f"[ChatGPT Web] SSE event #{event_count}: "
                    f"author={author}, content_type={ct}, parts_count={len(parts)}, "
                    f"parts_types={[type(p).__name__ for p in parts]}"
                )
                if parts and isinstance(parts[0], dict):
                    safe_print(f"[ChatGPT Web]   part[0] keys: {list(parts[0].keys())}")

            if found_conversation_id is None:
                if msg_data.get("conversation_id"):
                    found_conversation_id = msg_data["conversation_id"]
                    safe_print(f"[ChatGPT Web] conversation_id: {found_conversation_id}")
                if msg_data.get("message", {}).get("id"):
                    found_parent_message_id = msg_data["message"]["id"]

            message_author_role = msg_data.get("message", {}).get("author", {}).get("role")
            if message_author_role != "assistant":
                continue

            msg_id = msg_data.get("message", {}).get("id")
            if msg_id and msg_id != current_msg_id:
                current_msg_id = msg_id
                accumulated_content = ""

            content = self._extract_content(msg_data)
            if isinstance(content, str) and content:
                delta = content[len(accumulated_content):]
                if delta:
                    accumulated_content = content
                    if not yielded_ids:
                        yielded_ids = True
                        yield delta, found_conversation_id, found_parent_message_id
                    else:
                        yield delta, None, None

        if not accumulated_content and event_count > 0:
            safe_print(f"[ChatGPT Web] Parsed {event_count} SSE events but extracted no content")
            if found_conversation_id:
                self._fetch_created_conversation = True

    async def _fetch_conversation_content(
        self,
        page: Page,
        conversation_id: str,
        max_wait: int = 120,
    ) -> AsyncGenerator[Tuple[str, Optional[str], Optional[str]], None]:
        """当 handoff/WS 全部失败时，等待对话完成后通过 API 获取助手回复内容。"""
        safe_print(f"[ChatGPT Web] Fetch conversation fallback: waiting for response (max {max_wait}s)")
        waited = 0
        poll_interval = 0.5

        while waited < max_wait:
            await asyncio.sleep(poll_interval)
            waited += poll_interval

            try:
                result = await page.evaluate(
                    """async ({ conversationId }) => {
                        const session = await fetch("https://chatgpt.com/api/auth/session", { credentials: "include" })
                            .then(r => r.ok ? r.json() : null);
                        const accessToken = session?.accessToken;

                        const headers = {
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        };
                        if (accessToken) {
                            headers["Authorization"] = "Bearer " + accessToken;
                        }

                        const r = await fetch(
                            "https://chatgpt.com/backend-api/conversation/" + conversationId,
                            { headers, credentials: "include" }
                        );

                        if (!r.ok) return { ok: false, status: r.status };

                        const data = await r.json();
                        const mapping = data?.mapping || {};

                        let latestAssistantMsg = null;
                        let latestCreateTime = 0;
                        const nonTextTypes = ["thinking", "thoughts", "reasoning_recap", "model_editable_context"];

                        for (const [id, node] of Object.entries(mapping)) {
                            const msg = node?.message;
                            if (!msg) continue;
                            if (msg.author?.role !== "assistant") continue;
                            // Skip thinking/reasoning messages — we want the actual text response
                            const msgCt = msg.content?.content_type || "";
                            if (nonTextTypes.includes(msgCt)) continue;
                            const ct = msg.create_time || 0;
                            if (ct > latestCreateTime) {
                                latestCreateTime = ct;
                                latestAssistantMsg = msg;
                            }
                        }

                        if (!latestAssistantMsg) return { ok: true, content: null, status: null };

                        const content = latestAssistantMsg.content || {};
                        const parts = content.parts || [];
                        const textParts = [];

                        for (const part of parts) {
                            if (typeof part === "string" && part) {
                                textParts.push(part);
                                continue;
                            }
                            if (typeof part === "object" && part) {
                                const partCt = part.content_type || part.type || "";
                                if (nonTextTypes.includes(partCt)) continue;
                                const t = part.text || part.value || part.content || null;
                                if (typeof t === "string" && t) textParts.push(t);
                            }
                        }
                        const text = textParts.length ? textParts.join("") : null;

                        return {
                            ok: true,
                            content: text,
                            status: latestAssistantMsg.status || "",
                            messageId: latestAssistantMsg.id || null,
                        };
                    }""",
                    {"conversationId": conversation_id},
                )
            except Exception as e:
                safe_print(f"[ChatGPT Web] Fetch conversation error: {e}")
                continue

            if not result or not result.get("ok"):
                status = result.get("status", "?") if result else "?"
                safe_print(f"[ChatGPT Web] Fetch conversation API error: status={status}")
                continue

            msg_status = result.get("status", "")
            content = result.get("content")

            if msg_status == "finished_successfully" and content:
                safe_print(f"[ChatGPT Web] Fetch conversation got content ({len(content)} chars)")
                yield content, conversation_id, result.get("messageId")
                return

            if waited >= 5 and poll_interval < 2:
                poll_interval = 2

            if content and msg_status in ("in_progress", "streaming", "generating"):
                continue

            if not content and waited > 60:
                safe_print("[ChatGPT Web] Fetch conversation: no assistant message after 60s")
                break

        safe_print(f"[ChatGPT Web] Fetch conversation timed out after {max_wait}s")

    async def _chat_completions_via_dom(self, message: str) -> AsyncGenerator[Tuple[str, Optional[str], Optional[str]], None]:
        page = self.page

        input_selectors = [
            "#prompt-textarea",
            "textarea[placeholder]",
            "textarea",
            '[contenteditable="true"]',
        ]

        input_handle = None
        for sel in input_selectors:
            input_handle = await page.query_selector(sel)
            if input_handle:
                break

        if not input_handle:
            raise Exception("ChatGPT DOM 模拟失败: 找不到输入框")

        await input_handle.click()
        await asyncio.sleep(0.3)
        await page.keyboard.type(message, delay=20)
        await asyncio.sleep(0.5)
        await page.keyboard.press("Enter")
        safe_print("[ChatGPT Web] DOM: typed message and pressed Enter")

        max_wait_ms = 90000
        poll_interval_ms = 2000
        last_text = ""
        stable_count = 0

        for _elapsed in range(0, max_wait_ms, poll_interval_ms):
            await asyncio.sleep(poll_interval_ms / 1000)

            result = await page.evaluate(
                """() => {
                    const clean = (t) => t.replace(/[\\u200B-\\u200D\\uFEFF]/g, "").trim();
                    const els = document.querySelectorAll(
                        'div[data-message-author-role="assistant"], .agent-turn [data-message-author-role="assistant"], [class*="markdown"], [class*="assistant"]'
                    );
                    const last = els.length > 0 ? els[els.length - 1] : null;
                    const text = last ? clean(last.textContent || "") : "";
                    const stopBtn = document.querySelector('button.bg-black .icon-lg, [aria-label*="Stop"]');
                    const isStreaming = !!stopBtn;
                    return { text, isStreaming };
                }"""
            )

            if result["text"] and result["text"] != last_text:
                delta = result["text"][len(last_text):]
                yield delta, None, None
                last_text = result["text"]
                stable_count = 0
            elif result["text"]:
                stable_count += 1
                if not result["isStreaming"] and stable_count >= 2:
                    break

        if not last_text:
            raise Exception("ChatGPT DOM 模拟：未检测到回复")

    async def chat_completions(
        self,
        message: str,
        conversation_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> AsyncGenerator[Tuple[str, Optional[str], Optional[str]], None]:
        """
        Yields (delta, conversation_id, parent_message_id).

        流程：
        1. 优先流式 fetch
        2. 若流式返回有效内容则直接结束
        3. 若流式完全失败且未创建对话，则尝试非流式 fetch
        4. 若 fetch 也失败，再尝试 DOM fallback
        关键：如果已经创建了对话，不再走 DOM 发送同一条消息，避免重复创建。
        """
        await self._ensure_browser()
        page = self.page
        self._fetch_created_conversation = False

        parent_message_id = parent_message_id or str(uuid.uuid4())
        message_id = str(uuid.uuid4())

        model = normalize_model(model)
        safe_print(f"[ChatGPT Web] Sending message (model: {model})")

        body = {
            "action": "next",
            "messages": [
                {
                    "id": message_id,
                    "author": {"role": "user"},
                    "content": {
                        "content_type": "text",
                        "parts": [message],
                    },
                },
            ],
            "conversation_id": conversation_id,
            "parent_message_id": parent_message_id,
            "model": model,
            "timezone_offset_min": -480,
            "timezone": "Asia/Shanghai",
            "history_and_training_disabled": False,
            "conversation_mode": {"kind": "primary_assistant", "plugin_ids": None},
            "force_paragen": False,
            "force_paragen_model_slug": "",
            "force_rate_limit": False,
            "reset_rate_limits": False,
            "force_use_sse": True,
        }

        if model == "gpt-5-5-thinking":
            body["thinking_effort"] = "extended"
            body["supports_buffering"] = True
            body["supported_encodings"] = ["v1"]
            body["enable_message_followups"] = True
            body["paragen_cot_summary_display_override"] = "allow"
            body["force_parallel_switch"] = "auto"
            body["client_contextual_info"] = {
                "is_dark_mode": False,
                "time_since_loaded": 10,
                "page_height": 900,
                "page_width": 1440,
                "pixel_ratio": 1,
                "screen_height": 1080,
                "screen_width": 1920,
                "app_name": "chatgpt.com",
            }

        page_url = page.url

        try:
            safe_print("[ChatGPT Web] Streaming fetch request...")
            got_delta = False
            async for delta, conv_id, msg_id in self._chat_completions_via_stream(page, body, page_url, model):
                got_delta = True
                yield delta, conv_id, msg_id
            if got_delta:
                return
            handoff_state = await page.evaluate(
                """() => {
                    const h = window._cgStream?.handoff || null;
                    if (!h) return null;
                    return {
                        topicId: h.topicId || null,
                        conversationId: h.conversationId || null,
                        transport: h.transport || null,
                        wsError: h.wsError || null,
                        wsResult: h.wsResult || null,
                    };
                }"""
            )

            if handoff_state:
                safe_print(f"[ChatGPT Web] Handoff state after stream: transport={handoff_state.get('transport')}, "
                           f"wsError={handoff_state.get('wsError')}, wsResult={handoff_state.get('wsResult')}")

                # celsius WS 成功时内容已在 stream 中 yield
                # 只在 celsius WS 失败时，才尝试 fetch conversation fallback
                if handoff_state.get("wsError") and handoff_state.get("conversationId"):
                    safe_print(f"[ChatGPT Web] Celsius WS failed, trying fetch conversation fallback")
                    got_fetch_delta = False
                    async for delta, conv_id, msg_id in self._fetch_conversation_content(
                            page, handoff_state["conversationId"], max_wait=120):
                        got_fetch_delta = True
                        yield delta, conv_id, msg_id
                    if got_fetch_delta:
                        return
        except Exception as e:
            err_str = str(e)
            if err_str.startswith("403:"):
                safe_print("[ChatGPT Web] 403 in stream, trying DOM fallback")
                async for delta, conv_id, msg_id in self._chat_completions_via_dom(message):
                    yield delta, conv_id, msg_id
                return
            if "authentication failed" in err_str.lower():
                raise
            safe_print(f"[ChatGPT Web] Stream failed: {e}")

        if self._fetch_created_conversation:
            # 尝试从 handoff 获取 conversation_id
            conv_id_for_fetch = None
            try:
                conv_id_for_fetch = await page.evaluate(
                    """() => {
                        const h = window._cgStream?.handoff || null;
                        return h?.conversationId || null;
                    }"""
                )
            except Exception:
                pass

            if conv_id_for_fetch:
                safe_print(f"[ChatGPT Web] Conversation created but no content, trying fetch conversation fallback")
                got_fetch_delta = False
                async for delta, conv_id, msg_id in self._fetch_conversation_content(
                        page, conv_id_for_fetch, max_wait=120):
                    got_fetch_delta = True
                    yield delta, conv_id, msg_id
                if got_fetch_delta:
                    return
            else:
                safe_print("[ChatGPT Web] Conversation created but no conversation_id available for fetch fallback")
            return

        try:
            safe_print("[ChatGPT Web] Non-streaming fetch request...")
            response_data = await self._chat_completions_via_fetch(page, body, page_url)
        except Exception as e:
            safe_print(f"[ChatGPT Web] Fetch failed: {e}")
            async for delta, conv_id, msg_id in self._chat_completions_via_dom(message):
                yield delta, conv_id, msg_id
            return

        if not response_data.get("ok"):
            status = response_data.get("status", 0)
            error = response_data.get("error", "")
            sentinel_error = response_data.get("sentinelError", "")

            if status == 403:
                safe_print(f"[ChatGPT Web] 403 risk control, trying DOM fallback (sentinel: {sentinel_error})")
                async for delta, conv_id, msg_id in self._chat_completions_via_dom(message):
                    yield delta, conv_id, msg_id
                return
            if status == 401:
                raise Exception("ChatGPT authentication failed")
            raise Exception(f"ChatGPT API error {status}: {error[:200]}")

        data = response_data.get("data", "")
        if not data:
            safe_print("[ChatGPT Web] Empty response, trying DOM fallback")
            async for delta, conv_id, msg_id in self._chat_completions_via_dom(message):
                yield delta, conv_id, msg_id
            return

        safe_print(f"[ChatGPT Web] Response received ({len(data)} bytes), parsing SSE...")
        got_delta = False
        for delta, conv_id, msg_id in self._parse_sse_response(data):
            got_delta = True
            yield delta, conv_id, msg_id

        if not got_delta:
            if self._fetch_created_conversation:
                safe_print("[ChatGPT Web] Non-streaming also got no assistant content after conversation creation")
            else:
                safe_print("[ChatGPT Web] SSE parse yielded no content, trying DOM fallback")
                async for delta, conv_id, msg_id in self._chat_completions_via_dom(message):
                    yield delta, conv_id, msg_id

    async def init(self):
        await self._ensure_browser()

    async def close(self):
        self.browser = None
        self.page = None
        self._browser = None
        self._stream_binding_installed = False
        self._stream_queues.clear()
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._browser_process = None

    async def discover_models(self) -> list[ModelDefinitionConfig]:
        if CHATGPT_WEB_MODELS:
            return [
                ModelDefinitionConfig(
                    id=model["id"],
                    name=model["name"],
                    api="chatgpt-web",
                    reasoning=bool(model["reasoning"]),
                    input=["text", "image"],
                    cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    context_window=int(model["context_window"]),
                    max_tokens=int(model["max_tokens"]),
                )
                for model in CHATGPT_WEB_MODELS
            ]
        return [
            ModelDefinitionConfig(
                id=DEFAULT_MODEL,
                name="GPT-5.3",
                api="chatgpt-web",
                reasoning=False,
                input=["text", "image"],
                cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                context_window=52815,
                max_tokens=52815,
            )
        ]

    @classmethod
    async def create(
        cls,
        auto_init: bool = True,
        cdp_port: Optional[int] = None,
        attach_only: bool = False,
        chrome_path: Optional[str] = None,
        user_data_dir: Optional[str] = None,
    ) -> "ChatGPTWebClient":
        login_result = await login_chatgpt_web(
            cdp_port=cdp_port,
            attach_only=attach_only,
            user_data_dir=user_data_dir,
        )
        options = ChatGPTWebClientOptions(
            access_token=login_result.auth.access_token,
            cookie=login_result.auth.cookie,
            user_agent=login_result.auth.user_agent,
        )
        client = cls(
            options,
            cdp_port=cdp_port,
            attach_only=attach_only,
            chrome_path=chrome_path,
            user_data_dir=user_data_dir,
            browser=login_result.browser,
            playwright=login_result.playwright,
            browser_process=login_result.browser_process,
            chatgpt_page=login_result.chatgpt_page,
        )

        if auto_init:
            await client.init()

        return client
