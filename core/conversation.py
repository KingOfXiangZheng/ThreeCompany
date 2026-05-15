"""Conversation continuity helpers for OpenAI API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


CACHE_DIR = Path(__file__).resolve().parent.parent / "conv_cache"


def extract_text(message: dict[str, Any]) -> str:
    """Extract plain text from a message's content field."""
    content = message.get("content", "")
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(content)


def _real_user_message_texts(messages: list[dict[str, Any]]) -> list[str]:
    user_messages: list[str] = []
    for msg in messages:
        if msg.get("role") == "user" and not msg.get("_is_tool_result_inline"):
            text = extract_text(msg)
            if text:
                user_messages.append(text)
    return user_messages


def _hash_user_messages(user_messages: list[str]) -> str:
    if not user_messages:
        return ""
    content = "\n".join(user_messages)
    return hashlib.md5(content.encode()).hexdigest()


def user_messages_fingerprint_for_lookup(messages: list[dict[str, Any]], include_last: bool = False) -> str:
    """Generate a conversation fingerprint from real user messages.

    Lookup fingerprints exclude the current user prompt so they can match the
    conversation saved after the previous assistant response. Save fingerprints
    include every real user prompt in the completed conversation. Inline tool
    result messages are skipped because they are generated transport context,
    not user-authored conversation turns.
    """
    user_messages = _real_user_message_texts(messages)
    if include_last:
        return _hash_user_messages(user_messages)

    if messages:
        last_msg = messages[-1]
        if last_msg.get("role") == "user" and not last_msg.get("_is_tool_result_inline"):
            return _hash_user_messages(user_messages[:-1])

    return _hash_user_messages(user_messages)


def user_message_fingerprints_for_lookup(messages: list[dict[str, Any]]) -> list[str]:
    """Return lookup fingerprints from longest to shortest real-user prefix."""
    user_messages = _real_user_message_texts(messages)
    if messages:
        last_msg = messages[-1]
        if last_msg.get("role") == "user" and not last_msg.get("_is_tool_result_inline"):
            user_messages = user_messages[:-1]
    return [
        fingerprint
        for fingerprint in (_hash_user_messages(user_messages[:index]) for index in range(len(user_messages), 0, -1))
        if fingerprint
    ]


def tools_hash(tools: list[dict[str, Any]] | None) -> str:
    """Hash tool definitions to detect changes."""
    if not tools:
        return ""
    stable = []
    for tool in tools:
        fn = tool.get("function", tool)
        stable.append({"name": fn.get("name", ""), "parameters": fn.get("parameters", {})})
    return hashlib.md5(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _scoped_fingerprint_key(fingerprint: str, model: str | None, tools_hash_value: str) -> str:
    scope = hashlib.md5(f"{model or ''}:{tools_hash_value}".encode()).hexdigest()
    return f"user_fp_{scope}_{fingerprint}"


def cache_path() -> Path:
    """Get the conversation cache path."""
    return CACHE_DIR / "conversations.json"


def load_cache() -> dict[str, Any]:
    """Load conversation cache from disk."""
    path = cache_path()
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8"))
    except Exception:
        pass
    return {}


def save_cache(cache: dict[str, Any]) -> None:
    """Save conversation cache to disk."""
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        cache_path().write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def safe_print(msg: str) -> None:
    """Safe print to avoid encoding issues."""
    try:
        print(msg.encode("utf-8", errors="backslashreplace").decode("utf-8"))
    except Exception:
        try:
            print(msg)
        except Exception:
            pass


def lookup_conversation(
    messages: list[dict[str, Any]],
    explicit_conversation_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> tuple[str | None, str | None, bool]:
    """
    Look up conversation.
    Returns (conversation_id, parent_message_id, tools_changed).
    """
    current_tools_hash = tools_hash(tools)
    cache = load_cache()

    if explicit_conversation_id:
        safe_print(f"Trying explicit conversation_id: {explicit_conversation_id}")
        cached_model = cache.get(f"model_{explicit_conversation_id}", "")
        if model and cached_model and cached_model != model:
            safe_print(
                f"Explicit conversation_id model mismatch: cached={cached_model}, requested={model}. Starting new."
            )
            return None, None, True
        cached_tools_hash = cache.get(f"tools_{explicit_conversation_id}", "")
        return explicit_conversation_id, None, cached_tools_hash != current_tools_hash

    lookup_fingerprints = user_message_fingerprints_for_lookup(messages)
    fingerprint = lookup_fingerprints[0] if lookup_fingerprints else ""
    if not lookup_fingerprints:
        safe_print("No conversation history to match, starting new conversation")
        return None, None, True

    safe_print(f"Looking for conversation with fingerprint: {fingerprint}")
    scoped_key = _scoped_fingerprint_key(fingerprint, model, current_tools_hash)
    cached = cache.get(scoped_key)
    if not isinstance(cached, dict):
        safe_print(
            f"No scoped conversation for model={model or '-'}, tools_hash={current_tools_hash or '-'}"
        )
        alternate_models = []
        suffix = f"_{fingerprint}"
        for key, value in cache.items():
            if key.startswith("user_fp_") and key.endswith(suffix) and isinstance(value, dict):
                alternate_models.append(str(value.get("model") or "unknown"))
        if alternate_models:
            safe_print(
                "Found same fingerprint under other model scopes: "
                + ", ".join(sorted(set(alternate_models)))
            )
        cached = cache.get(f"user_fp_{fingerprint}")

    if not isinstance(cached, dict):
        for fallback_fingerprint in lookup_fingerprints[1:]:
            fallback_key = _scoped_fingerprint_key(fallback_fingerprint, model, current_tools_hash)
            fallback_cached = cache.get(fallback_key)
            if isinstance(fallback_cached, dict) and fallback_cached.get("conv_id"):
                safe_print(
                    "Falling back to earlier same-model conversation prefix: "
                    f"{fallback_fingerprint}"
                )
                cached = fallback_cached
                break

    if isinstance(cached, dict) and cached.get("conv_id"):
        cached_model = cached.get("model")
        if model and cached_model != model:
            safe_print(
                f"Cached conversation model mismatch: cached={cached_model or 'unknown'}, requested={model}. Starting new."
            )
            return None, None, True
        conv_id = cached["conv_id"]
        parent_message_id = cached.get("parent_message_id")
        cached_tools_hash = cached.get("tools_hash", "")
        return conv_id, parent_message_id, cached_tools_hash != current_tools_hash

    safe_print("No matching conversation found, starting new")
    return None, None, True


def save_conversation(
    messages: list[dict[str, Any]],
    conversation_id: str,
    parent_message_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> None:
    """Save conversation mapping."""
    if not conversation_id:
        return

    new_fingerprint = user_messages_fingerprint_for_lookup(messages, include_last=True)
    if not new_fingerprint:
        safe_print("No history fingerprint to save")
        return

    cache = load_cache()

    scoped_key = _scoped_fingerprint_key(new_fingerprint, model, tools_hash(tools))
    cache[scoped_key] = {
        "conv_id": conversation_id,
        "parent_message_id": parent_message_id,
        "tools_hash": tools_hash(tools),
        "model": model,
    }
    cache[f"tools_{conversation_id}"] = tools_hash(tools)
    cache[f"model_{conversation_id}"] = model
    cache["last_conv_id"] = conversation_id
    save_cache(cache)
    safe_print(f"Saved conversation: {conversation_id} with fingerprint: {new_fingerprint}")
