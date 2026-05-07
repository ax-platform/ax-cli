"""ax apps — API adapter for MCP app signals."""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import httpx
import typer

from ..config import get_authoring_client, resolve_space_id
from ..output import JSON_OPTION, console, handle_error, mention_prefix, print_json, print_table

app = typer.Typer(name="apps", help="MCP app signal adapter", no_args_is_help=True)


APP_SPECS: dict[str, dict[str, str]] = {
    "tasks": {"resource_uri": "ui://tasks/board", "title": "Task Board"},
    "tasks/detail": {"resource_uri": "ui://tasks/detail", "title": "Task Detail"},
    "messages": {"resource_uri": "ui://messages/timeline", "title": "Message Timeline"},
    "agents": {"resource_uri": "ui://agents/dashboard", "title": "Agent Dashboard"},
    "spaces": {"resource_uri": "ui://spaces/navigator", "title": "Space Navigator"},
    "search": {"resource_uri": "ui://search/results", "title": "Search Results"},
    "context": {"resource_uri": "ui://context/explorer", "title": "Context Explorer"},
    "context/graph": {"resource_uri": "ui://context/graph", "title": "Context Graph"},
    "whoami": {"resource_uri": "ui://whoami/identity", "title": "Agent Identity"},
}


_mention_prefix = mention_prefix


def _parse_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _context_item_from_response(context_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    item = dict(payload.get("item") or payload)
    item.setdefault("key", context_key)
    wrapped = item.get("value") if isinstance(item.get("value"), dict) else None
    raw_value = wrapped.get("value") if wrapped and "value" in wrapped else item.get("value")
    parsed_value = _parse_json_value(raw_value)
    if parsed_value is not None:
        item["value"] = parsed_value
    if wrapped:
        for key in ("agent_name", "created_at", "updated_at", "expires_at", "summary", "topic", "ttl", "source"):
            if key in wrapped and key not in item:
                item[key] = wrapped[key]
    if isinstance(parsed_value, dict):
        for key in ("summary", "file_upload", "file_content", "ttl", "created_at", "updated_at", "expires_at"):
            if key in parsed_value and key not in item:
                item[key] = parsed_value[key]
        if "content" in parsed_value and "file_content" not in item:
            item["file_content"] = parsed_value["content"]
    return item


def _space_name_from_whoami(payload: dict[str, Any], space_id: str) -> str:
    bound_agent = payload.get("bound_agent") if isinstance(payload.get("bound_agent"), dict) else {}
    for item in bound_agent.get("allowed_spaces") or []:
        if isinstance(item, dict) and _first_text(item.get("space_id"), item.get("id")) == space_id:
            return _first_text(item.get("name"), item.get("display_name"))
    return _first_text(
        bound_agent.get("default_space_name"),
        payload.get("resolved_space_name"),
        payload.get("space_name"),
    )


def _whoami_initial_data(payload: dict[str, Any], *, space_id: str, summary: str | None) -> dict[str, Any]:
    bound_agent = payload.get("bound_agent") if isinstance(payload.get("bound_agent"), dict) else {}
    agent_name = _first_text(bound_agent.get("agent_name"), payload.get("resolved_agent"))
    agent_id = _first_text(bound_agent.get("agent_id"))
    principal_kind = "agent" if agent_name else "user"

    if principal_kind == "agent":
        identity: dict[str, Any] = {
            "principal_kind": "agent",
            "role_label": "Agent",
            "status": "active",
            "handle": agent_name,
            "display_name": agent_name,
        }
        if agent_id:
            identity["id"] = agent_id
    else:
        identity = {
            "principal_kind": "user",
            "role_label": _first_text(payload.get("role"), "User").title(),
            "status": "active",
        }
        user_id = _first_text(payload.get("id"), payload.get("user_id"))
        handle = _first_text(payload.get("username"), payload.get("handle"))
        display_name = _first_text(payload.get("full_name"), payload.get("display_name"), handle, payload.get("email"))
        if user_id:
            identity["id"] = user_id
        if handle:
            identity["handle"] = handle
        if display_name:
            identity["display_name"] = display_name
        if payload.get("email"):
            identity["email"] = payload["email"]

    context: dict[str, Any] = {
        "workspace_id": space_id,
        "binding_label": "Bound space" if principal_kind == "agent" else "Current space",
    }
    space_name = _space_name_from_whoami(payload, space_id)
    if space_name:
        context["workspace_name"] = space_name
    if principal_kind == "agent":
        owner: dict[str, Any] = {}
        owner_id = _first_text(payload.get("id"), payload.get("user_id"))
        owner_handle = _first_text(payload.get("username"), payload.get("handle"))
        owner_name = _first_text(payload.get("full_name"), payload.get("display_name"), owner_handle)
        if owner_id:
            owner["id"] = owner_id
        if owner_handle:
            owner["handle"] = owner_handle
        if owner_name:
            owner["display_name"] = owner_name
        if payload.get("email"):
            owner["email"] = payload["email"]
        if owner:
            context["owner"] = owner

    data = {
        "identity": identity,
        "context": context,
        "capabilities": payload.get("capabilities") or [],
        "memory": payload.get("memory") or {},
    }
    return {
        "kind": "whoami_profile",
        "version": 2,
        "state": "ready",
        "data": data,
        "active_tab": "profile",
        "summary": summary,
        "source": "axctl_apps_signal",
    }


def _collection_items(payload: Any, *candidate_keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in candidate_keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _collection_count(payload: Any, items: list[dict[str, Any]]) -> int:
    if isinstance(payload, dict):
        for key in ("count", "total"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
    return len(items)


def _collection_keys(items: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for item in items:
        key = _first_text(item.get("id"), item.get("space_id"), item.get("task_id"), item.get("name"))
        if key:
            keys.append(key)
    return keys


def _collection_initial_data(
    *,
    kind: str,
    action: str,
    space_id: str,
    payload: Any,
    summary: str | None,
) -> dict[str, Any]:
    key_map = {
        "agents": ("agents", "items", "results"),
        "spaces": ("spaces", "items", "results"),
        "tasks": ("tasks", "items", "results"),
    }
    items = _collection_items(payload, *key_map.get(kind, ("items", "results")))
    data: dict[str, Any] = {
        "kind": kind,
        "version": 1,
        "action": action,
        "items": items,
        "keys": _collection_keys(items),
        "count": _collection_count(payload, items),
        "summary": summary,
        "space_id": space_id,
        "source": "axctl_apps_signal",
    }
    if isinstance(payload, dict):
        for key in ("total", "permissions", "viewer", "space_context", "next_cursor", "has_more"):
            if key in payload:
                data[key] = payload[key]
    return data


def _build_initial_data(
    *,
    app_name: str,
    action: str,
    space_id: str,
    context_key: str | None,
    context_item: dict[str, Any] | None,
    whoami_payload: dict[str, Any] | None,
    collection_payload: Any | None,
    summary: str | None,
) -> dict[str, Any]:
    if app_name == "context" and context_key:
        item = context_item or {"key": context_key}
        return {
            "kind": "context",
            "version": 1,
            "action": "get",
            "items": [item],
            "keys": [context_key],
            "count": 1,
            "selected_key": context_key,
            "summary": summary,
            "source": "axctl_apps_signal",
        }

    if app_name == "whoami":
        return _whoami_initial_data(whoami_payload or {}, space_id=space_id, summary=summary)

    kind = app_name.split("/", 1)[0]
    if kind in {"agents", "spaces", "tasks"} and collection_payload is not None:
        return _collection_initial_data(
            kind=kind,
            action=action,
            space_id=space_id,
            payload=collection_payload,
            summary=summary,
        )

    return {
        "kind": kind,
        "version": 1,
        "action": action,
        "items": [],
        "keys": [context_key] if context_key else [],
        "count": 1 if context_key else 0,
        "selected_key": context_key,
        "summary": summary,
        "space_id": space_id,
        "source": "axctl_apps_signal",
    }


def _build_signal_metadata(
    *,
    app_name: str,
    resource_uri: str,
    title: str,
    action: str,
    space_id: str,
    context_key: str | None,
    context_item: dict[str, Any] | None,
    whoami_payload: dict[str, Any] | None,
    collection_payload: Any | None,
    summary: str | None,
    target: str | None,
    alert_kind: str | None,
    severity: str,
) -> tuple[dict[str, Any], str]:
    tool_name = app_name.split("/", 1)[0]
    tool_call_id = str(uuid.uuid4())
    arguments: dict[str, Any] = {"action": action, "space_id": space_id}
    if context_key:
        arguments["key"] = context_key

    initial_data = _build_initial_data(
        app_name=app_name,
        action=action,
        space_id=space_id,
        context_key=context_key,
        context_item=context_item,
        whoami_payload=whoami_payload,
        collection_payload=collection_payload,
        summary=summary,
    )
    card_id = f"app-signal:{tool_call_id}"
    card = {
        "card_id": card_id,
        "type": "context" if tool_name == "context" else "result",
        "version": 1,
        "payload": {
            "title": title,
            "summary": summary,
            "tool_name": tool_name,
            "resource_uri": resource_uri,
            "context_key": context_key,
            "severity": severity,
            "source": "axctl_apps_signal",
        },
    }
    metadata: dict[str, Any] = {
        "ui": {
            "cards": [card],
            "widget": {
                "kind": "mcp_app",
                "tool_name": tool_name,
                "tool_action": action,
                "tool_call_id": tool_call_id,
                "resource_uri": resource_uri,
                "display_mode": "inline",
                "lifecycle": "complete",
                "revision": 1,
                "title": title,
                "arguments": arguments,
                "initial_data": initial_data,
                "result_kind": tool_name,
                "source": "axctl_apps_signal",
            },
        },
        "app_signal": {
            "app": app_name,
            "resource_uri": resource_uri,
            "tool_call_id": tool_call_id,
            "context_key": context_key,
            "source": "axctl_apps_signal",
        },
    }
    if alert_kind:
        alert: dict[str, Any] = {
            "kind": alert_kind,
            "severity": severity,
            "source": "axctl_apps_signal",
            "context_key": context_key,
            "tool_call_id": tool_call_id,
        }
        if title:
            alert["title"] = title
        if summary:
            alert["summary"] = summary
        target_agent = _mention_prefix(target).lstrip("@")
        if target_agent:
            alert["target_agent"] = target_agent
            alert["response_required"] = True
        metadata["alert"] = alert
    return metadata, tool_call_id


def _default_signal_message(*, title: str, summary: str | None, context_key: str | None) -> str:
    details = summary or (f"Context key `{context_key}`" if context_key else None)
    return f"{title}: {details}" if details else f"{title} signal ready."


def _is_passive_signal(*, to: str | None, alert_kind: str | None) -> bool:
    return not _mention_prefix(to) and not alert_kind


@app.command("list")
def list_apps(as_json: bool = JSON_OPTION):
    """List known MCP app surfaces the CLI adapter can signal."""
    rows = [
        {"app": name, "title": spec["title"], "resource_uri": spec["resource_uri"]}
        for name, spec in sorted(APP_SPECS.items())
    ]
    if as_json:
        print_json(rows)
    else:
        print_table(["App", "Title", "Resource URI"], rows, keys=["app", "title", "resource_uri"])


@app.command("signal")
def signal(
    app_name: str = typer.Argument(..., help="App key, e.g. context, agents, tasks"),
    action: str = typer.Option("list", "--action", help="Tool action represented by this signal"),
    context_key: Optional[str] = typer.Option(None, "--context-key", "-k", help="Context key to open/select"),
    title: Optional[str] = typer.Option(None, "--title", help="Signal/widget title"),
    summary: Optional[str] = typer.Option(None, "--summary", help="Short signal summary"),
    message: Optional[str] = typer.Option(None, "--message", "-m", help="Feed message content"),
    to: Optional[str] = typer.Option(None, "--to", help="@mention a user or agent"),
    channel: str = typer.Option("main", "--channel", help="Message channel"),
    alert_kind: Optional[str] = typer.Option(None, "--alert-kind", help="Optional alert kind metadata"),
    severity: str = typer.Option("info", "--severity", help="Alert/signal severity"),
    message_type: str = typer.Option("system", "--message-type", help="Message type to write"),
    space_id: Optional[str] = typer.Option(None, "--space-id", help="Override default space"),
    as_json: bool = JSON_OPTION,
):
    """Write an API-backed signal that opens an existing MCP app panel in the UI."""
    app_key = app_name.strip().lower()
    spec = APP_SPECS.get(app_key)
    if not spec:
        choices = ", ".join(sorted(APP_SPECS))
        typer.echo(f"Error: unknown app '{app_name}'. Known apps: {choices}", err=True)
        raise typer.Exit(1)

    client = get_authoring_client()
    sid = resolve_space_id(client, explicit=space_id)
    resolved_title = title or spec["title"]
    resolved_action = "get" if app_key == "context" and context_key and action == "list" else action

    context_item = None
    if app_key == "context" and context_key:
        try:
            context_item = _context_item_from_response(
                context_key,
                client.get_context(context_key, space_id=sid),
            )
        except httpx.HTTPStatusError as exc:
            handle_error(exc)

    whoami_payload = None
    if app_key == "whoami":
        try:
            whoami_payload = client.whoami()
        except httpx.HTTPStatusError as exc:
            handle_error(exc)

    collection_payload = None
    try:
        if app_key == "agents":
            collection_payload = client.list_agents(space_id=sid, limit=500)
        elif app_key == "spaces":
            collection_payload = client.list_spaces()
        elif app_key == "tasks":
            collection_payload = client.list_tasks(limit=50, space_id=sid)
    except httpx.HTTPStatusError as exc:
        handle_error(exc)

    metadata, tool_call_id = _build_signal_metadata(
        app_name=app_key,
        resource_uri=spec["resource_uri"],
        title=resolved_title,
        action=resolved_action,
        space_id=sid,
        context_key=context_key,
        context_item=context_item,
        whoami_payload=whoami_payload,
        collection_payload=collection_payload,
        summary=summary,
        target=to,
        alert_kind=alert_kind,
        severity=severity,
    )
    if _is_passive_signal(to=to, alert_kind=alert_kind):
        metadata["top_level_ingress"] = False
        metadata["signal_only"] = True
        metadata["app_signal"]["signal_only"] = True

    prefix = _mention_prefix(to)
    body = message or _default_signal_message(
        title=resolved_title,
        summary=summary,
        context_key=context_key,
    )
    if prefix:
        body = f"{prefix} {body}"

    try:
        data = client.send_message(
            sid,
            body,
            channel=channel,
            metadata=metadata,
            message_type=message_type,
        )
    except httpx.HTTPStatusError as exc:
        handle_error(exc)

    result = {
        "message": data.get("message", data),
        "app": app_key,
        "resource_uri": spec["resource_uri"],
        "tool_call_id": tool_call_id,
        "context_key": context_key,
        "channel": channel,
    }
    if as_json:
        print_json(result)
    else:
        msg = result["message"]
        console.print(f"[green]App signal sent.[/green] id={msg.get('id') or msg.get('message_id')}")
        console.print(f"[dim]{app_key} -> {spec['resource_uri']} ({tool_call_id})[/dim]")
