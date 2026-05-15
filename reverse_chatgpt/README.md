# ChatGPT Reverse

Pure HTTP ChatGPT Web protocol probe. This project does not use a browser,
Playwright, Chrome CDP, or `chatgpt_web`.

## Auth

Provide credentials yourself through environment variables:

```powershell
$env:CHATGPT_COOKIE="__Secure-next-auth.session-token=..."
# optional
$env:CHATGPT_ACCESS_TOKEN="..."
$env:CHATGPT_USER_AGENT="..."
```

The script will not export cookies from a browser.

Or run the local setup helper and paste the Cookie header yourself:

```powershell
python setup_auth.py
```

It writes `config/auth.local.json`, which is ignored by git.

## Usage

```powershell
python main.py --check
python main.py "hello"
python main.py "hello" --backend browser --attach-only
python main.py "continue" --conversation-id <id> --parent-message-id <id>
```

`--backend auto` is the default. It first probes the pure HTTP path and, when
`/backend-api/f/conversation` returns the known 403 risk-control boundary, falls
back to the repository's browser-backed `chatgpt_web` client. Use
`--backend http` to keep pure HTTP diagnostic behavior, or `--backend browser`
to go directly through the browser-backed path.

## Current HTTP protocol

The HTTP probe now uses the newer ChatGPT Web path:

```text
POST /backend-api/f/conversation
```

Before the conversation request it performs best-effort warmup calls observed
in the browser flow:

```text
POST /backend-api/f/conversation/prepare
POST /backend-api/sentinel/heartbeat
POST /backend-api/sentinel/chat-requirements/prepare
POST /backend-api/sentinel/chat-requirements/finalize
POST /backend-api/sentinel/req
```

Dynamic protocol headers can be provided through either
`config/protocol_headers.local.json` or the `CHATGPT_PROTOCOL_HEADERS`
environment variable. The value must be a JSON object, for example:

```json
{
  "openai-sentinel-chat-requirements-token": "...",
  "openai-sentinel-turnstile-token": "...",
  "openai-sentinel-proof-token": "...",
  "x-conduit-token": "...",
  "x-oai-is": "...",
  "x-oai-turn-trace-id": "...",
  "oai-client-version": "...",
  "oai-client-build-number": "...",
  "oai-session-id": "..."
}
```

`tools/refresh_browser_auth.py` writes `config/auth.local.json` with the
Cookie header, access token, and browser user-agent. `main.py` reuses that
browser state and a stable `oai-device-id`, but it still cannot synthesize the
current sentinel, Turnstile, proof-token, and conduit token chain by itself.
