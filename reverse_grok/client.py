"""Async Grok Web client backed by the pure HTTP reverse implementation."""
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


class GrokWebClient:
    """ChatBackend-compatible Grok Web reverse client.

    ``conversation_id`` is the Grok conversation identifier returned by the
    creation endpoint. Grok's new-conversation endpoint currently starts the
    response stream, so continuation support is intentionally conservative.
    """

    def __init__(self):
        self._reverse = _reverse
        self.stream_chunk_size = 96
        self.stream_chunk_delay = 0.01

    async def init(self) -> None:
        return None

    async def close(self) -> None:
        return None

    @classmethod
    async def create(cls, auto_init: bool = True) -> "GrokWebClient":
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
    ) -> None:
        def put(item: tuple[str, str | None, str | None] | BaseException | None) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        try:
            cookies, headers, config = self._reverse.load_config()
            session = self._reverse.make_session(cookies, headers, config)
            mode_id = self._reverse.mode_id_from_model(model, config)
            safe_print(
                "[Grok Web] completion: "
                f"model={model or '-'}, mode_id={mode_id}, conversation_id={conversation_id or '-'}"
            )

            found_conversation_id: str | None = conversation_id
            found_parent_id: str | None = parent_message_id
            for event in self._reverse.stream_new_conversation(
                session,
                config,
                message,
                model,
                conversation_id=conversation_id,
                parent_response_id=parent_message_id,
            ):
                if event.conversation_id and not found_conversation_id:
                    found_conversation_id = event.conversation_id
                    put(("", found_conversation_id, found_parent_id))
                if event.response_id:
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
