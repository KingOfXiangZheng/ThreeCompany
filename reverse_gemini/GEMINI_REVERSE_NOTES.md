# Gemini Web Reverse Notes

## 2026-05-12: Pro Mode and Browser History Persistence

This note records the debugging path for a case where `gemini-pro` was expected
to call Gemini Pro, but the result appeared to fall back to Flash and browser
history was not created.

## Key Findings

The `gemini-pro -> gemini-3-pro` alias was not the root cause. The upper model
registry and the Gemini request layer both normalized `gemini-pro` to
`gemini-3-pro` correctly.

The real issues were:

- The protocol payload was slightly stale compared with the current Gemini Web
  browser request.
- Only `StreamGenerate` was being called, so the browser-side post-generation
  acknowledgement was missing.
- Cookie handling differed from the browser and could either create anonymous
  replies or return `401` when complete login cookies were duplicated.

## Pro Mode Evidence

Browser network capture showed these stable Pro markers:

- Mode id: `e6fa609c3fa255c0`
- Mode code: `3`
- Request body mode fields: `inner[17]=[[0]]`, `inner[79]=3`
- Response metadata model label: `3.1 Pro`

Do not rely on asking the model to identify itself. Prefer request fields and
response metadata from captured traffic.

## Required Request Chain

A successful text response from `StreamGenerate` is not enough to make the
conversation visible in the Gemini browser UI.

The browser performs a follow-up acknowledgement:

- RPC: `PCck7e`
- Parameter: `[response_id]`
- Must include the same bootstrap `at` token in the form body.

Without `at`, the acknowledgement fails with `400` and an `xsrf` marker in the
response sample.

Other observed follow-up RPCs such as `qpEbW`, `aPya6c`, and `L5adhe` appear to
handle status, quota, empty refresh, and history/list refresh behavior, but
`PCck7e(response_id)` was the critical post-generation confirmation.

## Cookie Lessons

Using only `__Secure-ENID` can be enough to generate a reply, but it does not
behave like the logged-in browser session for history persistence.

Using the complete browser login cookie set is required for browser-visible
history, but it must be sent like the browser sends it. The failed approach was
to set every cookie on both `.google.com` and `gemini.google.com`; that created
duplicate `SID`, `__Secure-*PSID`, and related cookies in the request header and
caused `401`.

The working approach is to construct one explicit browser-style `Cookie` header
from the Gemini/Google cookies and send that header directly.

Useful cookie names seen in the real Gemini browser request included:

- `SOCS`
- `__Secure-ENID`
- `SID`
- `__Secure-1PSID`
- `__Secure-3PSID`
- `HSID`
- `SSID`
- `APISID`
- `SAPISID`
- `__Secure-1PAPISID`
- `__Secure-3PAPISID`
- `SIDCC`
- `__Secure-1PSIDCC`
- `__Secure-3PSIDCC`
- `COMPASS`
- `__Secure-1PSIDTS`
- `__Secure-3PSIDTS`
- `NID`

## Validation Criteria

Use all of these checks before considering the reverse flow correct:

1. `gemini-pro` normalizes to `gemini-3-pro`.
2. `StreamGenerate` returns HTTP `200`.
3. Parsed response includes `conversation_id` and `response_id`.
4. `PCck7e(response_id)` returns HTTP `200`.
5. Browser can open:

   ```text
   https://gemini.google.com/app/<conversation_id without c_ prefix>
   ```

6. The opened browser page resolves with HTTP `200` and shows the generated
   conversation.

## Debugging Order

For future Gemini Web protocol issues, check in this order:

1. Model alias normalization.
2. `StreamGenerate` body mode fields and mode headers.
3. Response metadata model marker.
4. Post-generation `PCck7e` acknowledgement.
5. Cookie header parity with the browser request.
6. Browser URL access using the returned `conversation_id`.

## Implementation Notes

The current fix keeps browser automation out of the runtime path. Browser/Camoufox
is used only to collect evidence and refresh cookies. The final Gemini request
flow remains pure HTTP.

## 2026-05-13: Tool Continuation, Proxy Transport, and 1076/1097 Errors

This follow-up records the final fixes after the Pro/history work. The symptoms
were:

- First Gemini Pro turns succeeded, but tool-result continuations returned
  `BardErrorInfo [1097]` or later `BardErrorInfo [1076]`.
- Some attempts failed before Gemini responded with TLS errors such as
  `UNEXPECTED_EOF_WHILE_READING` or curl error 28.
- Browser access worked while Python HTTP calls failed.

### Continuation State

Gemini Web continuation needs more than `conversation_id` and `response_id`.
Browser captures showed the state array also carries:

- `rc_*` candidate id in `inner[2][2]`
- a conversation token in `inner[2][9]`

The implementation now extracts these values from successful `StreamGenerate`
responses and stores them in `conv_cache/gemini_state.json`. Continuation turns
load this state and write it back into the next `StreamGenerate` body.

Expected continuation log:

```text
[Gemini Web] continuation state: candidate_id=rc_..., has_token=True
```

If `has_token=False`, capture the previous successful response and check whether
the parser missed the token or the response did not include it.

### Proxy Transport

The network failure was not a Gemini protocol failure. Local evidence showed:

- Direct TCP to `gemini.google.com:443` failed.
- Windows user proxy was enabled at `127.0.0.1:7897`.
- That port worked as a SOCKS proxy, not as an HTTP proxy:

  ```text
  curl.exe --proxy socks5h://127.0.0.1:7897 https://gemini.google.com/
  ```

The runtime now detects proxy settings from:

1. `GEMINI_PROXY`
2. `HTTPS_PROXY` / `ALL_PROXY`
3. Windows user proxy registry settings

For local `127.0.0.1:<port>` proxies it normalizes the proxy to
`socks5h://127.0.0.1:<port>`.

### curl_cffi Caveats

`curl_cffi.Session(impersonate="chrome")` and `Session(proxy=...)` were unstable
with this local SOCKS proxy. The stable path was stateless module-level
`curl_cffi.request(...)` with an explicit SOCKS proxy and redirects disabled.

The implementation wraps this in a small stateless session adapter so existing
code can still call `.get(...)`, `.post(...)`, and use `with response:`.

Important transport rules from this case:

- Use `socks5h://` so DNS also goes through the proxy.
- Do not rely on WinHTTP proxy settings; they were `Direct access`.
- Disable automatic redirects for bootstrap requests. Following `/u/1` redirects
  through this proxy sometimes caused curl TLS errors.
- Use non-streaming `POST` for `StreamGenerate` in the service path. The CLI
  path succeeded because it already read the full response body; the service
  path failed while using `stream=True` plus `iter_lines`.

### Post-generation Ack

`PCck7e(response_id)` is still needed for browser history persistence, but its
network failure must not discard an already parsed model response.

The ack is now retried up to three times. If all attempts fail, the failure is
logged and the already parsed assistant response is still returned. This keeps
the API response usable even if browser history persistence is temporarily
unreliable.

### Tool Result Formatting

Explicit transport labels such as:

```text
[Tool result from tool_name]: ...
Please continue based on the tool results above.
```

can trigger Gemini Web refusal/errors during multi-turn continuation. The working
format is a natural user-style continuation:

```text
Use the runtime information below to continue answering the user's request.

User request:
...

Runtime information:
Result from tool_name:
...
Continue the answer using the information above.
```

Also do not append the tool-call reminder or full tool schema on tool-result
continuation turns. The tool schema is only needed when asking Gemini to emit a
new tool call.

### Final Validation

The final working checks were:

```text
python reverse_gemini/main.py "Reply exactly: PROXY_OK" --model gemini-3-pro
```

Service client path:

```text
CLIENT_OK
```

Tool-result continuation path:

```text
FIRST_OK -> TOOL_WRAP_OK
```

A healthy service run should show:

- `model=gemini-3-pro`
- current `bl` from bootstrap, not stale `20260507`
- `continuation state: candidate_id=rc_..., has_token=True`
- `post-generation ack: status=200`

If the next failure is HTTP `200` with `BardErrorInfo`, inspect request content
and continuation state first. If the next failure is curl/TLS, inspect proxy
selection and whether the request path is accidentally using direct access,
HTTP proxy, redirects, or streaming mode.
