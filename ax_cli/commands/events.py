"""ax events — SSE event streaming and runtime probes."""
import json
import os
import sys
import threading
import time
from queue import Empty, Queue
from typing import Optional

import typer
import httpx

from ..config import get_client, resolve_space_id
from ..output import JSON_OPTION, console, print_json

app = typer.Typer(name="events", help="Event streaming", no_args_is_help=True)

ROUTING_EVENT_TYPES = {"routing_status", "dispatch_progress", "agent_processing"}


@app.command("stream")
def stream(
    max_events: int = typer.Option(0, "--max-events", help="Stop after N events (0=unlimited)"),
    agent_id: Optional[str] = typer.Option(None, "--agent-id", help="Target agent"),
    filter: Optional[str] = typer.Option(None, "--filter", help="Filter: 'routing', 'messages', or event type"),
    as_json: bool = JSON_OPTION,
):
    """Stream SSE events in real-time. Use --filter routing to see only routing events."""
    client = get_client()
    url = f"{client.base_url}/api/v1/sse/messages"
    params = {"token": client.token}
    headers = {}
    if agent_id:
        headers["X-Agent-Id"] = agent_id

    filter_types: set[str] | None = None
    if filter == "routing":
        filter_types = ROUTING_EVENT_TYPES
    elif filter == "messages":
        filter_types = {"message", "mention"}
    elif filter:
        filter_types = {filter}

    typer.echo(f"Connecting to {url} ...", err=True)
    if filter_types:
        typer.echo(f"Filtering: {', '.join(sorted(filter_types))}", err=True)
    count = 0
    try:
        with httpx.stream("GET", url, params=params, headers=headers, timeout=None) as resp:
            event_type = None
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    if filter_types and event_type not in filter_types:
                        continue

                    data_str = line[5:].strip()
                    try:
                        parsed = json.loads(data_str)
                    except json.JSONDecodeError:
                        parsed = data_str

                    if as_json:
                        print(json.dumps({"event": event_type, "data": parsed}, default=str))
                        sys.stdout.flush()
                    else:
                        preview = data_str[:120] + "..." if len(data_str) > 120 else data_str
                        console.print(f"[bold cyan][{event_type}][/bold cyan] {preview}")

                    count += 1
                    if max_events and count >= max_events:
                        typer.echo(f"\nReached {max_events} events, stopping.", err=True)
                        return
    except KeyboardInterrupt:
        typer.echo(f"\nStopped after {count} events.", err=True)
    except httpx.HTTPStatusError as e:
        typer.echo(f"Error {e.response.status_code}: {e.response.text}", err=True)
        raise typer.Exit(1)


def _extract_message_payload(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("message"), dict):
            return payload["message"]
        if isinstance(payload.get("data"), dict):
            return payload["data"]
    return payload if isinstance(payload, dict) else {}


def _event_listener(
    *,
    base_url: str,
    token: str,
    space_id: str,
    headers: dict[str, str],
    outbox: Queue,
) -> None:
    url = f"{base_url}/api/v1/sse/messages"
    params = {"token": token, "space_id": space_id}
    event_type = None

    try:
        with httpx.stream("GET", url, params=params, headers=headers, timeout=None) as resp:
            resp.raise_for_status()
            outbox.put(("__status__", {"connected": True}, time.monotonic()))
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        parsed = json.loads(data_str)
                    except json.JSONDecodeError:
                        parsed = data_str
                    outbox.put((event_type, parsed, time.monotonic()))
    except Exception as exc:
        outbox.put(("__error__", {"error": str(exc)}, time.monotonic()))


@app.command("probe")
def probe(
    prompt: str = typer.Argument(
        "Give me a two-paragraph answer about why streaming parity matters for AgentCore migration.",
        help="Prompt used for the probe message",
    ),
    timeout: int = typer.Option(45, "--timeout", "-t", help="Max seconds to wait for final reply"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Send one message and measure SSE processing, tool, and delta timing."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space_id)
    event_queue: Queue = Queue()
    listener = threading.Thread(
        target=_event_listener,
        kwargs={
            "base_url": client.base_url,
            "token": client.token,
            "space_id": sid,
            "headers": {},
            "outbox": event_queue,
        },
        daemon=True,
    )
    listener.start()

    connected = False
    connected_deadline = time.monotonic() + 5
    while time.monotonic() < connected_deadline:
        try:
            event_type, payload, _ts = event_queue.get(timeout=0.2)
        except Empty:
            continue
        if event_type == "__status__":
            connected = True
            break
        if event_type == "__error__":
            typer.echo(f"Error connecting probe stream: {payload.get('error')}", err=True)
            raise typer.Exit(1)

    if not connected:
        typer.echo("Error: probe stream did not connect within 5s", err=True)
        raise typer.Exit(1)

    send_started_at = time.monotonic()
    data = client.send_message(sid, prompt)
    sent = data.get("message", data)
    message_id = sent.get("id") or sent.get("message_id") or data.get("id")
    if not message_id:
        typer.echo("Error: send did not return a message id", err=True)
        raise typer.Exit(1)

    first_processing_at = None
    first_delta_at = None
    final_reply_at = None
    reply_stream_id = None
    reply_id = None
    delta_count = 0
    delta_chars = 0
    tool_events: list[dict[str, str]] = []
    seen_tool_pairs: set[tuple[str, str]] = set()
    last_reply_poll = 0.0

    deadline = send_started_at + timeout
    while time.monotonic() < deadline:
        now = time.monotonic()
        remaining = max(0.1, min(1.0, deadline - now))
        try:
            event_type, payload, event_ts = event_queue.get(timeout=remaining)
        except Empty:
            event_type = None
            payload = None
            event_ts = None

        if event_type == "__error__":
            typer.echo(f"Probe stream error: {payload.get('error')}", err=True)
            break

        if event_type == "agent_processing" and isinstance(payload, dict):
            parent_id = payload.get("parent_id")
            stream_id = payload.get("message_id")
            if parent_id == message_id or (reply_stream_id and stream_id == reply_stream_id):
                reply_stream_id = stream_id or reply_stream_id
                if first_processing_at is None:
                    first_processing_at = event_ts
                status = str(payload.get("status") or "")
                tool_name = str(payload.get("tool") or payload.get("tool_name") or "")
                if tool_name:
                    tool_key = (status, tool_name)
                    if tool_key not in seen_tool_pairs:
                        seen_tool_pairs.add(tool_key)
                        tool_events.append({"status": status, "tool": tool_name})

        elif event_type == "message_delta" and isinstance(payload, dict):
            parent_id = payload.get("parent_id")
            stream_id = payload.get("message_id")
            if parent_id == message_id or (reply_stream_id and stream_id == reply_stream_id):
                reply_stream_id = stream_id or reply_stream_id
                if first_delta_at is None:
                    first_delta_at = event_ts
                delta = str(payload.get("delta") or "")
                delta_count += 1
                delta_chars += len(delta)

        elif event_type in {"message", "new_message", "message_created"}:
            msg = _extract_message_payload(payload)
            if not isinstance(msg, dict):
                msg = {}
            sender_type = str(msg.get("sender_type") or "")
            parent_id = msg.get("parent_id")
            if sender_type not in {"user", "human"} and parent_id == message_id:
                reply_id = str(msg.get("id") or "")
                final_reply_at = event_ts
                break

        if now - last_reply_poll >= 1.0:
            last_reply_poll = now
            try:
                replies_data = client.list_replies(message_id)
                replies = (
                    replies_data
                    if isinstance(replies_data, list)
                    else replies_data.get("messages", replies_data.get("replies", []))
                )
                for reply in replies:
                    if not isinstance(reply, dict):
                        continue
                    sender_type = str(reply.get("sender_type") or "")
                    if sender_type in {"user", "human"}:
                        continue
                    reply_id = str(reply.get("id") or "")
                    final_reply_at = time.monotonic()
                    break
            except Exception:
                pass
            if final_reply_at is not None:
                break

    total_ms = int((time.monotonic() - send_started_at) * 1000)
    first_processing_ms = (
        int((first_processing_at - send_started_at) * 1000)
        if first_processing_at is not None
        else None
    )
    first_delta_ms = (
        int((first_delta_at - send_started_at) * 1000)
        if first_delta_at is not None
        else None
    )
    final_reply_ms = (
        int((final_reply_at - send_started_at) * 1000)
        if final_reply_at is not None
        else None
    )

    report = {
        "space_id": sid,
        "sent_message_id": message_id,
        "reply_stream_id": reply_stream_id,
        "reply_message_id": reply_id,
        "metrics": {
            "first_processing_ms": first_processing_ms,
            "first_delta_ms": first_delta_ms,
            "final_reply_ms": final_reply_ms,
            "delta_count": delta_count,
            "delta_chars": delta_chars,
            "tool_events": tool_events,
            "total_probe_ms": total_ms,
        },
        "parity": {
            "has_processing_event": first_processing_ms is not None,
            "has_delta_stream": delta_count > 0,
            "multi_delta_stream": delta_count > 1,
            "has_final_reply": final_reply_ms is not None,
        },
    }

    if as_json:
        print_json(report)
        return

    console.print(f"[green]Probe complete[/green] sent={message_id} reply={reply_id or 'pending'}")
    console.print(f"  first processing: {first_processing_ms if first_processing_ms is not None else 'none'} ms")
    console.print(f"  first delta:      {first_delta_ms if first_delta_ms is not None else 'none'} ms")
    console.print(f"  final reply:      {final_reply_ms if final_reply_ms is not None else 'none'} ms")
    console.print(f"  deltas:           {delta_count} ({delta_chars} chars)")
    if tool_events:
        console.print(
            "  tools:            "
            + ", ".join(f"{item['status']}:{item['tool']}" for item in tool_events)
        )
    else:
        console.print("  tools:            none")


def _extract_return_control_tools(return_controls: list[dict]) -> list[dict[str, str]]:
    tools: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rc in return_controls:
        for invocation in rc.get("invocationInputs", []) or []:
            func_input = invocation.get("functionInvocationInput", {}) or {}
            action_group = str(func_input.get("actionGroup") or "")
            function_name = str(func_input.get("function") or "")
            if not function_name:
                continue
            key = (action_group, function_name)
            if key in seen:
                continue
            seen.add(key)
            tools.append({
                "action_group": action_group,
                "function": function_name,
            })
    return tools


@app.command("probe-runtime")
def probe_runtime(
    prompt: str = typer.Argument(
        "Give me a three paragraph explanation of why streaming UX matters for agents. Do not call any tools.",
        help="Prompt sent directly to the Bedrock runtime",
    ),
    mode: str = typer.Option(
        "bedrock-agent",
        "--mode",
        help="Runtime mode: bedrock-agent or agentcore-runtime",
    ),
    region: str = typer.Option(
        os.getenv("BEDROCK_AGENT_REGION", os.getenv("AWS_REGION", "us-west-2")),
        "--region",
        help="AWS region for the runtime client",
    ),
    agent_id: str = typer.Option(
        os.getenv("BEDROCK_AGENT_ID", ""),
        "--agent-id",
        help="Bedrock agent ID for --mode bedrock-agent",
    ),
    agent_alias_id: str = typer.Option(
        os.getenv("BEDROCK_AGENT_ALIAS_ID", ""),
        "--agent-alias-id",
        help="Bedrock agent alias ID for --mode bedrock-agent",
    ),
    agent_runtime_arn: str = typer.Option(
        os.getenv("AGENTCORE_RUNTIME_ARN", ""),
        "--agent-runtime-arn",
        help="AgentCore runtime ARN for --mode agentcore-runtime",
    ),
    session_id: Optional[str] = typer.Option(
        None,
        "--session-id",
        help="Reuse a specific runtime session ID instead of generating one",
    ),
    enable_trace: bool = typer.Option(
        False,
        "--enable-trace",
        help="Enable Bedrock trace events where supported",
    ),
    as_json: bool = JSON_OPTION,
):
    """Probe the AWS runtime directly and measure native chunk timing."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ModuleNotFoundError:
        typer.echo(
            "Error: runtime probe requires boto3 in the ax-cli environment. "
            "Install boto3 or run the probe from an environment that already has it.",
            err=True,
        )
        raise typer.Exit(1)

    probe_session_id = session_id or f"cli-runtime-probe-{int(time.time() * 1000)}"
    boto_config = BotoConfig(
        region_name=region,
        connect_timeout=5.0,
        read_timeout=120.0,
        retries={"max_attempts": 3, "mode": "adaptive"},
    )

    if mode == "bedrock-agent":
        if not agent_id or not agent_alias_id:
            typer.echo(
                "Error: --agent-id and --agent-alias-id are required for --mode bedrock-agent",
                err=True,
            )
            raise typer.Exit(1)

        client = boto3.client("bedrock-agent-runtime", config=boto_config)
        start = time.monotonic()
        response = client.invoke_agent(
            agentId=agent_id,
            agentAliasId=agent_alias_id,
            sessionId=probe_session_id,
            inputText=prompt,
            enableTrace=enable_trace,
        )

        chunk_times: list[tuple[float, int, str]] = []
        chunk_chars = 0
        trace_count = 0
        return_controls: list[dict] = []
        for event in response.get("completion", []):
            event_ts = time.monotonic() - start
            if "chunk" in event:
                text = event["chunk"].get("bytes", b"").decode("utf-8")
                if text:
                    chunk_times.append((event_ts, len(text), text[:120]))
                    chunk_chars += len(text)
            elif "trace" in event:
                trace_count += 1
            elif "returnControl" in event:
                return_controls.append(event["returnControl"])

        report = {
            "mode": mode,
            "region": region,
            "session_id": probe_session_id,
            "agent_id": agent_id,
            "agent_alias_id": agent_alias_id,
            "metrics": {
                "chunk_count": len(chunk_times),
                "first_chunk_ms": round(chunk_times[0][0] * 1000, 1) if chunk_times else None,
                "second_chunk_ms": round(chunk_times[1][0] * 1000, 1) if len(chunk_times) > 1 else None,
                "last_chunk_ms": round(chunk_times[-1][0] * 1000, 1) if chunk_times else None,
                "chunk_chars": chunk_chars,
                "trace_count": trace_count,
                "return_control_count": len(return_controls),
            },
            "return_control_tools": _extract_return_control_tools(return_controls),
            "chunk_previews": [item[2] for item in chunk_times[:3]],
        }
    elif mode == "agentcore-runtime":
        if not agent_runtime_arn:
            typer.echo(
                "Error: --agent-runtime-arn is required for --mode agentcore-runtime",
                err=True,
            )
            raise typer.Exit(1)

        client = boto3.client("bedrock-agentcore", config=boto_config)
        start = time.monotonic()
        response = client.invoke_agent_runtime(
            agentRuntimeArn=agent_runtime_arn,
            runtimeSessionId=probe_session_id,
            payload=prompt.encode("utf-8"),
        )

        body = response.get("response")
        line_times: list[tuple[float, int, str]] = []
        text_chars = 0
        if hasattr(body, "iter_lines"):
            for raw_line in body.iter_lines(chunk_size=64):
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8")
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if not data:
                    continue
                event_ts = time.monotonic() - start
                line_times.append((event_ts, len(data), data[:120]))
                text_chars += len(data)
        else:
            raw = body.read() if hasattr(body, "read") else body
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if raw:
                line_times.append((time.monotonic() - start, len(raw), raw[:120]))
                text_chars += len(raw)

        report = {
            "mode": mode,
            "region": region,
            "session_id": probe_session_id,
            "agent_runtime_arn": agent_runtime_arn,
            "content_type": response.get("contentType"),
            "metrics": {
                "chunk_count": len(line_times),
                "first_chunk_ms": round(line_times[0][0] * 1000, 1) if line_times else None,
                "second_chunk_ms": round(line_times[1][0] * 1000, 1) if len(line_times) > 1 else None,
                "last_chunk_ms": round(line_times[-1][0] * 1000, 1) if line_times else None,
                "chunk_chars": text_chars,
            },
            "chunk_previews": [item[2] for item in line_times[:3]],
        }
    else:
        typer.echo("Error: --mode must be 'bedrock-agent' or 'agentcore-runtime'", err=True)
        raise typer.Exit(1)

    if as_json:
        print_json(report)
        return

    console.print(f"[green]Runtime probe complete[/green] mode={report['mode']}")
    console.print(f"  session:          {report['session_id']}")
    console.print(f"  first chunk:      {report['metrics']['first_chunk_ms']} ms")
    console.print(f"  second chunk:     {report['metrics'].get('second_chunk_ms')} ms")
    console.print(f"  last chunk:       {report['metrics']['last_chunk_ms']} ms")
    console.print(
        f"  chunks:           {report['metrics']['chunk_count']} "
        f"({report['metrics']['chunk_chars']} chars)"
    )
    if report.get("return_control_tools"):
        console.print(
            "  return control:   "
            + ", ".join(
                f"{item['action_group']}::{item['function']}"
                for item in report["return_control_tools"]
            )
        )
    elif mode == "bedrock-agent":
        console.print("  return control:   none")
