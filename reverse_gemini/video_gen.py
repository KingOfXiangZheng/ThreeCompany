#!/usr/bin/env python3
"""
Pure HTTP Gemini video generation client.

Sends a video generation prompt via StreamGenerate with media_generation flag,
polls for completion, and extracts video download URLs from the response.
"""

from __future__ import annotations

import argparse
import json
import re
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

from .main import (
    Bootstrap,
    build_batchexecute_body,
    build_batchexecute_headers,
    build_batchexecute_url,
    build_stream_body,
    build_stream_headers,
    extract_stream_state,
    fetch_bootstrap,
    is_request_error,
    load_config,
    make_session,
    safe_print,
    with_query,
)


OUTPUT_DIR = Path(__file__).resolve().parent.parent / "generated_videos"


def extract_video_task_id(raw: str) -> str | None:
    """Extract video generation task UUID from StreamGenerate response."""
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
            # Walk the payload looking for video_gen_chip and task UUID
            task_id = _walk_extract_video_task(payload)
            if task_id:
                return task_id
    return None


def _walk_extract_video_task(value: Any) -> str | None:
    """Recursively walk response to find video task UUID."""
    if isinstance(value, list):
        for item in value:
            result = _walk_extract_video_task(item)
            if result:
                return result
    elif isinstance(value, dict):
        for v in value.values():
            result = _walk_extract_video_task(v)
            if result:
                return result
    elif isinstance(value, str):
        # Look for UUID pattern that appears in video task metadata
        if re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", value):
            return value
    return None


def extract_video_urls_from_response(raw: str) -> list[dict[str, Any]]:
    """Extract video URLs and metadata from hNvQHb response."""
    videos: list[dict[str, Any]] = []

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
            _walk_extract_videos(payload, videos)

    return videos


def _walk_extract_videos(value: Any, videos: list[dict[str, Any]]) -> None:
    """Recursively walk response to find video metadata."""
    if isinstance(value, list):
        # Check if this looks like a video metadata tuple
        # Pattern: [null, 2, "video.mp4", null, null, "token", null, [thumbnail_url, download_url, ...], 2, [timestamp], null, "video/mp4", ...]
        if (
            len(value) >= 8
            and isinstance(value[1], (int, float))
            and value[1] == 2
            and isinstance(value[2], str)
            and value[2].endswith(".mp4")
        ):
            video_info: dict[str, Any] = {
                "filename": value[2],
            }
            # Extract token at index 5
            if len(value) > 5 and isinstance(value[5], str):
                video_info["token"] = value[5]
            # Extract URLs at index 7
            if len(value) > 7 and isinstance(value[7], list):
                urls = value[7]
                if len(urls) >= 3:
                    video_info["thumbnail_url"] = urls[0] if isinstance(urls[0], str) else None
                    video_info["download_url"] = urls[1] if isinstance(urls[1], str) else None
                    video_info["preview_url"] = urls[2] if isinstance(urls[2], str) else None
            # Extract mime type
            if len(value) > 11 and isinstance(value[11], str):
                video_info["mime_type"] = value[11]
            # Extract dimensions at index 14
            if len(value) > 14 and isinstance(value[14], list) and len(value[14]) >= 3:
                dims = value[14]
                if isinstance(dims[1], (int, float)) and isinstance(dims[2], (int, float)):
                    video_info["width"] = int(dims[1])
                    video_info["height"] = int(dims[2])
            # Extract timestamp
            if len(value) > 9 and isinstance(value[9], list) and len(value[9]) >= 2:
                ts = value[9]
                if isinstance(ts[0], (int, float)):
                    video_info["timestamp"] = int(ts[0])

            # Avoid duplicates
            if video_info.get("download_url") and not any(
                v.get("download_url") == video_info["download_url"] for v in videos
            ):
                videos.append(video_info)

        # Recurse into children
        for item in value:
            _walk_extract_videos(item, videos)
    elif isinstance(value, dict):
        for v in value.values():
            _walk_extract_videos(v, videos)


def download_video(url: str, save_path: Path, referer: str = "https://gemini.google.com/") -> bool:
    """Download a video from Google CDN."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
        "Accept": "*/*",
        "Referer": referer,
        "Origin": referer.rstrip("/"),
    }

    session_to_use = requests.Session()
    if curl_requests is not None:
        try:
            resp = curl_requests.get(url, headers=headers, timeout=60, allow_redirects=True)
            save_path.write_bytes(resp.content)
            return True
        except Exception as e:
            safe_print(f"[Video] curl_cffi download failed: {e}")

    try:
        resp = session_to_use.get(url, headers=headers, timeout=60, allow_redirects=True)
        if resp.status_code == 200:
            save_path.write_bytes(resp.content)
            return True
        safe_print(f"[Video] HTTP {resp.status_code} downloading video")
    except Exception as e:
        safe_print(f"[Video] Download failed: {e}")

    return False


def poll_video_status(
    session: requests.Session,
    config: dict[str, Any],
    bootstrap: Bootstrap,
    task_id: str,
    max_wait_seconds: int = 300,
    poll_interval: int = 5,
) -> bool:
    """Poll kwDCne endpoint until video generation completes."""
    url = build_batchexecute_url(config, "kwDCne", bootstrap.f_sid)
    body = build_batchexecute_body("kwDCne", [task_id])
    headers = build_batchexecute_headers(bootstrap.url)

    start_time = time.time()
    while time.time() - start_time < max_wait_seconds:
        try:
            response = session.post(url, data=body, headers=headers, timeout=30, verify=False)
            if response.status_code == 200:
                # Check if video is ready by looking for completion markers
                text = response.text
                # If we see timestamps indicating completion, break
                # The response contains [1779613747,794404105] when complete vs [1779613695,252708616] when pending
                if "177961374" in text or "true" in text.lower():
                    safe_print("[Video Gen] Video generation completed")
                    return True
                safe_print(f"[Video Gen] Still generating... ({int(time.time() - start_time)}s)")
            else:
                safe_print(f"[Video Gen] Poll HTTP {response.status_code}")
        except Exception as e:
            if not is_request_error(e):
                raise
            safe_print(f"[Video Gen] Poll error: {e}")

        time.sleep(poll_interval)

    safe_print("[Video Gen] Poll timeout reached")
    return False


def fetch_conversation_content(
    session: requests.Session,
    config: dict[str, Any],
    bootstrap: Bootstrap,
    conversation_id: str,
) -> str:
    """Fetch full conversation content via hNvQHb to get video URLs."""
    url = build_batchexecute_url(config, "hNvQHb", bootstrap.f_sid)
    # Params: [conversation_id, limit, null, 1, [1], [4], null, 1]
    body = build_batchexecute_body("hNvQHb", [conversation_id, 10, None, 1, [1], [4], None, 1])
    headers = build_batchexecute_headers(bootstrap.url)

    try:
        response = session.post(url, data=body, headers=headers, timeout=30, verify=False)
        if response.status_code == 200:
            return response.text
        safe_print(f"[Video Gen] hNvQHb HTTP {response.status_code}")
    except Exception as e:
        if not is_request_error(e):
            raise
        safe_print(f"[Video Gen] hNvQHb error: {e}")

    return ""


def generate_videos(
    prompt: str,
    model: str = "gemini-3-pro",
    gemini_url: str = "https://gemini.google.com/u/1",
    output_dir: Path | None = None,
    max_wait_seconds: int = 300,
) -> list[dict[str, Any]]:
    """
    Generate videos using Gemini's StreamGenerate API with media_generation flag.

    Returns a list of dicts with: download_url, preview_url, thumbnail_url,
    filename, width, height, mime_type, local_path
    """
    if output_dir is None:
        output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    cookies, headers, config = load_config()
    session = make_session(cookies, headers)
    bootstrap = fetch_bootstrap(session, config, gemini_url)

    safe_print(
        f"[Video Gen] bootstrap: status={bootstrap.status}, bl={bootstrap.bl}, "
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
        media_generation=True,
    )
    request_headers = build_stream_headers(bootstrap.url, model, request_id=request_id)

    if bootstrap.bl:
        config["version"] = bootstrap.bl

    conversation_id: str | None = None
    response_id: str | None = None
    task_id: str | None = None
    full_raw = ""

    for path in bootstrap.stream_paths:
        full_url = urllib.parse.urljoin(
            config["api_base"],
            with_query(path, config, bootstrap.f_sid),
        )

        safe_print(f"[Video Gen] requesting: {full_url[:120]}...")

        try:
            with session.post(
                full_url,
                data=body,
                headers=request_headers,
                timeout=120,
                verify=False,
            ) as response:
                if response.status_code != 200:
                    safe_print(f"[Video Gen] HTTP {response.status_code}")
                    continue

                full_raw = response.text
                safe_print(f"[Video Gen] response: {len(full_raw)} bytes, {full_raw.count(chr(10))} lines")

                # Extract conversation state
                state = extract_stream_state(full_raw)
                conversation_id = state.get("conversation_id")
                response_id = state.get("response_id")

                safe_print(
                    f"[Video Gen] conversation_id={conversation_id or '-'}, "
                    f"response_id={response_id or '-'}"
                )

                # Extract video task ID
                task_id = extract_video_task_id(full_raw)
                if task_id:
                    safe_print(f"[Video Gen] video task_id={task_id}")
                else:
                    safe_print("[Video Gen] no video task_id found in initial response")
                    # Print sample for debugging
                    safe_print(f"[Video Gen] response sample:\n{full_raw[:2000]}")

        except Exception as e:
            if not is_request_error(e):
                raise
            safe_print(f"[Video Gen] request failed: {e}")
            continue

    if not conversation_id:
        safe_print("[Video Gen] No conversation_id found")
        return []

    # Poll for video completion
    if task_id:
        safe_print("[Video Gen] Polling for video completion...")
        poll_video_status(session, config, bootstrap, task_id, max_wait_seconds)

    # Fetch full conversation content to get video URLs
    safe_print("[Video Gen] Fetching conversation content for video URLs...")
    conv_content = fetch_conversation_content(session, config, bootstrap, conversation_id)

    if not conv_content:
        safe_print("[Video Gen] No conversation content returned")
        return []

    # Extract video URLs
    videos = extract_video_urls_from_response(conv_content)
    if videos:
        safe_print(f"[Video Gen] found {len(videos)} video(s)")
        for i, vid in enumerate(videos):
            safe_print(
                f"  [{i+1}] {vid.get('filename', '?')} "
                f"{vid.get('width', '?')}x{vid.get('height', '?')} "
                f"→ {vid.get('download_url', '?')[:80]}..."
            )
    else:
        safe_print("[Video Gen] no videos found in response")
        safe_print(f"[Video Gen] conv content sample:\n{conv_content[:2000]}")
        return []

    # Download videos
    downloaded: list[dict[str, Any]] = []
    for i, vid in enumerate(videos):
        filename = vid.get("filename", f"video_{i+1}.mp4")
        save_path = output_dir / filename
        download_url = vid.get("download_url")
        if not download_url:
            safe_print(f"  [{i+1}] no download URL, skipping")
            continue

        safe_print(f"[Video Gen] downloading [{i+1}/{len(videos)}]: {filename}")

        if download_video(download_url, save_path):
            vid["local_path"] = str(save_path)
            vid["local_size"] = save_path.stat().st_size
            downloaded.append(vid)
            safe_print(f"  saved: {save_path} ({vid['local_size']} bytes)")
        else:
            safe_print(f"  download failed for {download_url[:80]}")

    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini Video Generation (Pure HTTP)")
    parser.add_argument("prompt", help="Video generation prompt")
    parser.add_argument("--model", default="gemini-3-pro", help="Model to use")
    parser.add_argument("--gemini-url", default="https://gemini.google.com/u/1")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--timeout", type=int, default=300, help="Max wait seconds")
    args = parser.parse_args()

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    videos = generate_videos(
        prompt=args.prompt,
        model=args.model,
        gemini_url=args.gemini_url,
        output_dir=args.output_dir,
        max_wait_seconds=args.timeout,
    )

    if videos:
        safe_print(f"\n{'='*50}")
        safe_print(f"Generated {len(videos)} video(s):")
        for vid in videos:
            safe_print(f"  {vid.get('filename', '?')} ({vid.get('width', '?')}x{vid.get('height', '?')})")
            safe_print(f"    Download: {vid.get('download_url', 'N/A')}")
            if vid.get("local_path"):
                safe_print(f"    Local: {vid['local_path']} ({vid.get('local_size', 0)} bytes)")
    else:
        safe_print("\nNo videos were generated.")


if __name__ == "__main__":
    main()
