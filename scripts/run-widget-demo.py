#!/usr/bin/env python3
"""Generate a polished Activity Stream widget demo in the active aX space."""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from ax_cli.commands.apps import (
    APP_SPECS,
    _build_signal_metadata,
    _context_item_from_response,
)
from ax_cli.commands.qa import _normalize_upload
from ax_cli.commands.tasks import _task_signal_metadata
from ax_cli.config import get_client, resolve_space_id

SPACE_ID = "12d6eafd-0316-4f3e-be33-fd8a3fd90f67"


def message_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    value = message.get("id") or message.get("message_id")
    return str(value) if value else None


def write_asset_files(run_id: str) -> dict[str, Path]:
    root = Path(tempfile.mkdtemp(prefix=f"ax-widget-demo-{run_id}-"))
    svg = root / "activity-stream-architecture.svg"
    svg.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="760" viewBox="0 0 1200 760">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0f172a"/>
      <stop offset="0.52" stop-color="#11363f"/>
      <stop offset="1" stop-color="#172554"/>
    </linearGradient>
    <linearGradient id="gold" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#fde68a"/>
      <stop offset="1" stop-color="#22d3ee"/>
    </linearGradient>
    <filter id="soft"><feDropShadow dx="0" dy="16" stdDeviation="18" flood-color="#020617" flood-opacity="0.45"/></filter>
  </defs>
  <rect width="1200" height="760" rx="34" fill="url(#bg)"/>
  <circle cx="1050" cy="120" r="110" fill="#22d3ee" opacity="0.12"/>
  <circle cx="180" cy="650" r="130" fill="#f59e0b" opacity="0.10"/>
  <text x="72" y="96" fill="#f8fafc" font-family="Inter,Arial" font-size="48" font-weight="800">aX Activity Stream</text>
  <text x="74" y="138" fill="#bfdbfe" font-family="Inter,Arial" font-size="22">One stream. Durable context. Openable MCP apps.</text>
  <g filter="url(#soft)">
    <rect x="74" y="210" width="294" height="150" rx="22" fill="#0f172a" stroke="#67e8f9" stroke-opacity=".55"/>
    <text x="108" y="260" fill="#e0f2fe" font-family="Inter,Arial" font-size="26" font-weight="700">Context</text>
    <text x="108" y="300" fill="#cbd5e1" font-family="Inter,Arial" font-size="18">Documents, diagrams,</text>
    <text x="108" y="326" fill="#cbd5e1" font-family="Inter,Arial" font-size="18">screenshots, evidence</text>
    <rect x="454" y="210" width="294" height="150" rx="22" fill="#0f172a" stroke="#fbbf24" stroke-opacity=".6"/>
    <text x="488" y="260" fill="#fef3c7" font-family="Inter,Arial" font-size="26" font-weight="700">Tasks</text>
    <text x="488" y="300" fill="#cbd5e1" font-family="Inter,Arial" font-size="18">Reminder cards open</text>
    <text x="488" y="326" fill="#cbd5e1" font-family="Inter,Arial" font-size="18">the exact task detail</text>
    <rect x="834" y="210" width="294" height="150" rx="22" fill="#0f172a" stroke="#a7f3d0" stroke-opacity=".55"/>
    <text x="868" y="260" fill="#d1fae5" font-family="Inter,Arial" font-size="26" font-weight="700">Agents</text>
    <text x="868" y="300" fill="#cbd5e1" font-family="Inter,Arial" font-size="18">Signals show status,</text>
    <text x="868" y="326" fill="#cbd5e1" font-family="Inter,Arial" font-size="18">identity, and ownership</text>
  </g>
  <path d="M370 285 C410 285 412 285 452 285" stroke="url(#gold)" stroke-width="6" fill="none" stroke-linecap="round"/>
  <path d="M750 285 C790 285 792 285 832 285" stroke="url(#gold)" stroke-width="6" fill="none" stroke-linecap="round"/>
  <g transform="translate(166 468)">
    <rect width="868" height="118" rx="26" fill="#020617" opacity=".68" stroke="#94a3b8" stroke-opacity=".25"/>
    <text x="42" y="46" fill="#f8fafc" font-family="Inter,Arial" font-size="23" font-weight="700">Demo loop</text>
    <text x="42" y="82" fill="#cbd5e1" font-family="Inter,Arial" font-size="19">Context preview → task detail → reminder → identity → media sidecar</text>
    <circle cx="784" cy="58" r="28" fill="#22d3ee" opacity=".2"/>
    <path d="M773 58h22M784 47v22" stroke="#cffafe" stroke-width="5" stroke-linecap="round"/>
  </g>
</svg>
""",
        encoding="utf-8",
    )

    md = root / "launch-brief.md"
    md.write_text(
        f"""# Demo Launch Brief

This is the clean demo set for the aX Activity Stream and MCP app panel.

## What to show

| Moment | What the audience sees | Why it matters |
| --- | --- | --- |
| Context | A polished artifact opens in the immersive Context viewer | Evidence stays durable and navigable |
| Task | A reminder opens the exact task, not a task list | Work is actionable |
| Identity | The current agent identity renders as an app card | Agent runtime state is visible |
| Media | A YouTube link renders as a sidecar | The stream can carry useful rich evidence |

## Talking points

- The transcript is becoming an **activity stream**.
- Cards are compact receipts. The app panel is the full experience.
- Share routes existing objects to people or agents without losing context.
- Widgets are the North Star: they should match or beat attachment previews.

## Demo run

`{run_id}`
""",
        encoding="utf-8",
    )
    return {"svg": svg, "md": md}


def upload_context_file(client: Any, sid: str, path: Path, run_id: str, key_name: str) -> dict[str, Any]:
    upload = _normalize_upload(client.upload_file(str(path), space_id=sid))
    content = path.read_text(encoding="utf-8", errors="replace")
    context_key = f"demo:{run_id}:{key_name}"
    value = {
        "type": "file_upload",
        "source": "widget_demo",
        "attachment_id": upload["attachment_id"],
        "context_key": context_key,
        "filename": path.name,
        "content_type": upload.get("content_type") or ("image/svg+xml" if path.suffix == ".svg" else "text/markdown"),
        "size": upload.get("size"),
        "url": upload.get("url"),
        "summary": "Demo artifact for the aX Activity Stream.",
        "content": content,
        "file_content": content,
        "file_upload": {
            "filename": path.name,
            "content_type": upload.get("content_type") or ("image/svg+xml" if path.suffix == ".svg" else "text/markdown"),
            "size": upload.get("size"),
            "context_key": context_key,
            "url": upload.get("url"),
        },
    }
    client.set_context(sid, context_key, json.dumps(value), ttl=86400)
    item = _context_item_from_response(context_key, client.get_context(context_key, space_id=sid))
    return {"key": context_key, "item": item}


def send_context_signal(client: Any, sid: str, *, title: str, summary: str, context_key: str, item: dict[str, Any]) -> str | None:
    spec = APP_SPECS["context"]
    metadata, _ = _build_signal_metadata(
        app_name="context",
        resource_uri=spec["resource_uri"],
        title=title,
        action="get",
        space_id=sid,
        context_key=context_key,
        context_item=item,
        whoami_payload=None,
        collection_payload=None,
        summary=summary,
        target=None,
        alert_kind=None,
        severity="info",
    )
    metadata["top_level_ingress"] = False
    metadata["signal_only"] = True
    metadata["app_signal"]["signal_only"] = True
    return message_id(client.send_message(sid, f"{title}: {summary}", metadata=metadata, message_type="system"))


def send_whoami_signal(client: Any, sid: str, run_id: str) -> str | None:
    spec = APP_SPECS["whoami"]
    metadata, _ = _build_signal_metadata(
        app_name="whoami",
        resource_uri=spec["resource_uri"],
        title="Current agent identity",
        action="get",
        space_id=sid,
        context_key=None,
        context_item=None,
        whoami_payload=client.whoami(),
        collection_payload=None,
        summary=f"Agent identity and runtime context for {run_id}.",
        target=None,
        alert_kind=None,
        severity="info",
    )
    metadata["top_level_ingress"] = False
    metadata["signal_only"] = True
    metadata["app_signal"]["signal_only"] = True
    return message_id(client.send_message(sid, "Current agent identity card is ready.", metadata=metadata, message_type="system"))


def send_spaces_signal(client: Any, sid: str) -> str | None:
    spec = APP_SPECS["spaces"]
    metadata, _ = _build_signal_metadata(
        app_name="spaces",
        resource_uri=spec["resource_uri"],
        title="Workspace map",
        action="list",
        space_id=sid,
        context_key=None,
        context_item=None,
        whoami_payload=None,
        collection_payload=client.list_spaces(),
        summary="Browse the spaces this account can access.",
        target=None,
        alert_kind=None,
        severity="info",
    )
    metadata["top_level_ingress"] = False
    metadata["signal_only"] = True
    metadata["app_signal"]["signal_only"] = True
    return message_id(client.send_message(sid, "Workspace map is ready.", metadata=metadata, message_type="system"))


def send_task_detail(client: Any, sid: str, run_id: str) -> tuple[str | None, str | None]:
    title = "Demo: Review Activity Stream launch checklist"
    description = (
        "Open this card during the demo. It should land directly on this task detail, "
        "show the description, and make the reminder feel actionable."
    )
    created = client.create_task(sid, title, description=description, priority="high")
    task = created.get("task") if isinstance(created.get("task"), dict) else created
    metadata = _task_signal_metadata(
        task,
        space_id=sid,
        title=title,
        description=description,
        assignee_id=None,
        assignee_label=None,
    )
    task_id = str(task.get("id") or task.get("task_id") or "")
    msg = client.send_message(
        sid,
        f"Demo task ready: **{title}**. Open the task card for details.",
        metadata=metadata,
        message_type="system",
    )
    return message_id(msg), task_id


def send_media_sidecar(client: Any, sid: str, run_id: str) -> str | None:
    text = (
        f"Demo media sidecar {run_id}\n\n"
        "This message intentionally carries a plain URL and a YouTube URL.\n\n"
        "Plain URL: https://example.com/\n"
        "Video: https://www.youtube.com/watch?v=jNQXAC9IVRw"
    )
    return message_id(
        client.send_message(
            sid,
            text,
            metadata={"demo_fixture": {"kind": "media_sidecar", "run_id": run_id}},
            message_type="text",
        )
    )


def main() -> None:
    run_id = os.environ.get("AX_DEMO_RUN_ID") or f"demo-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:4]}"
    client = get_client()
    sid = resolve_space_id(client, explicit=os.environ.get("AX_SPACE_ID") or SPACE_ID)
    files = write_asset_files(run_id)
    svg_context = upload_context_file(client, sid, files["svg"], run_id, "activity-stream-architecture.svg")
    md_context = upload_context_file(client, sid, files["md"], run_id, "launch-brief.md")

    cards = [
        {
            "name": "context:architecture",
            "message_id": send_context_signal(
                client,
                sid,
                title="Activity Stream architecture",
                summary="Open the SVG diagram in the immersive Context viewer.",
                context_key=svg_context["key"],
                item=svg_context["item"],
            ),
            "context_key": svg_context["key"],
        },
        {
            "name": "context:launch_brief",
            "message_id": send_context_signal(
                client,
                sid,
                title="Demo launch brief",
                summary="Open the Markdown brief with the demo talking points.",
                context_key=md_context["key"],
                item=md_context["item"],
            ),
            "context_key": md_context["key"],
        },
    ]
    task_message_id, task_id = send_task_detail(client, sid, run_id)
    cards.append({"name": "task:detail", "message_id": task_message_id, "task_id": task_id})
    cards.append({"name": "identity", "message_id": send_whoami_signal(client, sid, run_id)})
    cards.append({"name": "spaces", "message_id": send_spaces_signal(client, sid)})
    cards.append({"name": "media_sidecar", "message_id": send_media_sidecar(client, sid, run_id)})

    print(json.dumps({"ok": True, "run_id": run_id, "space_id": sid, "cards": cards}, indent=2))


if __name__ == "__main__":
    main()
