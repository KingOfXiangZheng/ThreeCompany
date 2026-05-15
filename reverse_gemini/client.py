"""Async Gemini Web client backed by the pure HTTP reverse implementation."""
from __future__ import annotations

import asyncio
import urllib.parse
import uuid
from typing import Any, AsyncGenerator, Optional, Tuple

from . import main as _reverse


def safe_print(msg: str) -> None:
    try:
        print(msg.encode("utf-8", errors="backslashreplace").decode("utf-8"))
    except Exception:
        try:
            print(msg)
        except Exception:
            pass


class GeminiWebClient:
    """ChatBackend-compatible Gemini reverse client.

    The controller passes ``parent_message_id`` as the continuation state. For
    Gemini this value is the previous ``response_id`` returned by StreamGenerate.
    """

    def __init__(self, gemini_url: str = "https://gemini.google.com/u/1"):
        self.gemini_url = gemini_url
        self._reverse = _reverse
        self.stream_chunk_size = 96
        self.stream_chunk_delay = 0.01

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @classmethod
    async def create(
        cls,
        auto_init: bool = True,
        cdp_port: Optional[int] = None,
        attach_only: bool = False,
        chrome_path: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        gemini_url: str = "https://gemini.google.com/u/1",
    ) -> "GeminiWebClient":
        client = cls(gemini_url=gemini_url)
        if auto_init:
            await client.init()
        return client

    def _stream_sync(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        message: str,
        conversation_id: str | None,
        response_id: str | None,
        model: str | None,
    ) -> None:
        import requests
        import urllib3

        def put(item: tuple[str, str | None, str | None] | BaseException | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            cookies, headers, config = self._reverse.load_config()
            session = self._reverse.make_session(cookies, headers)
            bootstrap = self._reverse.fetch_bootstrap(session, config, self.gemini_url)
            safe_print(
                "[Gemini Web] bootstrap: "
                f"status={bootstrap.status}, url={bootstrap.url}, bl={bootstrap.bl}, has_at={bool(bootstrap.at)}, "
                f"model={model or '-'}, conversation_id={conversation_id or '-'}, response_id={response_id or '-'}"
            )

            cached_state = self._reverse.load_gemini_stream_state(conversation_id, response_id)
            if conversation_id and response_id:
                safe_print(
                    "[Gemini Web] continuation state: "
                    f"candidate_id={cached_state.get('candidate_id') or '-'}, "
                    f"has_token={bool(cached_state.get('conversation_token'))}"
                )

            request_id = str(uuid.uuid4()).upper()
            body = self._reverse.build_stream_body(
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
            request_headers = self._reverse.build_stream_headers(bootstrap.url, model, request_id=request_id)
            if bootstrap.bl:
                config["version"] = bootstrap.bl

            last_error: str | None = None
            for path in bootstrap.stream_paths:
                for network_attempt in range(3):
                    if network_attempt:
                        session = self._reverse.make_session(cookies, headers)
                    full_url = urllib.parse.urljoin(
                        config["api_base"],
                        self._reverse.with_query(path, config, bootstrap.f_sid),
                    )

                    response_sample = ""
                    response_raw = ""
                    accumulated = ""
                    found_conv_id: str | None = None
                    found_response_id: str | None = None
                    saw_event = False

                    status_code: int | None = None
                    try:
                        with session.post(
                            full_url,
                            data=body,
                            headers=request_headers,
                            timeout=60,
                            verify=False,
                        ) as response:
                            status_code = response.status_code
                            for line in response.text.splitlines():
                                if not line:
                                    continue
                                if len(response_sample) < 800:
                                    response_sample += line + "\n"
                                if len(response_raw) < 20000:
                                    response_raw += line + "\n"
                                events = self._reverse.parse_stream_response(line)
                                for text, conv_id, resp_id in events:
                                    if conv_id:
                                        found_conv_id = conv_id
                                    if resp_id:
                                        found_response_id = resp_id
                                    if not text:
                                        continue
                                    saw_event = True
                                    if text == accumulated or accumulated.startswith(text):
                                        continue
                                    if text.startswith(accumulated):
                                        delta = text[len(accumulated):]
                                    else:
                                        delta = text
                                    if delta:
                                        accumulated = text
                                        put((delta, found_conv_id, found_response_id))
                    except Exception as exc:
                        if not self._reverse.is_request_error(exc):
                            raise
                        last_error = (
                            f"path={path}, attempt={network_attempt + 1}, "
                            f"status={status_code or '-'}, network_error={exc}"
                        )
                        safe_print(f"[Gemini Web] StreamGenerate path failed: {last_error}")
                        if not saw_event:
                            continue

                    if saw_event or found_conv_id or found_response_id:
                        if saw_event:
                            stream_state = self._reverse.extract_stream_state(response_raw)
                            if found_conv_id:
                                stream_state.setdefault("conversation_id", found_conv_id)
                            if found_response_id:
                                stream_state.setdefault("response_id", found_response_id)
                            self._reverse.save_gemini_stream_state(stream_state)
                            ack_session = self._reverse.make_session(cookies, headers)
                            ack = self._reverse.post_generation_ack(ack_session, config, bootstrap, found_response_id, model)
                            if ack:
                                safe_print(
                                    "[Gemini Web] post-generation ack: "
                                    f"status={ack.get('status')}, attempt={ack.get('attempt')}, "
                                    f"response_id={found_response_id or '-'}"
                                )
                            put(("", found_conv_id, found_response_id))
                            put(None)
                            return
                        error_codes = self._reverse.extract_bard_error_codes(response_sample)
                        last_error = (
                            f"status={status_code or '-'}, bard_errors={error_codes}, "
                            f"sample={response_sample[:800]}"
                        )
                        break

                    last_error = f"status={status_code or '-'}, sample={response_sample[:800]}"
                    break

            raise RuntimeError(f"Gemini StreamGenerate returned no parsed events: {last_error or 'no attempts'}")
        except BaseException as exc:
            put(exc)
            put(None)

    async def chat_completions(
        self,
        message: str,
        conversation_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> AsyncGenerator[Tuple[str, Optional[str], Optional[str]], None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, str | None, str | None] | BaseException | None] = asyncio.Queue()
        worker = asyncio.to_thread(
            self._stream_sync,
            loop,
            queue,
            message,
            conversation_id,
            parent_message_id,
            model,
        )
        worker_task = asyncio.create_task(worker)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                delta, conv_id, response_id = item
                if len(delta) <= self.stream_chunk_size:
                    yield item
                    continue
                for start in range(0, len(delta), self.stream_chunk_size):
                    chunk = delta[start:start + self.stream_chunk_size]
                    yield chunk, conv_id if start == 0 else None, response_id if start == 0 else None
                    await asyncio.sleep(self.stream_chunk_delay)
        finally:
            await worker_task
