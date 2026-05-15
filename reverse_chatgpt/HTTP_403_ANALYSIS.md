# ChatGPT Reverse HTTP 403 Analysis

## Summary

`python main.py "hello" --backend http` returns HTTP 403 because the pure HTTP client is using the obsolete ChatGPT Web conversation path:

```text
POST /backend-api/conversation
```

Current ChatGPT Web sends messages through a newer browser-mediated path:

```text
POST /backend-api/f/conversation
```

The new path requires browser runtime state and dynamic sentinel headers that the pure HTTP client does not generate.

## Evidence

Authentication is valid:

```powershell
python main.py --check
```

returns a valid session, access token, user-agent, and stable device id.

The old pure HTTP request still fails:

```powershell
python main.py "hello" --backend http
```

returns:

```text
HTTP 403
Unusual activity has been detected from your device.
```

The same old endpoint also fails when called from an authenticated browser page with `fetch('/backend-api/conversation')`, so this is not only a Python `requests` TLS/header issue.

The real ChatGPT UI succeeds. Network capture shows it sends:

```text
POST /backend-api/f/conversation
```

and calls several sentinel-related endpoints around the request:

```text
POST /backend-api/f/conversation/prepare
POST /backend-api/sentinel/heartbeat
POST /backend-api/sentinel/chat-requirements/prepare
POST /backend-api/sentinel/chat-requirements/finalize
POST /backend-api/sentinel/req
```

The successful UI request includes dynamic headers absent from `--backend http`, including:

```text
x-openai-target-path
x-openai-target-route
x-oai-is
openai-sentinel-chat-requirements-token
openai-sentinel-turnstile-token
openai-sentinel-proof-token
x-conduit-token
x-oai-turn-trace-id
oai-client-version
oai-client-build-number
oai-session-id
```

## Cause

The 403 is not caused by expired cookies. It is caused by protocol drift and missing browser-side risk-control state.

The pure HTTP client can reuse Cookie, access token, user-agent, and `oai-device-id`, but it cannot produce the current ChatGPT Web sentinel/Turnstile/proof/conduit token chain.

## Implemented Mitigation

`main.py` now supports:

```text
--backend auto
--backend http
--backend browser
```

Default `auto` behavior:

1. Try the pure HTTP diagnostic path.
2. If the known 403 boundary is hit, fall back to the repository's browser-backed `chatgpt_web` client.

Recommended commands:

```powershell
python main.py "hello" --attach-only
python main.py "hello" --backend browser --attach-only
```

Diagnostic-only command:

```powershell
python main.py "hello" --backend http
```

This is expected to keep returning 403 unless the pure HTTP protocol is fully rewritten for the new sentinel flow.

## Notes

Rewriting the pure HTTP path would require reproducing browser-side sentinel, Turnstile, proof-token, and conduit state generation. This is not a fixed-header patch and is expected to be fragile as ChatGPT Web changes.
