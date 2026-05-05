"""Reusable chat completion control flow for web-backed model providers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Protocol

from .conversation import extract_text, lookup_conversation, save_conversation
from .models import model_ids as default_model_ids
from .models import normalize_model as default_normalize_model
from .tool_support import build_tool_prompt, inline_tool_messages, try_parse_tool_calls


TOOL_CALL_REMINDER = """<reminder>
When you need to invoke a tool, please output the tool request text in the following format, and I will return the tool results to you. Tool call format must be strictly as follows:

{"name":"<tool_name>","arguments":{}}

Rules:
1. The JSON object may contain only two fields: `name` and `arguments`.
2. `arguments` must be an object and must strictly follow the tool schema.
3. When calling a tool, output only this JSON. Do not add explanations, Markdown, or code fences before or after it.
4. You may call multiple tools in a single response by providing multiple JSON objects.
5. Even if the tool has no parameters, you must still write `"arguments": {}`.
</reminder>"""


class ModelNotFoundError(ValueError):
    def __init__(self, model: str):
        super().__init__(f"Unsupported model: {model}")
        self.model = model


class NoUserMessageError(ValueError):
    pass


class ChatBackend(Protocol):
    async def chat_completions(
        self,
        message: str,
        conversation_id: str | None = None,
        parent_message_id: str | None = None,
        model: str | None = None,
    ) -> AsyncGenerator[tuple[str, str | None, str | None], None]:
        ...


@dataclass
class PreparedChatRequest:
    model: str
    messages: list[dict[str, Any]]
    final_message: str
    conversation_id: str | None
    parent_message_id: str | None
    tools: list[dict[str, Any]] | None


@dataclass
class ChatStreamEvent:
    delta: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    conversation_id: str | None = None
    parent_message_id: str | None = None


@dataclass
class ChatCompletionResult:
    content: str
    tool_calls: list[dict[str, Any]] | None
    finish_reason: str
    conversation_id: str | None
    parent_message_id: str | None
    model: str


def extract_system_message(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "system":
            return str(msg.get("content", ""))
    return ""


def last_user_message(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user" and msg.get("content"):
            return extract_text(msg)
    raise NoUserMessageError("No user message found")


class ChatCompletionController:
    """Provider-neutral orchestration around a ChatGPT-like streaming backend.

    Future providers, such as Gemini, only need to implement ``ChatBackend``.
    The conversation lookup, tool prompt injection, system prompt injection,
    and tool-call parsing stay here.
    """

    def __init__(
        self,
        backend: ChatBackend,
        normalize_model_fn: Callable[[str | None], str] = default_normalize_model,
        model_ids_fn: Callable[[], set[str]] = default_model_ids,
    ):
        self.backend = backend
        self.normalize_model = normalize_model_fn
        self.model_ids = model_ids_fn

    def prepare(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
        tools: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
    ) -> PreparedChatRequest:
        normalized_model = self.normalize_model(model)
        if normalized_model not in self.model_ids():
            raise ModelNotFoundError(model or "")

        prepared_messages = inline_tool_messages(messages)
        cached_conv_id, cached_parent_message_id, tools_changed = lookup_conversation(
            prepared_messages,
            conversation_id,
            tools,
            normalized_model,
        )

        conv_id = cached_conv_id or conversation_id
        final_message = last_user_message(prepared_messages)

        if tools:
            final_message = f"{final_message}\n\n{TOOL_CALL_REMINDER}"

        if conv_id is None and any(m.get("role") == "system" for m in prepared_messages):
            system_msg = extract_system_message(prepared_messages)
            if system_msg:
                final_message = f"{system_msg}\n\nUser: {final_message}"

        if tools_changed and tools:
            tool_prompt = build_tool_prompt(tools)
            final_message = f"{tool_prompt}\n\n{final_message}"

        return PreparedChatRequest(
            model=normalized_model,
            messages=prepared_messages,
            final_message=final_message,
            conversation_id=conv_id,
            parent_message_id=cached_parent_message_id,
            tools=tools,
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
        tools: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
    ) -> AsyncGenerator[ChatStreamEvent, None]:
        prepared = self.prepare(messages, model, tools, conversation_id)
        full_response = ""
        found_conversation_id: str | None = None
        found_parent_message_id: str | None = None

        async for delta, conv_id_delta, parent_msg_id in self.backend.chat_completions(
            prepared.final_message,
            conversation_id=prepared.conversation_id,
            parent_message_id=prepared.parent_message_id,
            model=prepared.model,
        ):
            if conv_id_delta and not found_conversation_id:
                found_conversation_id = conv_id_delta
            if parent_msg_id and not found_parent_message_id:
                found_parent_message_id = parent_msg_id

            if delta:
                full_response += delta
                yield ChatStreamEvent(
                    delta=delta,
                    conversation_id=found_conversation_id,
                    parent_message_id=found_parent_message_id,
                )

        tool_calls = try_parse_tool_calls(full_response)
        finish_reason = "tool_calls" if tool_calls else "stop"

        conversation_to_save = found_conversation_id or prepared.conversation_id
        parent_to_save = found_parent_message_id or prepared.parent_message_id
        if conversation_to_save:
            messages_to_save = [*prepared.messages, {"role": "assistant", "content": full_response}]
            save_conversation(
                messages_to_save,
                conversation_to_save,
                parent_to_save,
                prepared.tools,
                prepared.model,
            )

        yield ChatStreamEvent(
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            conversation_id=conversation_to_save,
            parent_message_id=parent_to_save,
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        model: str | None,
        tools: list[dict[str, Any]] | None = None,
        conversation_id: str | None = None,
    ) -> ChatCompletionResult:
        content = ""
        final_event: ChatStreamEvent | None = None

        async for event in self.stream(messages, model, tools, conversation_id):
            if event.delta:
                content += event.delta
            if event.finish_reason:
                final_event = event

        if final_event is None:
            final_event = ChatStreamEvent(finish_reason="stop")

        return ChatCompletionResult(
            content=content,
            tool_calls=final_event.tool_calls,
            finish_reason=final_event.finish_reason or "stop",
            conversation_id=final_event.conversation_id,
            parent_message_id=final_event.parent_message_id,
            model=self.normalize_model(model),
        )
