# Grok Web Reverse

Pure HTTP Grok Web client used by the project-level OpenAI-compatible API.

## Captured Text Flow

The current Grok text flow observed from `https://grok.com/` is:

```text
POST https://grok.com/rest/app-chat/conversations/new
Content-Type: application/json
x-xai-request-id: <uuid>
x-statsig-id: <browser/local token when available>
Cookie: <logged-in Grok cookies>
```

The browser request body includes the user message plus chat mode fields such as
`modeId: "fast"`. `fast`, `auto`, `expert`, and `heavy` are exposed as model
aliases in this project.

## Usage

Refresh auth from Chrome CDP:

```powershell
python core/refresh_browser_auth.py --target grok --attach-only --cdp-port 9222 --no-wait-for-login
```

Run a direct Grok request:

```powershell
python reverse_grok/main.py "Reply exactly: GROK_OK" --model grok-fast
```

Continue a conversation:

```powershell
python reverse_grok/main.py "next turn" --model grok-fast --conversation-id <conversation_id> --parent-response-id <assistant_response_id>
```

## Config

Recommended path:

```powershell
python core/refresh_browser_auth.py --target grok
```

To reuse an existing Chrome on port 9222:

```powershell
python core/refresh_browser_auth.py --target grok --attach-only --cdp-port 9222 --no-wait-for-login
```

Then verify:

```powershell
python reverse_grok/main.py "Reply exactly: GROK_OK" --model grok-fast
```

If Grok returns `Request rejected by anti-bot rules`, the protocol and auth files
were loaded but the exported browser state is not currently accepted by Grok's
send-path anti-bot gate.
