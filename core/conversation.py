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


def tools_hash(tools: list[dict[str, Any]] | None) -> str:
    """Hash tool definitions to detect changes."""
    if not tools:
        return ""
    stable = []
    for tool in tools:
        fn = tool.get("function", tool)
        stable.append({"name": fn.get("name", ""), "parameters": fn.get("parameters", {})})
    return hashlib.md5(json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


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
        cached_tools_hash = cache.get(f"tools_{explicit_conversation_id}", "")
        return explicit_conversation_id, None, cached_tools_hash != current_tools_hash

    fingerprint = user_messages_fingerprint_for_lookup(messages)
    if not fingerprint:
        safe_print("No conversation history to match, starting new conversation")
        return None, None, True

    safe_print(f"Looking for conversation with fingerprint: {fingerprint}")
    cached = cache.get(f"user_fp_{fingerprint}")

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

    old_fingerprint = None
    for key in list(cache.keys()):
        if key.startswith("user_fp_"):
            value = cache[key]
            if isinstance(value, dict) and value.get("conv_id") == conversation_id:
                old_fingerprint = key.replace("user_fp_", "")
                del cache[key]
                break

    if old_fingerprint:
        safe_print(f"Deleted old fingerprint: {old_fingerprint}")

    cache[f"user_fp_{new_fingerprint}"] = {
        "conv_id": conversation_id,
        "parent_message_id": parent_message_id,
        "tools_hash": tools_hash(tools),
        "model": model,
    }
    cache[f"tools_{conversation_id}"] = tools_hash(tools)
    cache["last_conv_id"] = conversation_id
    save_cache(cache)
    safe_print(f"Saved conversation: {conversation_id} with fingerprint: {new_fingerprint}")
