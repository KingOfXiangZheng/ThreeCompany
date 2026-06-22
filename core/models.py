"""ChatGPT Web model registry."""
from __future__ import annotations

from typing import Any


DEFAULT_MODEL = "gpt-5-3"


CHATGPT_WEB_MODELS: list[dict[str, Any]] = [
    {
        "id": "gpt-5-3",
        "name": "GPT-5.3",
        "max_tokens": 34834,
        "context_window": 34834,
        "reasoning_type": "auto",
        "reasoning": False,
        "enabled_tools": ["tools", "tools2", "dalle_3", "search", "canvas"],
    },
    {
        "id": "gpt-5-2",
        "name": "GPT-5.2",
        "max_tokens": 25384,
        "context_window": 25384,
        "reasoning_type": "auto",
        "reasoning": False,
        "enabled_tools": ["tools", "tools2", "dalle_3", "search", "canvas"],
    },
    {
        "id": "gpt-5-1",
        "name": "GPT-5.1",
        "max_tokens": 35815,
        "context_window": 35815,
        "reasoning_type": "auto",
        "reasoning": False,
        "enabled_tools": ["tools", "tools2", "dalle_3", "search", "canvas"],
    },
    {
        "id": "gpt-5",
        "name": "GPT-5",
        "max_tokens": 34815,
        "context_window": 34815,
        "reasoning_type": "auto",
        "reasoning": False,
        "enabled_tools": ["tools", "tools2", "dalle_3", "search", "canvas"],
    },
    {
        "id": "gpt-5-mini",
        "name": "GPT-5-mini",
        "max_tokens": 32767,
        "context_window": 32767,
        "reasoning_type": "none",
        "reasoning": False,
        "enabled_tools": ["tools", "tools2", "dalle_3", "search", "canvas"],
    },
    {
        "id": "gpt-5-3-mini",
        "name": "GPT-5.3 Mini",
        "max_tokens": 34834,
        "context_window": 34834,
        "reasoning_type": "none",
        "reasoning": False,
        "enabled_tools": ["tools", "tools2", "dalle_3", "search", "canvas"],
    },
    {
        "id": "gpt-5-5-thinking",
        "name": "GPT-5.5 Thinking",
        "max_tokens": 262144,
        "context_window": 262144,
        "reasoning_type": "reasoning",
        "reasoning": True,
        "configurable_thinking_effort": True,
        "enabled_tools": ["tools", "tools2", "dalle_3", "search", "canvas"],
    },
    {
        "id": "auto",
        "name": "Auto",
        "max_tokens": 34834,
        "context_window": 34834,
        "reasoning_type": "auto",
        "reasoning": False,
        "enabled_tools": ["tools", "tools2", "dalle_3", "search", "canvas"],
    },
]


GEMINI_WEB_MODELS: list[dict[str, Any]] = [
    {
        "id": "gemini-3-pro",
        "name": "Gemini 3 Pro (Web)",
        "max_tokens": 32768,
        "context_window": 1048576,
        "reasoning_type": "auto",
        "reasoning": True,
        "enabled_tools": [],
    },
    {
        "id": "gemini-3-thinking",
        "name": "Gemini 3 Thinking (Web)",
        "max_tokens": 32768,
        "context_window": 1048576,
        "reasoning_type": "reasoning",
        "reasoning": True,
        "enabled_tools": [],
    },
    {
        "id": "gemini-3-fast",
        "name": "Gemini 3 Fast (Web)",
        "max_tokens": 32768,
        "context_window": 1048576,
        "reasoning_type": "none",
        "reasoning": False,
        "enabled_tools": [],
    },
]


CLAUDE_WEB_MODELS: list[dict[str, Any]] = [
    {
        "id": "claude-sonnet-4-6",
        "name": "Claude Sonnet 4.6 (Web)",
        "max_tokens": 32768,
        "context_window": 200000,
        "reasoning_type": "auto",
        "reasoning": True,
        "enabled_tools": [],
    },
    {
        "id": "claude-opus-4-5",
        "name": "Claude Opus 4.5 (Web)",
        "max_tokens": 32768,
        "context_window": 200000,
        "reasoning_type": "auto",
        "reasoning": True,
        "enabled_tools": [],
    },
]


GITLAB_WEB_MODELS: list[dict[str, Any]] = [
    {"id": "claude_sonnet_4_6", "name": "GitLab Duo Claude Sonnet 4.6", "max_tokens": 64000, "context_window": 200000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "claude_sonnet_4_5_20250929", "name": "GitLab Duo Claude Sonnet 4.5", "max_tokens": 64000, "context_window": 200000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "claude_sonnet_4_20250514", "name": "GitLab Duo Claude Sonnet 4.0", "max_tokens": 64000, "context_window": 200000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "claude_opus_4_8", "name": "GitLab Duo Claude Opus 4.8", "max_tokens": 64000, "context_window": 200000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "claude_opus_4_7", "name": "GitLab Duo Claude Opus 4.7", "max_tokens": 64000, "context_window": 200000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "claude_opus_4_6_20260205", "name": "GitLab Duo Claude Opus 4.6", "max_tokens": 64000, "context_window": 200000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "claude_opus_4_5_20251101", "name": "GitLab Duo Claude Opus 4.5", "max_tokens": 64000, "context_window": 200000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "claude_haiku_4_5_20251001", "name": "GitLab Duo Claude Haiku 4.5", "max_tokens": 8192, "context_window": 200000, "reasoning_type": "none", "reasoning": False, "enabled_tools": []},
    {"id": "claude_fable_5", "name": "GitLab Duo Claude Fable 5", "max_tokens": 64000, "context_window": 200000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "gpt_5", "name": "GitLab Duo GPT-5.1", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "gpt_5_2", "name": "GitLab Duo GPT-5.2", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "gpt_5_codex", "name": "GitLab Duo GPT-5-Codex", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "gpt_5_2_codex", "name": "GitLab Duo GPT-5.2-Codex", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "gpt_5_3_codex", "name": "GitLab Duo GPT-5.3-Codex", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "gpt_5_mini", "name": "GitLab Duo GPT-5-Mini", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "none", "reasoning": False, "enabled_tools": []},
    {"id": "gpt_5_4", "name": "GitLab Duo GPT-5.4", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "gpt_5_4_mini", "name": "GitLab Duo GPT-5.4-Mini", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "none", "reasoning": False, "enabled_tools": []},
    {"id": "gpt_5_4_nano", "name": "GitLab Duo GPT-5.4-Nano", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "none", "reasoning": False, "enabled_tools": []},
    {"id": "gpt_5_5", "name": "GitLab Duo GPT-5.5", "max_tokens": 16384, "context_window": 128000, "reasoning_type": "auto", "reasoning": True, "enabled_tools": []},
    {"id": "gemini_3_5_flash_vertex", "name": "GitLab Duo Gemini 3.5 Flash", "max_tokens": 8192, "context_window": 1000000, "reasoning_type": "none", "reasoning": False, "enabled_tools": []},
]

GROK_WEB_MODELS: list[dict[str, Any]] = [
    {
        "id": "grok-fast",
        "name": "Grok Fast (Web)",
        "max_tokens": 32768,
        "context_window": 131072,
        "reasoning_type": "none",
        "reasoning": False,
        "enabled_tools": [],
    },
    {
        "id": "grok-auto",
        "name": "Grok Auto (Web)",
        "max_tokens": 32768,
        "context_window": 131072,
        "reasoning_type": "auto",
        "reasoning": True,
        "enabled_tools": [],
    },
    {
        "id": "grok-expert",
        "name": "Grok Expert (Web)",
        "max_tokens": 32768,
        "context_window": 131072,
        "reasoning_type": "reasoning",
        "reasoning": True,
        "enabled_tools": [],
    },
    {
        "id": "grok-heavy",
        "name": "Grok Heavy (Web)",
        "max_tokens": 32768,
        "context_window": 131072,
        "reasoning_type": "reasoning",
        "reasoning": True,
        "enabled_tools": [],
    },
]


MODEL_ALIASES = {
    # GPT-4 compatibility aliases → GPT-5.3
    "gpt-4": DEFAULT_MODEL,
    "gpt-4o": DEFAULT_MODEL,
    "chatgpt-4o-latest": DEFAULT_MODEL,
    # GPT-5.5 → Thinking mode (confirmed via DOM data-message-model-slug)
    "gpt5.5": "gpt-5-5-thinking",
    "gpt-5.5": "gpt-5-5-thinking",
    "gpt5.5-think": "gpt-5-5-thinking",
    "gpt5.5-thinking": "gpt-5-5-thinking",
    "gpt-5.5-think": "gpt-5-5-thinking",
    "gpt-5.5-thinking": "gpt-5-5-thinking",
    "gpt-5-5-think": "gpt-5-5-thinking",
    # GPT-5.4 → also thinking mode
    "gpt5.4": "gpt-5-5-thinking",
    "gpt-5.4": "gpt-5-5-thinking",
    "gpt5.4-think": "gpt-5-5-thinking",
    "gpt5.4-thinking": "gpt-5-5-thinking",
    "gpt-5.4-think": "gpt-5-5-thinking",
    "gpt-5.4-thinking": "gpt-5-5-thinking",
    "gpt-5-4-thinking": "gpt-5-5-thinking",
    "gpt-5-4-t-mini": "gpt-5-3-mini",
    # GPT-5.2 thinking aliases → gpt-5-2 (reasoning_type=auto handles thinking)
    "gpt5.2-think": "gpt-5-2",
    "gpt5.2-thinking": "gpt-5-2",
    "gpt-5.2-think": "gpt-5-2",
    "gpt-5.2-thinking": "gpt-5-2",
    "gpt-5-2-think": "gpt-5-2",
    "gpt-5-2-thinking": "gpt-5-2",
    # GPT-5.3 instant alias → gpt-5-3 (same model, just UI toggle)
    "gpt-5-3-instant": "gpt-5-3",
    # GPT-5.2 instant alias → gpt-5-2
    "gpt-5-2-instant": "gpt-5-2",
    "gemini-pro": "gemini-3-pro",
    "gemini-2.5-pro": "gemini-3-pro",
    "gemini-3": "gemini-3-pro",
    "gemini-3-pro": "gemini-3-pro",
    "gemini-thinking": "gemini-3-thinking",
    "gemini-3-thinking": "gemini-3-thinking",
    "gemini-flash": "gemini-3-fast",
    "gemini-fast": "gemini-3-fast",
    "gemini-3-flash": "gemini-3-fast",
    "gemini-3-fast": "gemini-3-fast",
    "claude": "claude-sonnet-4-6",
    "claude-web": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus": "claude-opus-4-5",
    "claude-opus-4": "claude-opus-4-5",
    "claude-opus-4-5": "claude-opus-4-5",
    "grok": "grok-fast",
    "grok-web": "grok-fast",
    "grok-fast": "grok-fast",
    "grok-3": "grok-fast",
    "grok-3-fast": "grok-fast",
    "grok-4-mini": "grok-fast",
    "grok-auto": "grok-auto",
    "grok-expert": "grok-expert",
    "grok-thinking": "grok-expert",
"grok-heavy": "grok-heavy",
    "gitlab": "claude_sonnet_4_6",
    "gitlab-duo": "claude_sonnet_4_6",
    "gitlab-chat": "claude_sonnet_4_6",
    "duo": "claude_sonnet_4_6",
    "duo-chat": "claude_sonnet_4_6",
    "sonnet": "claude_sonnet_4_6",
    "sonnet-4-6": "claude_sonnet_4_6",
    "sonnet-4-5": "claude_sonnet_4_5_20250929",
    "sonnet-4": "claude_sonnet_4_20250514",
    "opus": "claude_opus_4_7",
    "opus-4-8": "claude_opus_4_8",
    "opus-4-7": "claude_opus_4_7",
    "opus-4-6": "claude_opus_4_6_20260205",
    "opus-4-5": "claude_opus_4_5_20251101",
    "haiku": "claude_haiku_4_5_20251001",
    "haiku-4-5": "claude_haiku_4_5_20251001",
    "fable": "claude_fable_5",
    "fable-5": "claude_fable_5",
    "gpt5": "gpt_5_2",
    "gpt-5": "gpt_5_2",
    "gpt-5-1": "gpt_5",
    "gpt-5-2": "gpt_5_2",
    "gpt-5-mini": "gpt_5_mini",
    "gpt-5-codex": "gpt_5_codex",
    "gpt-5-4": "gpt_5_4",
    "gpt-5-5": "gpt_5_5",
    "duo-chat-sonnet-4-6": "claude_sonnet_4_6",
    "duo-chat-opus-4-6": "claude_opus_4_6_20260205",
    "duo-chat-opus-4-5": "claude_opus_4_5_20251101",
    "duo-chat-sonnet-4-5": "claude_sonnet_4_5_20250929",
    "duo-chat-sonnet-4": "claude_sonnet_4_20250514",
    "duo-chat-haiku-4-5": "claude_haiku_4_5_20251001",
    "duo-chat-gpt-5-1": "gpt_5",
    "duo-chat-gpt-5-2": "gpt_5_2",
    "duo-chat-gpt-5-mini": "gpt_5_mini",
}


def model_ids() -> set[str]:
    return {
        normalize_model(model["id"])
        for model in [*CHATGPT_WEB_MODELS, *GEMINI_WEB_MODELS, *CLAUDE_WEB_MODELS, *GROK_WEB_MODELS, *GITLAB_WEB_MODELS]
    }


def normalize_model(model: str | None) -> str:
    if not model:
        return DEFAULT_MODEL
    normalized = model.strip().lower().replace("_", "-").replace(" ", "-")
    return MODEL_ALIASES.get(normalized, normalized)


def is_gemini_model(model: str | None) -> bool:
    return normalize_model(model).startswith("gemini-")


def is_claude_model(model: str | None) -> bool:
    return normalize_model(model).startswith("claude-")


def is_grok_model(model: str | None) -> bool:
    return normalize_model(model).startswith("grok-")


_GITLAB_MODEL_IDS: set[str] = {m["id"] for m in GITLAB_WEB_MODELS}


def is_gitlab_model(model: str | None) -> bool:
    if not model:
        return False
    raw = model.strip().lower().replace(" ", "-")
    if raw in _GITLAB_MODEL_IDS:
        return True
    normalized = normalize_model(model)
    if normalized in _GITLAB_MODEL_IDS:
        return True
    if normalized.startswith("duo-chat-") or normalized.startswith("gitlab-"):
        return True
    return False


def openai_model_list() -> list[dict[str, Any]]:
    return [
        {
            "id": model["id"],
            "object": "model",
            "created": 1700000000,
            "owned_by": "chatgpt-web",
            "metadata": {
                "name": model["name"],
                "max_tokens": model["max_tokens"],
                "context_window": model["context_window"],
                "reasoning_type": model["reasoning_type"],
                "reasoning": model["reasoning"],
                "enabled_tools": model["enabled_tools"],
            },
        }
        for model in [*CHATGPT_WEB_MODELS, *GEMINI_WEB_MODELS, *CLAUDE_WEB_MODELS, *GROK_WEB_MODELS, *GITLAB_WEB_MODELS]
    ]
