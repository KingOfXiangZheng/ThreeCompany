"""Generate Datadog RUM (Real User Monitoring) trace headers for Claude API requests.

Claude's frontend uses Datadog for monitoring, and these headers are part of the
browser fingerprint. Missing them can trigger Cloudflare 403 errors.
"""
import random
import time


def generate_trace_id() -> str:
    """Generate a random 64-bit trace ID (as decimal string)."""
    return str(random.randint(1, (1 << 63) - 1))


def generate_span_id() -> str:
    """Generate a random 64-bit span ID (as hex string, 16 chars)."""
    return format(random.randint(1, (1 << 64) - 1), '016x')


def generate_traceparent(trace_id_hex: str, span_id_hex: str) -> str:
    """Generate W3C traceparent header.

    Format: version-trace_id-parent_id-trace_flags
    - version: 00 (fixed)
    - trace_id: 32 hex chars (128-bit)
    - parent_id: 16 hex chars (64-bit)
    - trace_flags: 01 (sampled)
    """
    # Pad trace_id to 32 chars
    trace_id_padded = trace_id_hex.zfill(32)
    return f"00-{trace_id_padded}-{span_id_hex}-01"


def generate_datadog_headers() -> dict[str, str]:
    """Generate complete Datadog RUM trace headers for Claude API requests.

    Returns:
        Dictionary with Datadog trace headers:
        - traceparent: W3C trace context
        - tracestate: Datadog trace state
        - x-datadog-origin: Origin identifier (rum = Real User Monitoring)
        - x-datadog-parent-id: Parent span ID (decimal)
        - x-datadog-sampling-priority: Sampling priority (1 = sampled)
        - x-datadog-trace-id: Trace ID (decimal)
    """
    # Generate IDs
    trace_id_decimal = generate_trace_id()
    parent_id_decimal = generate_trace_id()
    span_id_hex = generate_span_id()

    # Convert trace_id to hex for traceparent
    trace_id_hex = format(int(trace_id_decimal), 'x')

    return {
        "traceparent": generate_traceparent(trace_id_hex, span_id_hex),
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id_decimal,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id_decimal,
    }


def generate_activity_session_id() -> str:
    """Generate a UUID-like activity session ID.

    Format: xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx
    where x is random hex, 4 is version, y is 8/9/a/b
    """
    import uuid
    return str(uuid.uuid4())


def generate_dd_session_cookie() -> str:
    """Generate Datadog session cookie value (_dd_s).

    Format: aid=<uuid>&rum=2&id=<uuid>&created=<timestamp>&expire=<timestamp>
    """
    import uuid
    now_ms = int(time.time() * 1000)
    expire_ms = now_ms + 25 * 60 * 1000  # 25 minutes

    aid = str(uuid.uuid4())
    session_id = str(uuid.uuid4())

    return f"aid={aid}&rum=2&id={session_id}&created={now_ms}&expire={expire_ms}"


if __name__ == "__main__":
    # Test generation
    print("=== Datadog Trace Headers ===")
    headers = generate_datadog_headers()
    for key, value in headers.items():
        print(f"{key}: {value}")

    print("\n=== Activity Session ID ===")
    print(generate_activity_session_id())

    print("\n=== Datadog Session Cookie ===")
    print(f"_dd_s={generate_dd_session_cookie()}")
