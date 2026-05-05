"""Shared prompt, tool-call parsing, and conversation fingerprint helpers."""
from __future__ import annotations

import json
import re
import uuid
from typing import Any


def build_tool_prompt(tools: list[dict[str, Any]] | None) -> str:
    if not tools:
        return ""
    lines = [
        "# Tools",
        "",
        "You have access to the following tools.",
        "",
        "When you decide to call a tool, respond with a fenced JSON block:",
        "",
        "```json",
        '{"name": "<tool_name>", "arguments": {}}',
        "```",
        "",
        "Rules:",
        '1. The JSON must contain exactly "name" and "arguments".',
        "2. The arguments object must follow the tool schema.",
        "3. Output no explanation before or after the JSON when calling a tool.",
        "4. You may call multiple tools in a single response by providing multiple ```json blocks.",
        "",
        "Available tools:",
        "",
    ]
    for tool in tools:
        fn = tool.get("function", tool)
        name = fn.get("name", "")
        description = fn.get("description", "")
        parameters = fn.get("parameters", {})
        lines.append(f"## {name}")
        if description:
            lines.append(f"Description: {description}")
        if parameters:
            lines.append(f"Parameters: {json.dumps(parameters, ensure_ascii=False)}")
        lines.append("")
    return "\n".join(lines)


_TOOL_BLOCK_RE = re.compile(r"```(?:json|JSON)?\s*\n?\s*(\{[\s\S]*?)\s*\n?\s*```")
_TOOL_TEXT_RE = re.compile(
    r"(?:Tool|tool|Function|function)\s*[:：]\s*[\"']?(\w+)[\"']?\s*\n\s*(?:Arguments|arguments|Input|input|Params|params)\s*[:：]\s*(\{[\s\S]*?\})\s*$",
    re.MULTILINE,
)


def extract_balanced_json(text: str, start: int = 0) -> str | None:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start: index + 1]
        elif char == ")" and depth <= 2:
            return text[start:index] + "}" * depth
        elif char == "]" and depth == 1:
            return text[start:index] + "}"
    if 1 <= depth <= 3:
        return text[start:].rstrip().rstrip(",") + "}" * depth
    return None


def repair_json(raw: str) -> str:
    value = raw.strip()
    value = re.sub(r",\s*([}\]])", r"\1", value)
    value = re.sub(r"\)\s*$", "}", value)
    value = re.sub(r'\\(?!")', r"\\\\", value)
    if "'" in value and not re.search(r'"name"', value):
        value = value.replace("'", '"')
    value = re.sub(r"\{(\s*)(\w+)\s*:", r'{\1"\2":', value)
    value = re.sub(r",(\s*)(\w+)\s*:", r',\1"\2":', value)
    last = value.rfind("}")
    if last != -1 and last < len(value) - 1:
        value = value[: last + 1]
    return value


def try_parse_json(raw: str) -> dict[str, Any] | None:
    for candidate in [raw, repair_json(raw), raw.replace(")", "}"), repair_json(raw.replace(")", "}"))]:
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            continue
    return None


def try_parse_tool_calls(text: str) -> list[dict[str, Any]] | None:
    matches: list[tuple[int, int, dict[str, Any]]] = []

    def _overlaps(pos: int) -> bool:
        return any(s <= pos < e for s, e, _ in matches)

    for m in _TOOL_BLOCK_RE.finditer(text):
        candidate = extract_balanced_json(m.group(1).strip()) or m.group(1).strip()
        parsed = try_parse_json(candidate)
        if parsed and parsed.get("name"):
            matches.append((m.start(), m.end(), parsed))

    for m in re.finditer(r'\{\s*"name"\s*:', text):
        if _overlaps(m.start()):
            continue
        candidate = extract_balanced_json(text, m.start())
        if candidate:
            parsed = try_parse_json(candidate)
            if parsed and parsed.get("name"):
                matches.append((m.start(), m.start() + len(candidate), parsed))

    if not matches:
        for m in _TOOL_TEXT_RE.finditer(text):
            args = try_parse_json(m.group(2))
            if args is not None:
                matches.append((m.start(), m.end(), {"name": m.group(1), "arguments": args}))

    if not matches:
        return None

    matches.sort(key=lambda x: x[0])
    return [
        {
            "index": i,
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": str(tool["name"]),
                "arguments": json.dumps(
                    tool["arguments"] if isinstance(tool.get("arguments"), dict) else {},
                    ensure_ascii=False,
                ),
            },
        }
        for i, (_, _, tool) in enumerate(matches)
    ]


def _extract_text(message: dict[str, Any]) -> str:
    """Extract plain text from a message's content field."""
    content = message.get("content", "")
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(content)
    
def _compact_arguments(arguments: Any) -> str:
    if arguments is None or arguments == "":
        return ""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments.strip()
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))


def _tool_label(name: str, arguments: str) -> str:
    if not arguments or arguments == "{}":
        return name
    if len(arguments) > 100:
        arguments = arguments[:100] + "..."
    return f"{name} {arguments}"


def _tool_call_label_by_id(message: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        fn = call.get("function") or {}
        if isinstance(fn, dict) and call_id and fn.get("name"):
            name = str(fn["name"])
            arguments = _compact_arguments(fn.get("arguments"))
            labels[str(call_id)] = _tool_label(name, arguments)
    return labels


def _tool_call_id_from_message(message: dict[str, Any]) -> str | None:
    tool_call_id = message.get("tool_call_id") or message.get("toolCallId")
    if tool_call_id:
        return str(tool_call_id)

    text = _extract_text(message).strip()
    if not text:
        return None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None

    for key in ("tool_call_id", "toolCallId", "call_id", "callId"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _tool_result_label(message: dict[str, Any], tool_call_labels: dict[str, str] | None = None) -> str:
    if message.get("name"):
        return str(message["name"])
    tool_call_id = _tool_call_id_from_message(message)
    if tool_call_id and tool_call_labels and str(tool_call_id) in tool_call_labels:
        return tool_call_labels[str(tool_call_id)]
    return str(tool_call_id or "unknown")

def inline_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge assistant tool_calls + tool/function result messages into inline text.

    Converts the multi-message OpenAI tool pattern:
      assistant(tool_calls=[...]) → tool(content=...) → ...
    Into a single user message:
      [Tool result from tool_name]: ...
      Please continue based on the tool results above.
    """
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        # Detect assistant message with tool_calls
        if role == "assistant" and msg.get("tool_calls"):
            tool_parts: list[str] = []
            tool_call_labels = _tool_call_label_by_id(msg)
            # Collect following tool/function result messages
            j = i + 1
            while j < len(messages) and messages[j].get("role") in {"tool", "function"}:
                tmsg = messages[j]
                label = _tool_result_label(tmsg, tool_call_labels)
                tool_parts.append(f"[Tool result from {label}]: {_extract_text(tmsg)}")
                j += 1
            tool_parts.append("Please continue based on the tool results above.")
            # Discard the assistant's text content — it's the ```json tool call
            # and the tool results already provide the continuation context.
            # Only keep it if it contains non-tool-call text (unlikely but safe).
            result.append({"role": "user", "content": "\n".join(tool_parts), "_is_tool_result_inline": True})
            i = j
            continue

        # Standalone tool/function message (without preceding assistant tool_calls)
        if role in {"tool", "function"}:
            label = _tool_result_label(msg)
            result.append({"role": "user", "content": f"[Tool result from {label}]: {_extract_text(msg)}\n\nPlease continue based on the tool results above.", "_is_tool_result_inline": True})
            i += 1
            continue

        result.append(msg)
        i += 1

    return result
