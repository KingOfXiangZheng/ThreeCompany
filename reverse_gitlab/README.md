# GitLab Duo Chat Reverse

GraphQL + WebSocket GitLab Duo Chat client.

## How It Works

GitLab Duo Chat uses a **Workflow-based architecture** (not REST API):

1. **Create workflow**: GraphQL mutation `createAiDuoWorkflow` with `goal` = user message
2. **Poll for response**: GraphQL query `getWorkflowLatestCheckpoint` polls for agent messages
3. **WebSocket streaming**: `wss://gitlab.com/api/v4/ai/duo_workflows/ws` for real-time (optional)

Authentication uses **Personal Access Token (PAT)** with `api` scope.

## Authentication

### Required PAT scopes

- `api` or `ai_features`

### Prerequisites

- GitLab Duo subscription (Ultimate/Premium tier or Duo add-on)
- On GitLab.com: Works out of the box

### Configure your PAT

Create a GitLab PAT at: https://gitlab.com/-/user_settings/personal_access_tokens

Option A — via CLI:
```powershell
python core/refresh_browser_auth.py --target gitlab --gitlab-pat "glpat-xxxxxxxxxxxxx"
```

Option B — interactive:
```powershell
python core/refresh_browser_auth.py --target gitlab
```

Option C — manual:
```powershell
Copy-Item reverse_gitlab/config/cookies.example.json reverse_gitlab/config/cookies.json
```
Then edit `cookies.json` and set `"pat"` to your token.

## Usage

### Direct request

```powershell
python reverse_gitlab/main.py "Explain what Ruby classes are" --model claude_sonnet_4_6
```

### OpenAI-compatible API

```powershell
uv run app.py
```

```powershell
curl http://localhost:8000/v1/chat/completions `
  -H "Content-Type: application/json" `
  -H "Authorization: Bearer any" `
  -d '{"model": "claude_sonnet_4_6", "messages": [{"role": "user", "content": "What is GitLab CI?"}], "stream": true}'
```

## Supported Models

Real model IDs from browser capture (2025-06-12).

### Anthropic (direct)

| Model ID | Display Name | Context |
|----------|-------------|---------|
| `claude_sonnet_4_6` | Claude Sonnet 4.6 **(default)** | 200K |
| `claude_sonnet_4_5_20250929` | Claude Sonnet 4.5 | 200K |
| `claude_sonnet_4_20250514` | Claude Sonnet 4.0 | 200K |
| `claude_opus_4_8` | Claude Opus 4.8 | 200K |
| `claude_opus_4_7` | Claude Opus 4.7 | 200K |
| `claude_opus_4_6_20260205` | Claude Opus 4.6 | 200K |
| `claude_opus_4_5_20251101` | Claude Opus 4.5 | 200K |
| `claude_haiku_4_5_20251001` | Claude Haiku 4.5 | 200K |
| `claude_fable_5` | Claude Fable 5 | 200K |

### Anthropic via Bedrock / Vertex

Same model IDs with `_bedrock` or `_vertex` suffix, e.g. `claude_sonnet_4_6_bedrock`.

### OpenAI

| Model ID | Display Name | Context |
|----------|-------------|---------|
| `gpt_5` | GPT-5.1 | 128K |
| `gpt_5_2` | GPT-5.2 | 128K |
| `gpt_5_codex` | GPT-5-Codex | 128K |
| `gpt_5_2_codex` | GPT-5.2-Codex | 128K |
| `gpt_5_3_codex` | GPT-5.3-Codex | 128K |
| `gpt_5_mini` | GPT-5-Mini | 128K |
| `gpt_5_4` | GPT-5.4 | 128K |
| `gpt_5_4_mini` | GPT-5.4-Mini | 128K |
| `gpt_5_4_nano` | GPT-5.4-Nano | 128K |
| `gpt_5_5` | GPT-5.5 | 128K |

### Google

| Model ID | Display Name | Context |
|----------|-------------|---------|
| `gemini_3_5_flash_vertex` | Gemini 3.5 Flash | 1M |

### Aliases

| Alias | Resolves to |
|-------|------------|
| `gitlab` / `duo` / `duo-chat` | `claude_sonnet_4_6` |
| `sonnet` | `claude_sonnet_4_6` |
| `opus` | `claude_opus_4_7` |
| `haiku` | `claude_haiku_4_5_20251001` |
| `fable` | `claude_fable_5` |
| `gpt5` / `gpt-5` | `gpt_5_2` |
| `duo-chat-sonnet-4-6` (legacy) | `claude_sonnet_4_6` |

## Config

| File | Purpose |
|------|---------|
| `config/cookies.json` | Must contain `"pat": "glpat-..."` |
| `config/headers.json` | Optional custom headers |
| `config/config.json` | API base, default model, timeout, namespace_id |

## Architecture

```
User message
  → GraphQL mutation createAiDuoWorkflow (goal=message, workflowDefinition="chat")
  → Returns workflow GID: gid://gitlab/Ai::DuoWorkflows::Workflow/4379658
  → Poll getWorkflowLatestCheckpoint every 3s
  → Extract duoMessages where messageType="agent"
  → Stream delta text to client
```

The `namespace_id` in config.json is optional — if set, creates the workflow in that namespace.
