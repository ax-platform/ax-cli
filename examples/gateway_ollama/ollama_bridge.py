#!/usr/bin/env python3
"""Gateway-managed bridge for a local Ollama model.

This bridge is designed for `ax gateway agents add ... --template ollama`.
It emits Gateway progress events while making a streaming call to a local
Ollama server, then prints the final text reply to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from urllib import error, request

EVENT_PREFIX = "AX_GATEWAY_EVENT "
DEFAULT_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
DEFAULT_OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")


def emit_event(payload: dict[str, Any]) -> None:
    print(f"{EVENT_PREFIX}{json.dumps(payload, sort_keys=True)}", flush=True)


def _read_prompt() -> str:
    if len(sys.argv) > 1 and sys.argv[-1] != "-":
        return sys.argv[-1]
    env_prompt = os.environ.get("AX_MENTION_CONTENT", "").strip()
    if env_prompt:
        return env_prompt
    return sys.stdin.read().strip()


def _generate(prompt: str) -> str:
    model = DEFAULT_OLLAMA_MODEL
    endpoint = f"{DEFAULT_OLLAMA_BASE_URL}/api/generate"
    body = {
        "model": model,
        "prompt": prompt,
        "stream": True,
    }
    emit_event({"kind": "status", "status": "thinking", "message": f"Preparing Ollama request ({model})"})
    emit_event({"kind": "status", "status": "processing", "message": f"Calling Ollama ({model})"})

    req = request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    started = time.monotonic()
    chunks: list[str] = []
    first_token_seen = False
    last_activity_at = 0.0
    try:
        with request.urlopen(req, timeout=300) as response:
            for raw in response:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    continue
                if payload.get("error"):
                    raise RuntimeError(str(payload["error"]))
                text = str(payload.get("response") or "")
                if text:
                    chunks.append(text)
                    now = time.monotonic()
                    if not first_token_seen:
                        first_token_seen = True
                        emit_event({"kind": "status", "status": "processing", "message": f"Ollama is responding ({model})"})
                    if now - last_activity_at >= 1.0:
                        emit_event({"kind": "activity", "activity": f"Streaming response from {model}..."})
                        last_activity_at = now
                if payload.get("done"):
                    break
    except error.URLError as exc:
        raise RuntimeError(f"Failed to reach Ollama at {endpoint}: {exc.reason}") from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_event(
        {
            "kind": "status",
            "status": "completed",
            "message": f"Ollama completed in {duration_ms}ms",
            "detail": {"model": model, "duration_ms": duration_ms},
        }
    )
    return "".join(chunks).strip()


def main() -> int:
    prompt = _read_prompt()
    if not prompt:
        print("(no mention content received)", file=sys.stderr)
        return 1

    try:
        reply = _generate(prompt)
    except Exception as exc:
        emit_event({"kind": "status", "status": "error", "error_message": str(exc)})
        print(f"Ollama bridge failed: {exc}")
        return 1

    print(reply or f"Ollama ({DEFAULT_OLLAMA_MODEL}) finished without text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
