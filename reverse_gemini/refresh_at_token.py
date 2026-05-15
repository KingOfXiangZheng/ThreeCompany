"""Refresh the cached at token from a running Camoufox MCP browser.

Usage:
    python refresh_at_token.py                    # uses default MCP endpoint
    python refresh_at_token.py --mcp-url http://127.0.0.1:8765/mcp

The script calls the MCP browser's evaluate_js tool to extract SNlM0e
from the Gemini page and writes it to config/at_token.txt.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CACHE_PATH = ROOT / "config" / "at_token.txt"
DEFAULT_MCP_URL = "http://127.0.0.1:8765/mcp"


def mcp_call(url: str, tool: str, arguments: dict | None = None) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def extract_at_token(mcp_url: str) -> str | None:
    js_expr = (
        "(function() {"
        "  for (const s of document.scripts) {"
        "    const m = s.textContent && s.textContent.match(/\"SNlM0e\"\\s*:\\s*\"([^\"]+)\"/);"
        "    if (m) return m[1];"
        "  }"
        "  return null;"
        "})()"
    )
    result = mcp_call(mcp_url, "evaluate_js", {"expression": js_expr})
    content = result.get("result", {}).get("content", [])
    for item in content:
        if item.get("type") == "text":
            try:
                data = json.loads(item["text"])
                value = data.get("value")
                if value and value != "not found":
                    return value
            except json.JSONDecodeError:
                pass
    return None


def main() -> None:
    mcp_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MCP_URL
    print(f"Fetching at token via MCP at {mcp_url} ...")
    try:
        token = extract_at_token(mcp_url)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if not token:
        print("ERROR: could not extract SNlM0e from page", file=sys.stderr)
        sys.exit(1)

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(token + "\n", encoding="utf-8")
    print(f"at token saved to {CACHE_PATH}")


if __name__ == "__main__":
    main()
