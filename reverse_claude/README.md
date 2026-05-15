# Claude Web Reverse

Pure HTTP Claude Web client used by the project-level OpenAI-compatible API.

## Captured Text Flow

The current Claude Web text completion flow is:

```text
POST https://claude.ai/api/organizations/<org_id>/chat_conversations/<conversation_id>/completion
Accept: text/event-stream
Content-Type: application/json
anthropic-client-platform: web_claude_ai
anthropic-device-id: <browser device id>
Cookie: <logged-in Claude cookies>
```

The client reads this response as a real stream. For `curl_cffi` transport it
uses `content_callback`; for `requests` transport it uses `iter_lines()`.

The SSE response emits `message_start`, `content_block_delta`, `message_delta`,
and `message_stop`. Text deltas are in:

```text
content_block_delta.delta.text
```

For continuation turns, send the previous assistant `message.uuid` as
`parent_message_uuid`.

## Config

Recommended path:

```powershell
python core/refresh_browser_auth.py --target claude
```

Manual path: copy the example files and fill them from an authorized browser
capture:

```powershell
Copy-Item reverse_claude/config/config.example.json reverse_claude/config/config.json
Copy-Item reverse_claude/config/headers.example.json reverse_claude/config/headers.json
Copy-Item reverse_claude/config/cookies.example.json reverse_claude/config/cookies.json
```

Then verify:

```powershell
python reverse_claude/main.py "Reply exactly: CLAUDE_OK" --model claude-sonnet-4-6
```
