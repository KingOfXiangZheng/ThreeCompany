#!/usr/bin/env python3
"""
Pure HTTP Gemini image generation client.

Sends an image generation prompt via StreamGenerate, extracts image URLs
from the response, and downloads the generated images.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import requests

try:
    import curl_cffi.requests as curl_requests
except Exception:
    curl_requests = None

# Reuse existing infrastructure
from .main import (
    Bootstrap,
    _read_cached_at_token,
    _read_cached_bootstrap,
    fetch_bootstrap,
    generate_session_id,
    is_request_error,
    load_config,
    make_session,
    make_cookie_header,
    safe_print,
    with_query,
    build_stream_body,
    build_stream_headers,
)


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "generated_images"


def extract_image_urls_from_response(raw: str) -> list[dict[str, Any]]:
    """Extract image URLs and metadata from StreamGenerate response."""
    images: list[dict[str, Any]] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            batch = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in batch:
            if not isinstance(item, list) or len(item) < 3:
                continue
            payload_raw = item[2]
            if not isinstance(payload_raw, str) or not payload_raw.startswith("["):
                continue
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                continue

            # Walk the nested structure looking for image metadata
            _walk_extract_images(payload, images)

    return images


def _walk_extract_images(value: Any, images: list[dict[str, Any]]) -> None:
    """Recursively walk response to find image URLs and metadata."""
    if isinstance(value, list):
        # Check if this looks like an image metadata tuple:
        # [null, 1, "filename.png", "https://lh3.googleusercontent.com/...", null, "token", ...]
        if (
            len(value) >= 6
            and isinstance(value[1], (int, float))
            and isinstance(value[2], str)
            and isinstance(value[3], str)
            and "googleusercontent.com" in value[3]
        ):
            image_info = {
                "filename": value[2],
                "url": value[3],
            }
            # Extract dimensions if present (usually at index 14 or similar)
            for v in value:
                if isinstance(v, list) and len(v) == 3:
                    a, b, c = v
                    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and isinstance(c, (int, float)):
                        if a > 100 and b > 100:
                            image_info["width"] = int(a)
                            image_info["height"] = int(b)
                            image_info["size_bytes"] = int(c)
                elif isinstance(v, list) and len(v) == 2:
                    a, b = v
                    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                        if a > 1700000000:  # looks like a timestamp
                            image_info["timestamp"] = int(a)
            # Check for mime type
            for v in value:
                if isinstance(v, str) and v.startswith("image/"):
                    image_info["mime_type"] = v

            # Avoid duplicates
            if not any(img["url"] == image_info["url"] for img in images):
                images.append(image_info)

        # Recurse into children
        for item in value:
            _walk_extract_images(item, images)
    elif isinstance(value, dict):
        for v in value.values():
            _walk_extract_images(v, images)


def extract_conversation_state(raw: str) -> dict[str, str]:
    """Extract conversation_id, response_id, and conversation_token."""
    state: dict[str, str] = {}

    def _walk_strings(v: Any) -> list[str]:
        found: list[str] = []
        if isinstance(v, str):
            found.append(v)
        elif isinstance(v, list):
            for item in v:
                found.extend(_walk_strings(item))
        elif isinstance(v, dict):
            for item in v.values():
                found.extend(_walk_strings(item))
        return found

    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("[["):
            continue
        try:
            batch = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in batch:
            if not isinstance(item, list) or len(item) < 3:
                continue
            payload_raw = item[2]
            if not isinstance(payload_raw, str) or not payload_raw.startswith("["):
                continue
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                continue

            for text in _walk_strings(payload):
                if text.startswith("c_"):
                    state["conversation_id"] = text
                elif text.startswith("r_"):
                    state["response_id"] = text
                elif text.startswith("rc_"):
                    state["candidate_id"] = text

            # Extract conversation_token from metadata
            if isinstance(payload, list) and len(payload) >= 3 and isinstance(payload[2], dict):
                meta = payload[2]
                token_values = meta.get("21")
                if isinstance(token_values, list) and token_values and isinstance(token_values[0], str):
                    state["conversation_token"] = token_values[0]

    return state


def download_image(url: str, save_path: Path, referer: str = "https://gemini.google.com/") -> bool:
    """Download an image from Google CDN."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
        "Accept": "*/*",
        "Referer": referer,
        "Origin": referer.rstrip("/"),
    }

    session_to_use = requests.Session()
    if curl_requests is not None:
        try:
            resp = curl_requests.get(url, headers=headers, timeout=30, allow_redirects=True)
            save_path.write_bytes(resp.content)
            return True
        except Exception as e:
            safe_print(f"[Image] curl_cffi download failed: {e}")

    try:
        resp = session_to_use.get(url, headers=headers, timeout=30, allow_redirects=True)
        if resp.status_code == 200:
            save_path.write_bytes(resp.content)
            return True
        safe_print(f"[Image] HTTP {resp.status_code} downloading image")
    except Exception as e:
        safe_print(f"[Image] Download failed: {e}")

    return False


def generate_images(
    prompt: str,
    model: str = "gemini-3-fast",
    gemini_url: str = "https://gemini.google.com/u/1",
    output_dir: Path | None = None,
    max_wait_seconds: int = 120,
) -> list[dict[str, Any]]:
    """
    Generate images using Gemini's StreamGenerate API.

    Returns a list of dicts with: url, filename, local_path, width, height
    """
    if output_dir is None:
        output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    cookies, headers, config = load_config()
    session = make_session(cookies, headers)
    bootstrap = fetch_bootstrap(session, config, gemini_url)

    safe_print(
        f"[Image Gen] bootstrap: status={bootstrap.status}, bl={bootstrap.bl}, "
        f"has_at={bool(bootstrap.at)}, model={model}"
    )

    if not bootstrap.at:
        raise RuntimeError("No AT token found. Check your cookies and config.")

    request_id = str(uuid.uuid4()).upper()
    body = build_stream_body(
        prompt,
        bootstrap.at,
        request_id=request_id,
        request_context_token=config.get("request_context_token"),
        client_context_id=config.get("client_context_id"),
        model=model,
    )
    request_headers = build_stream_headers(bootstrap.url, model, request_id=request_id)

    if bootstrap.bl:
        config["version"] = bootstrap.bl

    all_images: list[dict[str, Any]] = []
    conversation_id: str | None = None
    response_id: str | None = None
    full_raw = ""

    for path in bootstrap.stream_paths:
        full_url = urllib.parse.urljoin(
            config["api_base"],
            with_query(path, config, bootstrap.f_sid),
        )

        safe_print(f"[Image Gen] requesting: {full_url[:120]}...")

        try:
            with session.post(
                full_url,
                data=body,
                headers=request_headers,
                timeout=max_wait_seconds,
                verify=False,
            ) as response:
                if response.status_code != 200:
                    safe_print(f"[Image Gen] HTTP {response.status_code}")
                    continue

                full_raw = response.text
                safe_print(f"[Image Gen] response: {len(full_raw)} bytes, {full_raw.count(chr(10))} lines")

                # Extract conversation state
                state = extract_conversation_state(full_raw)
                conversation_id = state.get("conversation_id")
                response_id = state.get("response_id")

                safe_print(
                    f"[Image Gen] conversation_id={conversation_id or '-'}, "
                    f"response_id={response_id or '-'}, "
                    f"candidate_id={state.get('candidate_id', '-')}"
                )

                # Extract image URLs
                images = extract_image_urls_from_response(full_raw)
                if images:
                    safe_print(f"[Image Gen] found {len(images)} image(s)")
                    for i, img in enumerate(images):
                        safe_print(
                            f"  [{i+1}] {img.get('filename', '?')} "
                            f"{img.get('width', '?')}x{img.get('height', '?')} "
                            f"→ {img['url'][:80]}..."
                        )
                    all_images.extend(images)
                else:
                    safe_print("[Image Gen] no images found in response")
                    # Print a sample for debugging
                    safe_print(f"[Image Gen] response sample:\n{full_raw[:2000]}")

        except Exception as e:
            if not is_request_error(e):
                raise
            safe_print(f"[Image Gen] request failed: {e}")
            continue

    # Download images
    downloaded: list[dict[str, Any]] = []
    for i, img in enumerate(all_images):
        filename = img.get("filename", f"image_{i+1}.png")
        # Ensure .jpg extension for downloaded images
        if not any(filename.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")):
            filename = Path(filename).stem + ".jpg"

        save_path = output_dir / filename
        safe_print(f"[Image Gen] downloading [{i+1}/{len(all_images)}]: {filename}")

        if download_image(img["url"], save_path):
            img["local_path"] = str(save_path)
            img["local_size"] = save_path.stat().st_size
            downloaded.append(img)
            safe_print(f"  saved: {save_path} ({img['local_size']} bytes)")
        else:
            safe_print(f"  download failed for {img['url'][:80]}")

    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini Image Generation (Pure HTTP)")
    parser.add_argument("prompt", help="Image generation prompt")
    parser.add_argument("--model", default="gemini-3-fast", help="Model to use (default: gemini-3-fast)")
    parser.add_argument("--gemini-url", default="https://gemini.google.com/u/1")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--timeout", type=int, default=120, help="Max wait seconds")
    parser.add_argument("--raw-response", action="store_true", help="Print raw response")
    args = parser.parse_args()

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    images = generate_images(
        prompt=args.prompt,
        model=args.model,
        gemini_url=args.gemini_url,
        output_dir=args.output_dir,
        max_wait_seconds=args.timeout,
    )

    if images:
        safe_print(f"\n{'='*50}")
        safe_print(f"Generated {len(images)} image(s):")
        for img in images:
            safe_print(f"  {img.get('filename', '?')} ({img.get('width', '?')}x{img.get('height', '?')})")
            safe_print(f"    URL: {img['url']}")
            if img.get("local_path"):
                safe_print(f"    Local: {img['local_path']} ({img.get('local_size', 0)} bytes)")
    else:
        safe_print("\nNo images were generated.")


if __name__ == "__main__":
    main()
