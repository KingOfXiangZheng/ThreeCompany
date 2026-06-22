#!/usr/bin/env python3
"""GitLab Duo Chat – GraphQL + WebSocket reverse client.

Auth flow:
  1. PAT or session cookie auth → GraphQL mutation to create workflow
  2. WebSocket connection to /api/v4/ai/duo_workflows/ws for streaming
  3. GraphQL query to poll for responses

Based on browser packet capture of real GitLab Duo Chat (2025-06).

Architecture discovered:
  - Create workflow: mutation createAiDuoWorkflow
  - WebSocket URL: wss://gitlab.com/api/v4/ai/duo_workflows/ws?...
  - Model selection via currentModel param in WS URL
  - Message sent via WebSocket startRequest
  - Response polled via getWorkflowLatestCheckpoint query
"""
from __future__ import annotations

import argparse
import asyncio
import json
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import requests

try:
    import curl_cffi.requests as curl_requests
except Exception:
    curl_requests = None

try:
    import websockets
except ImportError:
    websockets = None

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"

GITLAB_COM_URL = "https://gitlab.com"

DEFAULT_CONFIG: dict[str, Any] = {
    "api_base": GITLAB_COM_URL,
    "timeout_seconds": 120,
    "default_model": "claude_sonnet_4_6",
    "transport": "curl_cffi",
    "namespace_id": None,
}



MODEL_MAPPINGS: dict[str, dict[str, Any]] = {
    
    "claude_haiku_4_5_20251001": {
        "provider": "anthropic", "display": "Claude Haiku 4.5",
        "model": "claude_haiku_4_5_20251001", "reasoning": False,
        "context_window": 200000, "max_tokens": 8192,
    },
    "claude_sonnet_4_20250514": {
        "provider": "anthropic", "display": "Claude Sonnet 4.0",
        "model": "claude_sonnet_4_20250514", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_sonnet_4_5_20250929": {
        "provider": "anthropic", "display": "Claude Sonnet 4.5",
        "model": "claude_sonnet_4_5_20250929", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_sonnet_4_6": {
        "provider": "anthropic", "display": "Claude Sonnet 4.6",
        "model": "claude_sonnet_4_6", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_5_20251101": {
        "provider": "anthropic", "display": "Claude Opus 4.5",
        "model": "claude_opus_4_5_20251101", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_6_20260205": {
        "provider": "anthropic", "display": "Claude Opus 4.6",
        "model": "claude_opus_4_6_20260205", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_7": {
        "provider": "anthropic", "display": "Claude Opus 4.7",
        "model": "claude_opus_4_7", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_8": {
        "provider": "anthropic", "display": "Claude Opus 4.8",
        "model": "claude_opus_4_8", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_fable_5": {
        "provider": "anthropic", "display": "Claude Fable 5",
        "model": "claude_fable_5", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    
    "claude_haiku_4_5_20251001_bedrock": {
        "provider": "anthropic", "display": "Claude Haiku 4.5 (Bedrock)",
        "model": "claude_haiku_4_5_20251001_bedrock", "reasoning": False,
        "context_window": 200000, "max_tokens": 8192,
    },
    "claude_sonnet_4_20250514_bedrock": {
        "provider": "anthropic", "display": "Claude Sonnet 4.0 (Bedrock)",
        "model": "claude_sonnet_4_20250514_bedrock", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_sonnet_4_5_20250929_bedrock": {
        "provider": "anthropic", "display": "Claude Sonnet 4.5 (Bedrock)",
        "model": "claude_sonnet_4_5_20250929_bedrock", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_sonnet_4_6_bedrock": {
        "provider": "anthropic", "display": "Claude Sonnet 4.6 (Bedrock)",
        "model": "claude_sonnet_4_6_bedrock", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_6_bedrock": {
        "provider": "anthropic", "display": "Claude Opus 4.6 (Bedrock)",
        "model": "claude_opus_4_6_bedrock", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_7_bedrock": {
        "provider": "anthropic", "display": "Claude Opus 4.7 (Bedrock)",
        "model": "claude_opus_4_7_bedrock", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_8_bedrock": {
        "provider": "anthropic", "display": "Claude Opus 4.8 (Bedrock)",
        "model": "claude_opus_4_8_bedrock", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    
    "claude_haiku_4_5_20251001_vertex": {
        "provider": "anthropic", "display": "Claude Haiku 4.5 (Vertex)",
        "model": "claude_haiku_4_5_20251001_vertex", "reasoning": False,
        "context_window": 200000, "max_tokens": 8192,
    },
    "claude_sonnet_4_20250514_vertex": {
        "provider": "anthropic", "display": "Claude Sonnet 4.0 (Vertex)",
        "model": "claude_sonnet_4_20250514_vertex", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_sonnet_4_5_20250929_vertex": {
        "provider": "anthropic", "display": "Claude Sonnet 4.5 (Vertex)",
        "model": "claude_sonnet_4_5_20250929_vertex", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_5_20251101_vertex": {
        "provider": "anthropic", "display": "Claude Opus 4.5 (Vertex)",
        "model": "claude_opus_4_5_20251101_vertex", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_6_vertex": {
        "provider": "anthropic", "display": "Claude Opus 4.6 (Vertex)",
        "model": "claude_opus_4_6_vertex", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_7_vertex": {
        "provider": "anthropic", "display": "Claude Opus 4.7 (Vertex)",
        "model": "claude_opus_4_7_vertex", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    "claude_opus_4_8_vertex": {
        "provider": "anthropic", "display": "Claude Opus 4.8 (Vertex)",
        "model": "claude_opus_4_8_vertex", "reasoning": True,
        "context_window": 200000, "max_tokens": 64000,
    },
    
    "gemini_3_5_flash_vertex": {
        "provider": "google", "display": "Gemini 3.5 Flash",
        "model": "gemini_3_5_flash_vertex", "reasoning": False,
        "context_window": 1000000, "max_tokens": 8192,
    },
    
    "gpt_5": {
        "provider": "openai", "display": "GPT-5.1",
        "model": "gpt_5", "reasoning": True,
        "context_window": 128000, "max_tokens": 16384,
    },
    "gpt_5_codex": {
        "provider": "openai", "display": "GPT-5-Codex",
        "model": "gpt_5_codex", "reasoning": True,
        "context_window": 128000, "max_tokens": 16384,
    },
    "gpt_5_2_codex": {
        "provider": "openai", "display": "GPT-5.2-Codex",
        "model": "gpt_5_2_codex", "reasoning": True,
        "context_window": 128000, "max_tokens": 16384,
    },
    "gpt_5_3_codex": {
        "provider": "openai", "display": "GPT-5.3-Codex",
        "model": "gpt_5_3_codex", "reasoning": True,
        "context_window": 128000, "max_tokens": 16384,
    },
    "gpt_5_mini": {
        "provider": "openai", "display": "GPT-5-Mini",
        "model": "gpt_5_mini", "reasoning": False,
        "context_window": 128000, "max_tokens": 16384,
    },
    "gpt_5_2": {
        "provider": "openai", "display": "GPT-5.2",
        "model": "gpt_5_2", "reasoning": True,
        "context_window": 128000, "max_tokens": 16384,
    },
    "gpt_5_4": {
        "provider": "openai", "display": "GPT-5.4",
        "model": "gpt_5_4", "reasoning": True,
        "context_window": 128000, "max_tokens": 16384,
    },
    "gpt_5_4_mini": {
        "provider": "openai", "display": "GPT-5.4-Mini",
        "model": "gpt_5_4_mini", "reasoning": False,
        "context_window": 128000, "max_tokens": 16384,
    },
    "gpt_5_4_nano": {
        "provider": "openai", "display": "GPT-5.4-Nano",
        "model": "gpt_5_4_nano", "reasoning": False,
        "context_window": 128000, "max_tokens": 16384,
    },
    "gpt_5_5": {
        "provider": "openai", "display": "GPT-5.5",
        "model": "gpt_5_5", "reasoning": True,
        "context_window": 128000, "max_tokens": 16384,
    },
}

MODEL_ALIASES: dict[str, str] = {
    # Default
    "gitlab": "claude_sonnet_4_6",
    "gitlab-duo": "claude_sonnet_4_6",
    "gitlab-chat": "claude_sonnet_4_6",
    "duo": "claude_sonnet_4_6",
    "duo-chat": "claude_sonnet_4_6",
    # Anthropic direct
    "sonnet": "claude_sonnet_4_6",
    "sonnet-4.6": "claude_sonnet_4_6",
    "sonnet-4.5": "claude_sonnet_4_5_20250929",
    "sonnet-4.0": "claude_sonnet_4_20250514",
    "opus": "claude_opus_4_7",
    "opus-4.8": "claude_opus_4_8",
    "opus-4.7": "claude_opus_4_7",
    "opus-4.6": "claude_opus_4_6_20260205",
    "opus-4.5": "claude_opus_4_5_20251101",
    "haiku": "claude_haiku_4_5_20251001",
    "haiku-4.5": "claude_haiku_4_5_20251001",
    "fable": "claude_fable_5",
    "fable-5": "claude_fable_5",
    # OpenAI
    "gpt5": "gpt_5_2",
    "gpt-5": "gpt_5_2",
    "gpt-5.1": "gpt_5",
    "gpt-5.2": "gpt_5_2",
    "gpt-5-mini": "gpt_5_mini",
    "gpt-5-codex": "gpt_5_codex",
    "gpt-5.4": "gpt_5_4",
    "gpt-5.5": "gpt_5_5",
    # Legacy compat (old duo-chat-* names → real IDs)
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


@dataclass
class GitLabStreamEvent:
    delta: str = ""
    conversation_id: str | None = None
    response_id: str | None = None


class GitLabHTTPStatusError(RuntimeError):
    def __init__(self, status_code: int, body: str = ""):
        super().__init__(f"GitLab request failed: status={status_code}, body={body[:800]}")
        self.status_code = status_code
        self.body = body


def safe_print(message: str, end: str = "\n") -> None:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = str(message).encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, end=end, flush=True)


def _read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(f"Missing GitLab config file: {path}")
    with path.open("r", encoding="utf-8-sig") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError(f"GitLab config must be a JSON object: {path}")
    return value


def load_config() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    cookies = _read_json(CONFIG_DIR / "cookies.json")
    headers = _read_json(CONFIG_DIR / "headers.json", {})
    config = _read_json(CONFIG_DIR / "config.json", {})
    for key, value in DEFAULT_CONFIG.items():
        config.setdefault(key, value)
    return cookies, headers, config


def normalize_gitlab_model(model: str | None) -> str:
    key = (model or "claude_sonnet_4_6").strip().lower().replace("_", "-").replace(" ", "-")
    return MODEL_ALIASES.get(key, key.replace("-", "_"))


def get_model_mapping(model_id: str) -> dict[str, Any]:
    direct = MODEL_MAPPINGS.get(model_id)
    if direct:
        return direct
    # Try dash-separated variant
    alt = model_id.replace("_", "-")
    for _key, mapping in MODEL_MAPPINGS.items():
        if mapping["model"] == alt or mapping["model"] == model_id:
            return mapping
    return MODEL_MAPPINGS["claude_sonnet_4_6"]


def _system_proxy_url() -> str:
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        value = __import__("os").environ.get(key)
        if value:
            return value
    return ""


def _make_http_session(proxy_url: str = "", transport: str = "curl_cffi") -> requests.Session | _CurlCffiStatelessSession:
    if transport == "curl_cffi" and curl_requests is not None:
        return _CurlCffiStatelessSession(proxy_url)
    session = requests.Session()
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


class _CurlCffiStatelessSession:
    def __init__(self, proxy_url: str):
        self.headers: dict[str, str] = {}
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
        self._proxy_url = proxy_url

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}) or {})
        if self._proxy_url and "proxy" not in kwargs:
            kwargs["proxy"] = self._proxy_url
        kwargs["headers"] = headers
        impersonate = kwargs.pop("impersonate", None) or "chrome"
        for attempt in range(3):
            try:
                return curl_requests.request(method, url, impersonate=impersonate, **kwargs)
            except Exception:
                if attempt >= 2:
                    raise
                time.sleep(0.25 * (attempt + 1))
        raise RuntimeError("curl_cffi request failed")

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._request("POST", url, **kwargs)


def get_pat(cookies: dict[str, Any]) -> str:
    pat = cookies.get("pat") or cookies.get("personal_access_token") or cookies.get("token") or ""
    if not pat:
        raise ValueError(
            "GitLab config/cookies.json must contain a 'pat' key with a valid "
            "GitLab Personal Access Token (scopes: api, ai_features). "
            "Create one at: https://gitlab.com/-/user_settings/personal_access_tokens"
        )
    return str(pat).strip()


def _get_session_cookie(cookies: dict[str, Any]) -> str | None:
    """Try to get a _gitlab_session cookie value."""
    return cookies.get("_gitlab_session") or cookies.get("session") or None


def _get_csrf_token(cookies: dict[str, Any]) -> str | None:
    """Try to get a CSRF token from cookies or headers."""
    return cookies.get("csrf_token") or cookies.get("x_csrf_token") or None




CREATE_WORKFLOW_MUTATION = """mutation createAiDuoWorkflow($projectId: ProjectID, $namespaceId: NamespaceID, $goal: String!, $workflowDefinition: String!, $agentPrivileges: [Int!], $preApprovedAgentPrivileges: [Int!], $allowAgentToRequestUser: Boolean, $aiCatalogItemVersionId: AiCatalogItemVersionID) {
  aiDuoWorkflowCreate(
    input: {projectId: $projectId, namespaceId: $namespaceId, environment: WEB, goal: $goal, workflowDefinition: $workflowDefinition, agentPrivileges: $agentPrivileges, preApprovedAgentPrivileges: $preApprovedAgentPrivileges, allowAgentToRequestUser: $allowAgentToRequestUser, aiCatalogItemVersionId: $aiCatalogItemVersionId}
  ) {
    workflow { id __typename }
    errors
    __typename
  }
}"""

GET_WORKFLOW_CHECKPOINT_QUERY = """query getWorkflowLatestCheckpoint($workflowId: AiDuoWorkflowsWorkflowID!) {
  duoWorkflowWorkflows(workflowId: $workflowId) {
    nodes {
      id
      status
      aiCatalogItemVersionId
      workflowDefinition
      archived
      stalled
      latestCheckpoint {
        workflowGoal
        workflowStatus
        errors
        duoMessages {
          content
          messageType
          messageSubType
          status
          toolInfo
          timestamp
          correlationId
          messageId
          role
          additionalContext { category id content metadata { icon title enabled subType pagePath projectPath subTypeLabel } __typename }
          __typename
        }
        __typename
      }
      __typename
    }
    __typename
  }
}"""


def _graphql_request(
    session: Any,
    api_base: str,
    csrf_token: str | None,
    pat: str | None,
    operation_name: str,
    query: str,
    variables: dict[str, Any],
    timeout: int = 60,
) -> dict[str, Any]:
    """Send a GraphQL request to GitLab API."""
    url = f"{api_base.rstrip('/')}/api/graphql"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-gitlab-version": "19.1.0-pre",
        "x-gitlab-feature-category": "duo_agent_platform",
    }
    if csrf_token:
        headers["x-csrf-token"] = csrf_token
    if pat:
        headers["Authorization"] = f"Bearer {pat}"

    body = json.dumps({
        "operationName": operation_name,
        "variables": variables,
        "query": query,
    })

    if isinstance(session, _CurlCffiStatelessSession):
        response = session.post(url, data=body, headers=headers, timeout=timeout)
        status = response.status_code if hasattr(response, "status_code") else getattr(response, "status", 0)
        text = response.text
    else:
        response = session.post(url, data=body, headers=headers, timeout=timeout)
        status = response.status_code
        text = response.text

    if status >= 400:
        raise GitLabHTTPStatusError(status, text)

    return json.loads(text)


def create_workflow(
    session: Any,
    api_base: str,
    pat: str | None,
    csrf_token: str | None,
    goal: str,
    namespace_id: str | None = None,
    timeout: int = 60,
) -> str:
    """Create a new Duo Workflow (chat session). Returns workflow GID."""
    variables: dict[str, Any] = {
        "goal": goal,
        "workflowDefinition": "chat",
        "agentPrivileges": [2, 3, 7],
        "preApprovedAgentPrivileges": [2],
    }
    if namespace_id:
        variables["namespaceId"] = namespace_id

    result = _graphql_request(
        session, api_base, csrf_token, pat,
        "createAiDuoWorkflow", CREATE_WORKFLOW_MUTATION, variables, timeout,
    )

    data = result.get("data", {}).get("aiDuoWorkflowCreate", {})
    errors = data.get("errors", [])
    if errors:
        raise RuntimeError(f"Workflow creation errors: {errors}")

    workflow_gid = data.get("workflow", {}).get("id", "")
    if not workflow_gid:
        raise RuntimeError(f"No workflow ID returned: {json.dumps(result, ensure_ascii=False)[:500]}")

    safe_print(f"[GitLab Duo] Created workflow: {workflow_gid}")
    return workflow_gid


def poll_workflow_messages(
    session: Any,
    api_base: str,
    pat: str | None,
    csrf_token: str | None,
    workflow_gid: str,
    timeout: int = 30,
) -> tuple[str, list[dict[str, Any]]]:
    result = _graphql_request(
        session, api_base, csrf_token, pat,
        "getWorkflowLatestCheckpoint", GET_WORKFLOW_CHECKPOINT_QUERY,
        {"workflowId": workflow_gid}, timeout,
    )

    nodes = (result.get("data", {})
             .get("duoWorkflowWorkflows", {})
             .get("nodes", []))

    if not nodes:
        return "", []

    node = nodes[0]
    workflow_status = node.get("status", "")
    checkpoint = node.get("latestCheckpoint", {})
    messages = checkpoint.get("duoMessages", [])
    return workflow_status, messages


def _extract_workflow_iid(workflow_gid: str) -> str | None:
    """Extract numeric ID from GID like 'gid://gitlab/Ai::DuoWorkflows::Workflow/4379658'."""
    if "/" in workflow_gid:
        return workflow_gid.rsplit("/", 1)[-1]
    return None


def _build_ws_url(
    api_base: str,
    workflow_id: str | None = None,
    namespace_id: str | None = None,
    current_model: str | None = None,
    default_model: str | None = None,
    root_namespace_id: str | None = None,
    project_id: str | None = None,
) -> str:
    ws_base = api_base.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    url = f"{ws_base}/api/v4/ai/duo_workflows/ws"

    params: list[str] = ["client_type=browser"]
    if root_namespace_id:
        params.append(f"rootNamespaceId={root_namespace_id}")
    if namespace_id:
        params.append(f"namespaceId={namespace_id}")
    if project_id:
        params.append(f"projectId={project_id}")
    params.append("userModelSelectionEnabled=true")
    if current_model:
        params.append(f"currentModel={current_model}")
    if default_model:
        params.append(f"defaultModel={default_model}")
    params.append("workflowDefinition=chat")
    if workflow_id:
        params.append(f"workflowId={workflow_id}")

    url += "?" + "&".join(params)
    return url




async def _ws_connect_and_stream(
    ws_url: str,
    cookie_header: str,
    origin: str,
    start_request: dict[str, Any],
    q: queue.Queue,
    timeout: int = 120,
) -> None:
    if websockets is None:
        q.put(RuntimeError("websockets package required: pip install websockets"))
        q.put(None)
        return

    ws_headers = {"Cookie": cookie_header, "Origin": origin}

    try:
        async with websockets.connect(ws_url, additional_headers=ws_headers, ping_interval=30) as ws:
            await ws.send(json.dumps(start_request))
            goal_text = start_request.get("startRequest", {}).get("goal", "")
            safe_print(f"[GitLab Duo] WS sent startRequest, goal={goal_text[:50]}...")

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                except websockets.exceptions.ConnectionClosed:
                    break

                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    if raw.strip():
                        q.put(("delta", raw))
                    continue

                if "newCheckpoint" in msg:
                    nc = msg["newCheckpoint"]
                    status = nc.get("status", "")
                    cp_raw = nc.get("checkpoint", "")
                    try:
                        cp = json.loads(cp_raw) if isinstance(cp_raw, str) else cp_raw
                    except json.JSONDecodeError:
                        cp = {}

                    chat_log = (cp.get("channel_values", {}) or {}).get("ui_chat_log", [])
                    agent_msgs = [e for e in chat_log if e.get("message_type") in ("agent", "assistant")]
                    if agent_msgs:
                        safe_print(f"[GitLab Duo] WS checkpoint status={status}, agent_msgs={len(agent_msgs)}, content_preview={agent_msgs[-1].get('content','')[:100]}")
                    else:
                        user_msgs = [e for e in chat_log if e.get("message_type") == "user"]
                        safe_print(f"[GitLab Duo] WS checkpoint status={status}, user_msgs={len(user_msgs)}, no agent msgs")

                    for entry in chat_log:
                        mtype = entry.get("message_type", "")
                        content = entry.get("content", "")
                        if mtype in ("agent", "assistant") and content:
                            q.put(("delta", content))

                    if status in ("INPUT_REQUIRED", "COMPLETE", "FINISHED", "FAILED"):
                        break
                    continue

                if "error" in msg:
                    q.put(("error", msg.get("error", {}).get("message", str(msg))))
                    break

                if "content" in msg:
                    c = msg["content"]
                    if isinstance(c, str) and c:
                        q.put(("delta", c))
                    elif isinstance(c, dict):
                        t = c.get("text", "")
                        if t:
                            q.put(("delta", t))

    except Exception as exc:
        q.put(("error", str(exc)))

    q.put(None)




def stream_completion(
    session: Any,
    config: dict[str, Any],
    pat: str,
    message: str,
    model: str | None = None,
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> Iterator[GitLabStreamEvent]:
    api_base = str(config.get("api_base", GITLAB_COM_URL)).rstrip("/")
    timeout = int(config.get("timeout_seconds", 120) or 120)
    namespace_id = config.get("namespace_id")

    model_id = normalize_gitlab_model(model)
    mapping = get_model_mapping(model_id)
    gitlab_model = mapping["model"]

    safe_print(f"[GitLab Duo] model={model_id} → {mapping['display']} ({gitlab_model})")

    cookies, _headers, _config = load_config()

    if conversation_id:
        ns_gid = None
        if namespace_id:
            ns_gid = namespace_id if namespace_id.startswith("gid://") else f"gid://gitlab/Group/{namespace_id}"

        workflow_gid = create_workflow(
            session, api_base, pat, None,
            goal=message, namespace_id=ns_gid, timeout=timeout,
        )
        safe_print(f"[GitLab Duo] Continuation: new workflow {workflow_gid}")
    else:
        ns_gid = None
        if namespace_id:
            ns_gid = namespace_id if namespace_id.startswith("gid://") else f"gid://gitlab/Group/{namespace_id}"

        workflow_gid = create_workflow(
            session, api_base, pat, None,
            goal=message, namespace_id=ns_gid, timeout=timeout,
        )

    workflow_iid = _extract_workflow_iid(workflow_gid) or ""

    ws_url = _build_ws_url(
        api_base, workflow_id=workflow_iid,
        current_model=gitlab_model, default_model=gitlab_model,
    )

    cookie_parts = [f"{k}={v}" for k, v in cookies.items() if isinstance(v, str)]
    cookie_header = "; ".join(cookie_parts)
    origin = _headers.get("origin", api_base)

    goal_text = message
    additional_ctx: list[dict[str, Any]] = []
    if messages and len(messages) > 1:
        history_parts = []
        for msg in messages[:-1]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            if content:
                prefix = "User" if role == "user" else "Assistant"
                history_parts.append(f"{prefix}: {content}")
        if history_parts:
            goal_text = f"Previous conversation:\n{chr(10).join(history_parts)}\n\nCurrent message:\n{message}"

    start_request: dict[str, Any] = {
        "startRequest": {
            "workflowID": workflow_iid,
            "clientVersion": "19.1.0",
            "workflowDefinition": "chat",
            "goal": goal_text,
            "approval": {},
            "useOrbit": True,
            "clientCapabilities": [],
        }
    }

    safe_print(f"[GitLab Duo] Connecting WebSocket: {ws_url[:80]}...")

    q: queue.Queue = queue.Queue()
    yielded_content: set[str] = set()

    def _run_ws():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                _ws_connect_and_stream(ws_url, cookie_header, origin, start_request, q, timeout)
            )
        finally:
            loop.close()

    t = threading.Thread(target=_run_ws, daemon=True)
    t.start()

    full_response = ""
    while True:
        item = q.get(timeout=timeout + 10)
        if item is None:
            break
        if isinstance(item, BaseException):
            raise item
        tag, payload = item
        if tag == "error":
            raise RuntimeError(f"GitLab Duo WebSocket error: {payload}")
        if tag == "delta" and payload:
            if payload not in yielded_content:
                yielded_content.add(payload)
                delta = payload[len(full_response):] if payload.startswith(full_response) else payload
                if delta:
                    full_response = payload
                    yield GitLabStreamEvent(delta=delta)

    yield GitLabStreamEvent(delta="", conversation_id=workflow_iid)


def print_result(events: Iterator[GitLabStreamEvent]) -> None:
    saw_text = False
    conversation_id = None
    response_id = None
    for event in events:
        if event.conversation_id:
            conversation_id = event.conversation_id
        if event.response_id:
            response_id = event.response_id
        if event.delta:
            saw_text = True
            safe_print(event.delta, end="")
    safe_print("")
    if conversation_id or response_id:
        safe_print(
            json.dumps(
                {"conversation_id": conversation_id, "response_id": response_id},
                ensure_ascii=False,
                indent=2,
            )
        )
    if not saw_text:
        safe_print("[GitLab Duo] no response received")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GitLab Duo Chat – GraphQL+WebSocket client")
    parser.add_argument("message", help="Message to send to GitLab Duo Chat")
    parser.add_argument("--model", default="claude_sonnet_4_6", help="Model ID or alias (default: claude_sonnet_4_6)")
    parser.add_argument("--namespace-id", default=None, help="GitLab namespace/group ID for Duo Chat")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cookies, headers, config = load_config()
    pat = get_pat(cookies)
    proxy_url = str(config.get("proxy") or _system_proxy_url() or "")
    transport = str(config.get("transport") or "curl_cffi").lower()
    session = _make_http_session(proxy_url, transport)

    user_agent = headers.get(
        "user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    )
    session.headers.update({"user-agent": user_agent})

    # Override namespace_id from config if not specified
    if args.namespace_id:
        config["namespace_id"] = args.namespace_id

    events = stream_completion(
        session,
        config,
        pat,
        args.message,
        args.model,
    )
    print_result(events)


if __name__ == "__main__":
    main()