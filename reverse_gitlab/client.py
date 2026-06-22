"""Async GitLab Duo Chat client via GraphQL Workflow API.

Auth flow: PAT → GraphQL createAiDuoWorkflow → poll getWorkflowLatestCheckpoint.
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Optional, Tuple

from . import main as _reverse


def safe_print(msg: str) -> None:
    try:
        print(msg.encode("utf-8", errors="backslashreplace").decode("utf-8"))
    except Exception:
        try:
            print(msg)
        except Exception:
            pass


class GitLabWebClient:

    def __init__(self):
        self._reverse = _reverse
        self.stream_chunk_size = 96
        self.stream_chunk_delay = 0.01

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @classmethod
    async def create(cls, auto_init: bool = True) -> "GitLabWebClient":
        client = cls()
        if auto_init:
            await client.init()
        return client

    def _stream_sync(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        message: str,
        conversation_id: str | None,
        parent_message_id: str | None,
        model: str | None,
        messages: list | None = None,
    ) -> None:
        def put(item: tuple[str, str | None, str | None] | BaseException | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        try:
            cookies, headers, config = self._reverse.load_config()
            pat = self._reverse.get_pat(cookies)
            proxy_url = str(config.get("proxy") or self._reverse._system_proxy_url() or "")
            transport = str(config.get("transport") or "curl_cffi").lower()
            session = self._reverse._make_http_session(proxy_url, transport)

            user_agent = headers.get(
                "user-agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
            )
            session.headers.update({"user-agent": user_agent})

            model_id = self._reverse.normalize_gitlab_model(model)
            mapping = self._reverse.get_model_mapping(model_id)
            safe_print(f"[GitLab Duo] completion: model={model_id} -> {mapping['display']}")

            found_conversation_id: str | None = conversation_id
            found_parent_id: str | None = parent_message_id

            for event in self._reverse.stream_completion(
                session,
                config,
                pat,
                message,
                model,
                conversation_id=conversation_id,
                parent_message_id=parent_message_id,
                messages=messages,
            ):
                if event.conversation_id and not found_conversation_id:
                    found_conversation_id = event.conversation_id
                    put(("", found_conversation_id, found_parent_id))
                if event.response_id and not found_parent_id:
                    found_parent_id = event.response_id
                    put(("", found_conversation_id, found_parent_id))
                if event.delta:
                    put((event.delta, found_conversation_id, found_parent_id))

            put(("", found_conversation_id, found_parent_id))
            put(None)
        except BaseException as exc:
            put(exc)
            put(None)

    async def chat_completions(
        self,
        message: str,
        conversation_id: Optional[str] = None,
        parent_message_id: Optional[str] = None,
        model: Optional[str] = None,
        messages: Optional[list] = None,
    ) -> AsyncGenerator[Tuple[str, Optional[str], Optional[str]], None]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, str | None, str | None] | BaseException | None] = asyncio.Queue()
        worker_task = asyncio.create_task(
            asyncio.to_thread(
                self._stream_sync,
                loop,
                queue,
                message,
                conversation_id,
                parent_message_id,
                model,
                messages,
            )
        )
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                delta, conv_id, parent_id = item
                if len(delta) <= self.stream_chunk_size:
                    yield item
                    continue
                for start in range(0, len(delta), self.stream_chunk_size):
                    chunk = delta[start:start + self.stream_chunk_size]
                    yield chunk, conv_id if start == 0 else None, parent_id if start == 0 else None
                    await asyncio.sleep(self.stream_chunk_delay)
        finally:
            await worker_task
