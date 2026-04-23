"""ax gateway — local Gateway control plane."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import typer
from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import gateway as gateway_core
from ..client import AxClient
from ..commands import auth as auth_cmd
from ..commands.bootstrap import (
    _create_agent_in_space,
    _find_agent_in_space,
    _mint_agent_pat,
    _polish_metadata,
)
from ..config import resolve_user_base_url, resolve_user_token
from ..gateway import (
    GatewayDaemon,
    active_gateway_pid,
    active_gateway_pids,
    active_gateway_ui_pid,
    active_gateway_ui_pids,
    agent_dir,
    agent_token_path,
    annotate_runtime_health,
    approve_gateway_approval,
    clear_gateway_ui_state,
    daemon_log_path,
    daemon_status,
    deny_gateway_approval,
    ensure_gateway_identity_binding,
    ensure_local_asset_binding,
    evaluate_runtime_attestation,
    find_agent_entry,
    gateway_dir,
    get_gateway_approval,
    hermes_setup_status,
    infer_asset_descriptor,
    list_gateway_approvals,
    load_gateway_registry,
    load_gateway_session,
    load_recent_gateway_activity,
    ollama_setup_status,
    record_gateway_activity,
    remove_agent_entry,
    save_gateway_registry,
    save_gateway_session,
    ui_log_path,
    ui_status,
    upsert_agent_entry,
    write_gateway_ui_state,
)
from ..gateway_runtime_types import (
    agent_template_definition,
    agent_template_list,
    runtime_type_definition,
    runtime_type_list,
)
from ..output import JSON_OPTION, console, err_console, print_json, print_table

app = typer.Typer(name="gateway", help="Run the local Gateway control plane", no_args_is_help=True)
agents_app = typer.Typer(name="agents", help="Manage Gateway-controlled agents", no_args_is_help=True)
approvals_app = typer.Typer(name="approvals", help="Review and decide Gateway approval requests", no_args_is_help=True)
app.add_typer(agents_app, name="agents")
app.add_typer(approvals_app, name="approvals")

_STATE_STYLES = {
    "running": "green",
    "starting": "cyan",
    "reconnecting": "yellow",
    "stale": "yellow",
    "error": "red",
    "stopped": "dim",
}
_PRESENCE_STYLES = {
    "IDLE": "green",
    "QUEUED": "cyan",
    "WORKING": "green",
    "BLOCKED": "yellow",
    "STALE": "yellow",
    "OFFLINE": "dim",
    "ERROR": "red",
}
_CONFIDENCE_STYLES = {
    "HIGH": "green",
    "MEDIUM": "cyan",
    "LOW": "yellow",
    "BLOCKED": "red",
}
_PRESENCE_ORDER = {
    "ERROR": 0,
    "BLOCKED": 1,
    "WORKING": 2,
    "QUEUED": 3,
    "STALE": 4,
    "OFFLINE": 5,
    "IDLE": 6,
}

_UNSET = object()


def _resolve_gateway_login_token(explicit_token: str | None) -> str:
    if explicit_token and explicit_token.strip():
        return auth_cmd._resolve_login_token(explicit_token)
    existing = resolve_user_token()
    if existing:
        err_console.print("[cyan]Using existing axctl user login for Gateway bootstrap.[/cyan]")
        return existing
    return auth_cmd._resolve_login_token(None)


def _load_gateway_user_client() -> AxClient:
    session = load_gateway_session()
    if not session:
        err_console.print("[red]Gateway is not logged in.[/red] Run `ax gateway login` first.")
        raise typer.Exit(1)
    token = str(session.get("token") or "")
    if not token:
        err_console.print("[red]Gateway session is missing its bootstrap token.[/red]")
        raise typer.Exit(1)
    if not token.startswith("axp_u_"):
        err_console.print("[red]Gateway bootstrap currently requires a user PAT (axp_u_).[/red]")
        raise typer.Exit(1)
    return AxClient(base_url=str(session.get("base_url") or auth_cmd.DEFAULT_LOGIN_BASE_URL), token=token)


def _load_gateway_session_or_exit() -> dict:
    session = load_gateway_session()
    if not session:
        err_console.print("[red]Gateway is not logged in.[/red] Run `ax gateway login` first.")
        raise typer.Exit(1)
    return session


def _save_agent_token(name: str, token: str) -> Path:
    token_path = agent_token_path(name)
    token_path.write_text(token.strip() + "\n")
    token_path.chmod(0o600)
    return token_path


def _load_managed_agent_or_exit(name: str) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    return entry


def _load_managed_agent_client(entry: dict) -> AxClient:
    token_file = Path(str(entry.get("token_file") or "")).expanduser()
    if not token_file.exists():
        err_console.print(f"[red]Managed agent token is missing:[/red] {token_file}")
        raise typer.Exit(1)
    token = token_file.read_text().strip()
    if not token:
        err_console.print(f"[red]Managed agent token file is empty:[/red] {token_file}")
        raise typer.Exit(1)
    return AxClient(
        base_url=str(entry.get("base_url") or ""),
        token=token,
        agent_name=str(entry.get("name") or ""),
        agent_id=str(entry.get("agent_id") or "") or None,
    )


def _normalize_runtime_type(runtime_type: str) -> str:
    try:
        return str(runtime_type_definition(runtime_type)["id"])
    except KeyError as exc:
        raise ValueError("Unsupported runtime type. Use echo, exec, hermes_sentinel, sentinel_cli, or inbox.") from exc


def _validate_runtime_registration(runtime_type: str, exec_cmd: str | None) -> None:
    definition = runtime_type_definition(runtime_type)
    required = set(definition.get("requires") or [])
    if "exec_command" in required and not exec_cmd:
        raise ValueError("Exec runtimes require --exec.")
    if "exec_command" not in required and exec_cmd:
        raise ValueError("This runtime does not accept --exec.")


def _register_managed_agent(
    *,
    name: str,
    runtime_type: str | None = None,
    template_id: str | None = None,
    exec_cmd: str | None = None,
    workdir: str | None = None,
    ollama_model: str | None = None,
    space_id: str | None = None,
    audience: str = "both",
    description: str | None = None,
    model: str | None = None,
    start: bool = True,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")
    template = None
    if template_id:
        try:
            template = agent_template_definition(template_id)
        except KeyError as exc:
            raise ValueError(f"Unknown template: {template_id}") from exc
        if not bool(template.get("launchable", True)):
            raise ValueError(f"Template {template['label']} is not launchable yet.")
        defaults = template.get("defaults") or {}
        runtime_type = runtime_type or str(defaults.get("runtime_type") or "")
        exec_cmd = exec_cmd or (str(defaults.get("exec_command") or "").strip() or None)
        workdir = workdir or (str(defaults.get("workdir") or "").strip() or None)
    runtime_type = runtime_type or "echo"
    runtime_type = _normalize_runtime_type(runtime_type)
    normalized_ollama_model = str(ollama_model or "").strip() or None
    template_effective_id = str(template.get("id") if template else "").strip().lower()
    if normalized_ollama_model and template_effective_id != "ollama":
        raise ValueError("--ollama-model is only supported with the Ollama template.")
    if template_effective_id == "ollama" and not normalized_ollama_model:
        normalized_ollama_model = str(ollama_setup_status().get("recommended_model") or "").strip() or None
    _validate_runtime_registration(runtime_type, exec_cmd)

    session = _load_gateway_session_or_exit()
    selected_space = space_id or session.get("space_id")
    if not selected_space:
        raise ValueError("No space selected. Use --space-id or re-run `ax gateway login` with one.")

    client = _load_gateway_user_client()
    existing = _find_agent_in_space(client, name, selected_space)
    if existing:
        agent = existing
        if description or model:
            client.update_agent(name, **{k: v for k, v in {"description": description, "model": model}.items() if v})
    else:
        agent = _create_agent_in_space(
            client,
            name=name,
            space_id=selected_space,
            description=description,
            model=model,
        )
    _polish_metadata(client, name=name, bio=None, specialization=None, system_prompt=None)

    agent_id = str(agent.get("id") or agent.get("agent_id") or "")
    token, pat_source = _mint_agent_pat(
        client,
        agent_id=agent_id,
        agent_name=name,
        audience=audience,
        expires_in_days=90,
        pat_name=f"gateway-{name}",
        space_id=selected_space,
    )
    token_file = _save_agent_token(name, token)

    registry = load_gateway_registry()
    entry = upsert_agent_entry(
        registry,
        {
            "name": name,
            "template_id": template.get("id") if template else None,
            "template_label": template.get("label") if template else None,
            "agent_id": agent_id,
            "space_id": selected_space,
            "base_url": session["base_url"],
            "runtime_type": runtime_type,
            "exec_command": exec_cmd,
            "workdir": workdir,
            "ollama_model": normalized_ollama_model,
            "token_file": str(token_file),
            "desired_state": "running" if start else "stopped",
            "effective_state": "stopped",
            "transport": "gateway",
            "credential_source": "gateway",
            "last_error": None,
            "backlog_depth": 0,
            "processed_count": 0,
            "dropped_count": 0,
            "pat_source": pat_source,
            "added_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    ensure_gateway_identity_binding(registry, entry, session=session, created_via="cli")
    entry.update(evaluate_runtime_attestation(registry, entry))
    hermes_status = hermes_setup_status(entry)
    if not hermes_status.get("ready", True):
        entry["effective_state"] = "error"
        entry["last_error"] = str(
            hermes_status.get("detail") or hermes_status.get("summary") or "Hermes setup is incomplete."
        )
        entry["current_activity"] = str(hermes_status.get("summary") or "Hermes setup is incomplete.")
    elif hermes_status.get("resolved_path"):
        entry["hermes_repo_path"] = str(hermes_status["resolved_path"])
    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_added",
        entry=entry,
        space_id=selected_space,
        token_file=str(token_file),
    )
    return annotate_runtime_health(entry, registry=registry)


def _update_managed_agent(
    *,
    name: str,
    template_id: str | None = None,
    runtime_type: str | None = None,
    exec_cmd: str | object = _UNSET,
    workdir: str | object = _UNSET,
    ollama_model: str | object = _UNSET,
    description: str | None = None,
    model: str | None = None,
    desired_state: str | None = None,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")

    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")

    template = None
    if template_id:
        try:
            template = agent_template_definition(template_id)
        except KeyError as exc:
            raise ValueError(f"Unknown template: {template_id}") from exc
        if not bool(template.get("launchable", True)):
            raise ValueError(f"Template {template['label']} is not launchable yet.")

    runtime_candidate = (
        runtime_type or (template.get("defaults") or {}).get("runtime_type") if template else runtime_type
    )
    runtime_effective = str(runtime_candidate or entry.get("runtime_type") or "echo")
    runtime_effective = _normalize_runtime_type(runtime_effective)
    template_effective_id = str(template.get("id") if template else entry.get("template_id") or "").strip().lower()

    if template:
        defaults = template.get("defaults") or {}
        exec_effective = (
            str(exec_cmd).strip() or None
            if exec_cmd is not _UNSET
            else (str(defaults.get("exec_command") or "").strip() or None)
        )
        workdir_effective = (
            str(workdir).strip() or None
            if workdir is not _UNSET
            else (str(defaults.get("workdir") or "").strip() or None)
        )
    else:
        exec_effective = (
            str(entry.get("exec_command") or "").strip() or None
            if exec_cmd is _UNSET
            else (str(exec_cmd).strip() or None)
        )
        workdir_effective = (
            str(entry.get("workdir") or "").strip() or None if workdir is _UNSET else (str(workdir).strip() or None)
        )

    if ollama_model is _UNSET:
        ollama_model_effective = str(entry.get("ollama_model") or "").strip() or None
    else:
        ollama_model_effective = str(ollama_model).strip() or None
    if ollama_model_effective and template_effective_id != "ollama":
        raise ValueError("--ollama-model is only supported with the Ollama template.")
    if template_effective_id == "ollama" and ollama_model is _UNSET and not ollama_model_effective:
        ollama_model_effective = str(ollama_setup_status().get("recommended_model") or "").strip() or None

    _validate_runtime_registration(runtime_effective, exec_effective)

    if desired_state is not None:
        normalized_desired = desired_state.lower().strip()
        if normalized_desired not in {"running", "stopped"}:
            raise ValueError("Desired state must be running or stopped.")
        entry["desired_state"] = normalized_desired

    session = _load_gateway_session_or_exit()
    if description or model:
        client = _load_gateway_user_client()
        client.update_agent(name, **{k: v for k, v in {"description": description, "model": model}.items() if v})

    if template:
        entry["template_id"] = template.get("id")
        entry["template_label"] = template.get("label")
    entry["runtime_type"] = runtime_effective
    entry["exec_command"] = exec_effective
    entry["workdir"] = workdir_effective
    if template_effective_id == "ollama":
        entry["ollama_model"] = ollama_model_effective
    else:
        entry.pop("ollama_model", None)
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    entry.setdefault("transport", "gateway")
    entry.setdefault("credential_source", "gateway")

    if template and template.get("id") != "hermes":
        entry.pop("hermes_repo_path", None)

    ensure_gateway_identity_binding(registry, entry, session=session)
    ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True, replace_existing=True)
    entry.update(evaluate_runtime_attestation(registry, entry))
    hermes_status = hermes_setup_status(entry)
    if not hermes_status.get("ready", True):
        entry["effective_state"] = "error"
        entry["last_error"] = str(
            hermes_status.get("detail") or hermes_status.get("summary") or "Hermes setup is incomplete."
        )
        entry["current_activity"] = str(hermes_status.get("summary") or "Hermes setup is incomplete.")
    elif hermes_status.get("resolved_path"):
        entry["hermes_repo_path"] = str(hermes_status["resolved_path"])

    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_updated",
        entry=entry,
        template_id=entry.get("template_id"),
        runtime_type=runtime_effective,
        workdir=workdir_effective,
        exec_command=exec_effective,
        desired_state=entry.get("desired_state"),
    )
    return annotate_runtime_health(entry, registry=registry)


def _set_managed_agent_desired_state(name: str, desired_state: str) -> dict:
    desired_state = desired_state.lower().strip()
    if desired_state not in {"running", "stopped"}:
        raise ValueError("Desired state must be running or stopped.")
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    entry["desired_state"] = desired_state
    save_gateway_registry(registry)
    event = "managed_agent_desired_running" if desired_state == "running" else "managed_agent_desired_stopped"
    record_gateway_activity(event, entry=entry)
    return annotate_runtime_health(entry, registry=registry)


def _remove_managed_agent(name: str) -> dict:
    registry = load_gateway_registry()
    entry = remove_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    save_gateway_registry(registry)
    token_file = Path(str(entry.get("token_file") or ""))
    if token_file.exists():
        token_file.unlink()
    record_gateway_activity("managed_agent_removed", entry=entry)
    return entry


def _identity_space_send_guard(entry: dict, *, explicit_space_id: str | None = None) -> dict:
    registry = load_gateway_registry()
    stored = find_agent_entry(registry, str(entry.get("name") or "")) or entry
    ensure_gateway_identity_binding(registry, stored, session=load_gateway_session())
    snapshot = annotate_runtime_health(stored, registry=registry, explicit_space_id=explicit_space_id)
    save_gateway_registry(registry)
    if str(snapshot.get("confidence") or "").upper() == "BLOCKED":
        reason = str(snapshot.get("confidence_reason") or "blocked")
        detail = str(snapshot.get("confidence_detail") or "Gateway blocked this action.")
        raise ValueError(f"{detail} ({reason})")
    return snapshot


def _sync_passive_queue_after_manual_send(
    *,
    entry: dict,
    handled_message_id: str | None,
    reply_message_id: str | None,
    reply_preview: str | None,
) -> None:
    runtime_type = str(entry.get("runtime_type") or "").lower()
    if runtime_type not in {"inbox", "passive", "monitor"}:
        return

    pending_items = gateway_core.remove_agent_pending_message(str(entry.get("name") or ""), handled_message_id)
    registry = load_gateway_registry()
    stored = find_agent_entry(registry, str(entry.get("name") or "")) or entry
    backlog_depth = len(pending_items)
    last_pending = pending_items[-1] if pending_items else {}

    if handled_message_id:
        stored["processed_count"] = int(stored.get("processed_count") or 0) + 1
        stored["last_work_completed_at"] = datetime.now(timezone.utc).isoformat()

    stored["backlog_depth"] = backlog_depth
    stored["current_status"] = "queued" if backlog_depth > 0 else None
    stored["current_activity"] = (
        gateway_core._gateway_pickup_activity(runtime_type, backlog_depth)[:240] if backlog_depth > 0 else None
    )
    stored["last_reply_message_id"] = reply_message_id or stored.get("last_reply_message_id")
    stored["last_reply_preview"] = reply_preview or stored.get("last_reply_preview")
    if last_pending:
        stored["last_received_message_id"] = last_pending.get("message_id")
        stored["last_work_received_at"] = (
            last_pending.get("queued_at") or last_pending.get("created_at") or stored.get("last_work_received_at")
        )
    elif handled_message_id:
        stored["last_received_message_id"] = None
        stored["last_work_received_at"] = None

    save_gateway_registry(registry)
    if handled_message_id:
        record_gateway_activity(
            "manual_queue_acknowledged",
            entry=stored,
            message_id=handled_message_id,
            reply_message_id=reply_message_id,
            backlog_depth=backlog_depth,
        )


def _send_from_managed_agent(
    *,
    name: str,
    content: str,
    to: str | None = None,
    parent_id: str | None = None,
    sent_via: str = "gateway_cli",
    metadata_extra: dict[str, object] | None = None,
) -> dict:
    if not content.strip():
        raise ValueError("Message content is required.")
    entry = _load_managed_agent_or_exit(name)
    snapshot = _identity_space_send_guard(entry)
    client = _load_managed_agent_client(entry)
    space_id = str(snapshot.get("active_space_id") or entry.get("space_id") or "")
    if not space_id:
        raise ValueError(f"Managed agent is missing a space id: @{name}")

    message_content = content.strip()
    mention = str(to or "").strip().lstrip("@")
    if mention:
        prefix = f"@{mention}"
        if not message_content.startswith(prefix):
            message_content = f"{prefix} {message_content}".strip()

    metadata = {
        "control_plane": "gateway",
        "gateway": {
            "managed": True,
            "agent_name": entry.get("name"),
            "agent_id": entry.get("agent_id"),
            "runtime_type": entry.get("runtime_type"),
            "transport": entry.get("transport", "gateway"),
            "credential_source": entry.get("credential_source", "gateway"),
            "sent_via": sent_via,
        },
    }
    if metadata_extra:
        gateway_meta = metadata["gateway"]
        if isinstance(gateway_meta, dict):
            gateway_meta.update(metadata_extra)
    result = client.send_message(
        space_id,
        message_content,
        agent_id=str(entry.get("agent_id") or "") or None,
        parent_id=parent_id or None,
        metadata=metadata,
    )
    payload = result.get("message", result) if isinstance(result, dict) else result
    if isinstance(payload, dict):
        record_gateway_activity(
            "manual_message_sent",
            entry=entry,
            message_id=payload.get("id"),
            reply_preview=message_content[:120] or None,
        )
        _sync_passive_queue_after_manual_send(
            entry=entry,
            handled_message_id=parent_id,
            reply_message_id=str(payload.get("id") or "") or None,
            reply_preview=message_content[:120] or None,
        )
    return {"agent": entry.get("name"), "message": payload, "content": message_content}


def _gateway_test_sender_name(space_id: str) -> str:
    normalized = "".join(ch for ch in str(space_id or "") if ch.isalnum()).lower()
    suffix = normalized[:8] or "default"
    return f"switchboard-{suffix}"


def _ensure_gateway_test_sender(target_entry: dict) -> dict:
    target_space = str(target_entry.get("space_id") or "").strip()
    if not target_space:
        raise ValueError("Managed agent is missing a space id for Gateway test delivery.")
    sender_name = _gateway_test_sender_name(target_space)
    registry = load_gateway_registry()
    existing = find_agent_entry(registry, sender_name)
    if existing:
        return annotate_runtime_health(existing, registry=registry)
    return _register_managed_agent(
        name=sender_name,
        template_id="inbox",
        space_id=target_space,
        description="Gateway-managed passive sender for agent-authored tests.",
        start=True,
    )


def _status_payload(*, activity_limit: int = 10) -> dict:
    daemon = daemon_status()
    ui = ui_status()
    session = load_gateway_session()
    registry = daemon["registry"]
    agents = [annotate_runtime_health(agent, registry=registry) for agent in registry.get("agents", [])]
    approvals = list_gateway_approvals()
    pending_approvals = [item for item in approvals if str(item.get("status") or "") == "pending"]
    live_agents = [a for a in agents if str(a.get("mode") or "") == "LIVE"]
    on_demand_agents = [a for a in agents if str(a.get("mode") or "") == "ON-DEMAND"]
    inbox_agents = [a for a in agents if str(a.get("mode") or "") == "INBOX"]
    connected_agents = [a for a in agents if bool(a.get("connected"))]
    stale_agents = [a for a in agents if str(a.get("presence") or "") == "STALE"]
    offline_agents = [a for a in agents if str(a.get("presence") or "") == "OFFLINE"]
    errored_agents = [a for a in agents if str(a.get("presence") or "") == "ERROR"]
    low_confidence_agents = [a for a in agents if str(a.get("confidence") or "") in {"LOW", "BLOCKED"}]
    blocked_agents = [a for a in agents if str(a.get("confidence") or "") == "BLOCKED"]
    gateway = dict(registry.get("gateway", {}))
    if not daemon["running"]:
        gateway["effective_state"] = "stopped"
        gateway["pid"] = None
    payload = {
        "gateway_dir": str(gateway_dir()),
        "connected": bool(session),
        "base_url": session.get("base_url") if session else None,
        "space_id": session.get("space_id") if session else None,
        "space_name": session.get("space_name") if session else None,
        "user": session.get("username") if session else None,
        "daemon": {
            "running": daemon["running"],
            "pid": daemon["pid"],
        },
        "ui": {
            "running": ui["running"],
            "pid": ui["pid"],
            "host": ui["host"],
            "port": ui["port"],
            "url": ui["url"],
            "log_path": ui["log_path"],
        },
        "gateway": gateway,
        "agents": agents,
        "approvals": approvals,
        "recent_activity": load_recent_gateway_activity(limit=activity_limit),
        "summary": {
            "managed_agents": len(agents),
            "live_agents": len(live_agents),
            "on_demand_agents": len(on_demand_agents),
            "inbox_agents": len(inbox_agents),
            "connected_agents": len(connected_agents),
            "stale_agents": len(stale_agents),
            "offline_agents": len(offline_agents),
            "errored_agents": len(errored_agents),
            "low_confidence_agents": len(low_confidence_agents),
            "blocked_agents": len(blocked_agents),
            "pending_approvals": len(pending_approvals),
        },
    }
    alerts = _gateway_alerts(payload)
    payload["alerts"] = alerts
    payload["summary"]["alert_count"] = len(alerts)
    return payload


def _gateway_alerts(payload: dict, *, limit: int = 6) -> list[dict]:
    alerts: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def push(severity: str, title: str, detail: str, *, agent_name: str | None = None) -> None:
        key = (severity, title, agent_name or "")
        if key in seen:
            return
        seen.add(key)
        alerts.append(
            {
                "severity": severity,
                "title": title,
                "detail": detail,
                "agent_name": agent_name,
            }
        )

    if not payload.get("connected"):
        push("error", "Gateway is not logged in", "Run `ax gateway login` to bootstrap the local control plane.")
    elif not payload.get("daemon", {}).get("running"):
        push(
            "error",
            "Gateway daemon is stopped",
            "Start it with `uv run ax gateway start` or relaunch the local service.",
        )

    if not payload.get("ui", {}).get("running"):
        push(
            "warning", "Gateway UI is stopped", "Start it with `uv run ax gateway start` to launch the local dashboard."
        )

    for agent in payload.get("agents", []):
        name = str(agent.get("name") or "")
        presence = str(agent.get("presence") or "").upper()
        approval_state = str(agent.get("approval_state") or "").lower()
        attestation_state = str(agent.get("attestation_state") or "").lower()
        preview = str(agent.get("last_reply_preview") or "")
        lowered_preview = preview.lower()
        setup_error_preview = (
            preview.startswith("(stderr:")
            or " repo not found" in lowered_preview
            or lowered_preview.startswith("ollama bridge failed:")
        )
        if approval_state == "pending":
            detail = str(agent.get("confidence_detail") or "Gateway needs approval before this runtime can be trusted.")
            push("warning", f"@{name} needs Gateway approval", detail, agent_name=name)
        elif approval_state == "rejected" or attestation_state == "blocked":
            detail = str(agent.get("confidence_detail") or "Gateway blocked this runtime.")
            push("error", f"@{name} is blocked by Gateway", detail, agent_name=name)
        elif attestation_state == "drifted":
            detail = str(agent.get("confidence_detail") or "Runtime changed since approval and needs review.")
            push("warning", f"@{name} changed since approval", detail, agent_name=name)
        elif presence == "BLOCKED":
            detail = str(
                agent.get("confidence_detail")
                or "Gateway blocked this runtime until identity, space, or approval state is fixed."
            )
            push("error", f"@{name} is blocked", detail, agent_name=name)
        elif presence == "ERROR":
            if setup_error_preview:
                push("error", f"@{name} has a runtime setup error", preview[:180], agent_name=name)
            else:
                detail = str(agent.get("confidence_detail") or agent.get("last_error") or "Runtime reported an error.")
                push("error", f"@{name} hit an error", detail, agent_name=name)
        elif presence == "STALE":
            detail = f"No heartbeat for {_format_age(agent.get('last_seen_age_seconds'))}."
            push("warning", f"@{name} looks stale", detail, agent_name=name)
        elif presence == "OFFLINE" and str(agent.get("mode") or "") == "LIVE":
            detail = str(
                agent.get("confidence_detail")
                or "Expected a live runtime, but Gateway does not currently have a working path."
            )
            push("warning", f"@{name} is offline", detail, agent_name=name)
        if setup_error_preview and presence != "ERROR":
            push("error", f"@{name} has a runtime setup error", preview[:180], agent_name=name)
        if int(agent.get("backlog_depth") or 0) > 0 and presence in {"OFFLINE", "ERROR", "STALE"}:
            detail = f"{agent.get('backlog_depth')} queued item(s) may be stuck until the agent is healthy."
            push("warning", f"@{name} has queued work", detail, agent_name=name)

    for item in reversed(payload.get("recent_activity", [])):
        event = str(item.get("event") or "")
        if event == "gateway_start_blocked":
            existing = item.get("existing_pid") or item.get("existing_pids")
            push("warning", "Another Gateway instance is already running", f"Existing process: {existing}.")
        elif event in {"listener_error", "listener_timeout"}:
            agent_name = str(item.get("agent_name") or "")
            detail = str(item.get("error") or "Listener lost contact and is reconnecting.")
            push("warning", f"@{agent_name} had a listener interruption", detail, agent_name=agent_name or None)
        if len(alerts) >= limit:
            break

    return alerts[:limit]


def _runtime_types_payload() -> dict:
    return {"runtime_types": runtime_type_list(), "count": len(runtime_type_list())}


def _annotate_template_taxonomy(definition: dict) -> dict:
    enriched = dict(definition)
    descriptor = infer_asset_descriptor(
        {
            "template_id": definition.get("id"),
            "template_label": definition.get("label"),
            "runtime_type": definition.get("runtime_type"),
            "telemetry_shape": definition.get("telemetry_shape"),
            "asset_class": definition.get("asset_class"),
            "intake_model": definition.get("intake_model"),
            "worker_model": definition.get("worker_model"),
            "trigger_sources": definition.get("trigger_sources"),
            "return_paths": definition.get("return_paths"),
            "tags": definition.get("tags"),
            "capabilities": definition.get("capabilities"),
            "constraints": definition.get("constraints"),
            "addressable": definition.get("addressable"),
            "messageable": definition.get("messageable"),
            "schedulable": definition.get("schedulable"),
            "externally_triggered": definition.get("externally_triggered"),
        }
    )
    enriched.update(
        {
            "asset_class": descriptor["asset_class"],
            "intake_model": descriptor["intake_model"],
            "worker_model": descriptor.get("worker_model"),
            "trigger_sources": descriptor["trigger_sources"],
            "return_paths": descriptor["return_paths"],
            "telemetry_shape": descriptor["telemetry_shape"],
            "asset_type_label": descriptor["type_label"],
            "output_label": descriptor["output_label"],
            "asset_descriptor": descriptor,
        }
    )
    return enriched


def _agent_templates_payload() -> dict:
    templates = [_annotate_template_taxonomy(item) for item in agent_template_list()]
    ollama_status = ollama_setup_status()
    for item in templates:
        if str(item.get("id") or "").strip().lower() != "ollama":
            continue
        defaults = dict(item.get("defaults") or {})
        recommended_model = str(ollama_status.get("recommended_model") or "").strip() or None
        if recommended_model and not str(defaults.get("ollama_model") or "").strip():
            defaults["ollama_model"] = recommended_model
        item["defaults"] = defaults
        item["ollama_server_reachable"] = bool(ollama_status.get("server_reachable"))
        item["ollama_available_models"] = list(ollama_status.get("available_models") or [])
        item["ollama_local_models"] = list(ollama_status.get("local_models") or [])
        item["ollama_recommended_model"] = recommended_model
        item["ollama_summary"] = str(ollama_status.get("summary") or "")
    return {"templates": templates, "count": len(templates)}


def _agent_detail_payload(name: str, *, activity_limit: int = 12) -> dict | None:
    payload = _status_payload(activity_limit=activity_limit)
    entry = next((agent for agent in payload["agents"] if str(agent.get("name") or "").lower() == name.lower()), None)
    if not entry:
        return None
    activity = load_recent_gateway_activity(limit=activity_limit, agent_name=name)
    return {
        "gateway": {
            "connected": payload["connected"],
            "base_url": payload["base_url"],
            "space_id": payload["space_id"],
            "daemon": payload["daemon"],
        },
        "agent": entry,
        "recent_activity": activity,
    }


def _approval_rows_payload(*, status: str | None = None) -> dict:
    approvals = list_gateway_approvals(status=status)
    return {
        "approvals": approvals,
        "count": len(approvals),
        "pending": len([item for item in approvals if str(item.get("status") or "") == "pending"]),
    }


def _approval_detail_payload(approval_id: str) -> dict:
    approval = get_gateway_approval(approval_id)
    return {"approval": approval}


def _recommended_test_message(entry: dict) -> str:
    template_id = str(entry.get("template_id") or "").strip()
    if template_id:
        try:
            template = agent_template_definition(template_id)
            message = str(template.get("recommended_test_message") or "").strip()
            if message:
                return message
        except KeyError:
            pass
    runtime_type = str(entry.get("runtime_type") or "").lower()
    if runtime_type == "echo":
        return "gateway test ping"
    if runtime_type == "inbox":
        return "Queue this test job, mark it received, and do not reply inline."
    return "Reply with exactly: Gateway test OK."


def _send_gateway_test_to_managed_agent(
    name: str,
    *,
    content: str | None = None,
    author: str = "agent",
    sender_agent: str | None = None,
) -> dict:
    entry = _load_managed_agent_or_exit(name)
    space_id = str(entry.get("space_id") or "")
    if not space_id:
        raise ValueError(f"Managed agent is missing a space id: @{name}")

    prompt = (content or "").strip() or _recommended_test_message(entry)
    target = str(entry.get("name") or "").lstrip("@")
    normalized_author = str(author or "agent").strip().lower()
    if normalized_author not in {"agent", "user"}:
        raise ValueError("Gateway test author must be one of: agent, user.")

    sender_name = None
    if normalized_author == "agent":
        sender_name = str(sender_agent or "").strip() or str(_ensure_gateway_test_sender(entry).get("name") or "")
        if not sender_name:
            raise ValueError("Gateway could not resolve a managed sender for the test message.")
        result = _send_from_managed_agent(
            name=sender_name,
            content=prompt,
            to=target,
            sent_via="gateway_test",
            metadata_extra={
                "managed_target": True,
                "target_agent_name": entry.get("name"),
                "target_agent_id": entry.get("agent_id"),
                "target_template": entry.get("template_id"),
                "target_runtime_type": entry.get("runtime_type"),
                "test_author": "agent",
            },
        )
        payload = result.get("message", result) if isinstance(result, dict) else result
        message_content = str(result.get("content") or f"@{target} {prompt}".strip())
    else:
        client = _load_gateway_user_client()
        message_content = f"@{target} {prompt}".strip()
        metadata = {
            "control_plane": "gateway",
            "gateway": {
                "managed_target": True,
                "target_agent_name": entry.get("name"),
                "target_agent_id": entry.get("agent_id"),
                "target_template": entry.get("template_id"),
                "target_runtime_type": entry.get("runtime_type"),
                "sent_via": "gateway_test",
                "test_author": "user",
            },
        }
        result = client.send_message(space_id, message_content, metadata=metadata)
        payload = result.get("message", result) if isinstance(result, dict) else result

    if isinstance(payload, dict):
        record_gateway_activity(
            "gateway_test_sent",
            entry=entry,
            message_id=payload.get("id"),
            reply_preview=message_content[:120] or None,
            sender_agent_name=sender_name,
            test_author=normalized_author,
        )
    return {
        "target_agent": entry.get("name"),
        "sender_agent": sender_name,
        "author": normalized_author,
        "message": payload,
        "content": message_content,
        "recommended_prompt": prompt,
    }


def _doctor_result_status(checks: list[dict]) -> str:
    statuses = {str(item.get("status") or "").strip().lower() for item in checks}
    if "failed" in statuses:
        return "failed"
    if "warning" in statuses:
        return "warning"
    return "passed"


def _doctor_summary(checks: list[dict], status: str) -> str:
    failures = [
        str(item.get("detail") or item.get("name") or "").strip()
        for item in checks
        if str(item.get("status") or "").strip().lower() == "failed"
    ]
    warnings = [
        str(item.get("detail") or item.get("name") or "").strip()
        for item in checks
        if str(item.get("status") or "").strip().lower() == "warning"
    ]
    if status == "failed" and failures:
        return failures[0]
    if status == "warning" and warnings:
        return warnings[0]
    return "Gateway path looks healthy."


def _store_doctor_result(name: str, result: dict[str, object]) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    completed_at = str(result.get("completed_at") or datetime.now(timezone.utc).isoformat())
    entry["last_doctor_result"] = result
    entry["last_doctor_at"] = completed_at
    if str(result.get("status") or "").lower() != "failed":
        entry["last_successful_doctor_at"] = completed_at
    save_gateway_registry(registry)
    record_gateway_activity(
        "doctor_completed",
        entry=entry,
        activity_message=str(result.get("summary") or ""),
        error=None if str(result.get("status") or "").lower() != "failed" else str(result.get("summary") or ""),
    )
    return annotate_runtime_health(entry, registry=registry)


def _run_gateway_doctor(name: str, *, send_test: bool = False) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    ensure_gateway_identity_binding(registry, entry, session=load_gateway_session(), verify_spaces=False)
    snapshot = annotate_runtime_health(entry, registry=registry)
    checks: list[dict[str, str]] = []
    asset_class = str(snapshot.get("asset_class") or "")
    intake_model = str(snapshot.get("intake_model") or "")
    return_paths = [str(item) for item in (snapshot.get("return_paths") or []) if str(item)]

    def add_check(check_name: str, status: str, detail: str) -> None:
        checks.append({"name": check_name, "status": status, "detail": detail})

    def has_check(check_name: str) -> bool:
        return any(str(item.get("name") or "") == check_name for item in checks)

    session = load_gateway_session()
    add_check(
        "gateway_auth",
        "passed" if session else "failed",
        "Gateway bootstrap session is present." if session else "Gateway is not logged in.",
    )

    identity_status = str(snapshot.get("identity_status") or "").lower()
    if identity_status == "verified":
        add_check(
            "identity_binding",
            "passed",
            f"Gateway is acting as {snapshot.get('acting_agent_name') or entry.get('name')}.",
        )
    elif identity_status == "bootstrap_only":
        add_check(
            "identity_binding",
            "failed",
            "Gateway would need to use a bootstrap credential for an agent-authored action.",
        )
    else:
        add_check(
            "identity_binding",
            "failed",
            str(snapshot.get("confidence_detail") or "Gateway does not have a valid acting identity binding."),
        )

    environment_status = str(snapshot.get("environment_status") or "").lower()
    if environment_status == "environment_allowed":
        add_check(
            "environment_binding",
            "passed",
            f"Requested environment matches {snapshot.get('environment_label') or snapshot.get('base_url') or entry.get('base_url')}.",
        )
    elif environment_status == "environment_mismatch":
        add_check(
            "environment_binding",
            "failed",
            str(snapshot.get("confidence_detail") or "Requested environment does not match the bound environment."),
        )
    else:
        add_check("environment_binding", "warning", "Gateway could not fully verify the bound environment.")

    allowed_spaces = snapshot.get("allowed_spaces") if isinstance(snapshot.get("allowed_spaces"), list) else []
    if allowed_spaces:
        add_check("allowed_spaces", "passed", f"Gateway resolved {len(allowed_spaces)} allowed space(s).")
    else:
        add_check("allowed_spaces", "warning", "Gateway does not have a cached allowed-space list yet.")

    space_status = str(snapshot.get("space_status") or "").lower()
    if space_status == "active_allowed":
        add_check(
            "space_binding",
            "passed",
            f"Active space is {snapshot.get('active_space_name') or snapshot.get('active_space_id')}.",
        )
    elif space_status == "no_active_space":
        add_check("space_binding", "failed", "Gateway does not have an active space selected for this asset.")
    elif space_status == "active_not_allowed":
        add_check(
            "space_binding",
            "failed",
            str(snapshot.get("confidence_detail") or "Active space is not allowed for this identity."),
        )
    else:
        add_check("space_binding", "warning", "Gateway could not fully verify the active space.")

    attestation_state = str(snapshot.get("attestation_state") or "").lower()
    approval_state = str(snapshot.get("approval_state") or "").lower()
    if approval_state == "pending":
        add_check(
            "binding_approval",
            "warning",
            str(snapshot.get("confidence_detail") or "Gateway needs approval before trusting this runtime binding."),
        )
    elif approval_state == "rejected" or attestation_state == "blocked":
        add_check(
            "binding_approval",
            "failed",
            str(snapshot.get("confidence_detail") or "Gateway blocked this runtime binding."),
        )
    elif attestation_state == "drifted":
        add_check(
            "binding_attestation",
            "failed",
            str(snapshot.get("confidence_detail") or "Runtime binding drifted from its approved launch spec."),
        )
    elif attestation_state == "verified":
        add_check("binding_attestation", "passed", "Runtime matches the approved local binding.")

    token_file = Path(str(entry.get("token_file") or "")).expanduser()
    if token_file.exists() and token_file.read_text().strip():
        add_check("agent_token", "passed", "Managed agent token file is present.")
    else:
        add_check("agent_token", "failed", f"Managed agent token is missing or empty at {token_file}.")

    if asset_class == "background_worker" or intake_model == "queue_accept":
        probe = agent_dir(name) / ".doctor-queue-check"
        try:
            probe.write_text("ok\n")
            probe.unlink(missing_ok=True)
            add_check("queue_writable", "passed", "Gateway queue is writable.")
        except OSError as exc:
            add_check("queue_writable", "failed", f"Gateway queue is not writable: {exc}")
        if bool(snapshot.get("connected")):
            add_check("worker_attached", "passed", "A queue worker is attached.")
        else:
            add_check("worker_attached", "warning", "Queue writable; no worker currently attached.")
        if "summary_post" in return_paths:
            add_check("summary_path", "passed", "Gateway is configured to post a summary after queued work completes.")
    else:
        exec_command = str(entry.get("exec_command") or "").strip()
        runtime_type = str(entry.get("runtime_type") or "").strip().lower()
        if intake_model == "live_listener":
            if snapshot.get("activation") == "attach_only":
                if str(snapshot.get("reachability") or "") == "attach_required":
                    add_check("session_attach", "warning", "Reconnect the attached session before sending.")
                elif bool(snapshot.get("connected")):
                    add_check("session_attach", "passed", "Attached session is connected to Gateway.")
                else:
                    add_check(
                        "session_attach", "failed", "Gateway does not currently have an attached session to supervise."
                    )
            elif runtime_type != "echo":
                if exec_command:
                    add_check("runtime_launch", "passed", "Gateway has a launch command for this runtime.")
                else:
                    add_check("runtime_launch", "failed", "Gateway does not have a launch command for this runtime.")
        elif intake_model == "launch_on_send":
            if runtime_type == "echo" or exec_command:
                add_check("launch_ready", "passed", "Gateway can launch this runtime when work arrives.")
            else:
                add_check(
                    "launch_ready", "failed", "Gateway does not have a launch command for this on-demand runtime."
                )
        elif intake_model == "scheduled_run":
            add_check(
                "schedule_ready",
                "warning",
                "Scheduled asset support is taxonomy-defined but not fully implemented in Gateway yet.",
            )
        elif intake_model == "event_triggered":
            add_check(
                "event_source",
                "warning",
                "Alert-driven asset support is taxonomy-defined but not fully implemented in Gateway yet.",
            )
        elif asset_class == "service_proxy":
            if exec_command:
                add_check("runtime_launch", "passed", "Gateway has a launch command for this runtime.")
            else:
                add_check("runtime_launch", "failed", "Gateway does not have a launch command for this runtime.")

    template_id = str(entry.get("template_id") or "").strip().lower()
    if template_id == "hermes":
        hermes_status = hermes_setup_status(entry)
        if hermes_status.get("ready", True):
            add_check("hermes_repo", "passed", str(hermes_status.get("summary") or "Hermes checkout found."))
        else:
            add_check("hermes_repo", "failed", str(hermes_status.get("summary") or "Hermes checkout not found."))
    elif template_id == "ollama":
        ollama_model = str(entry.get("ollama_model") or "").strip()
        ollama_status = ollama_setup_status(preferred_model=ollama_model or None)
        if bool(ollama_status.get("server_reachable")):
            add_check("ollama_server", "passed", str(ollama_status.get("summary") or "Ollama server is reachable."))
        else:
            add_check("ollama_server", "failed", str(ollama_status.get("summary") or "Ollama server is not reachable."))
        if ollama_model:
            if bool(ollama_status.get("preferred_model_available")):
                add_check("ollama_model", "passed", f"Gateway will launch Ollama with model {ollama_model}.")
            else:
                add_check("ollama_model", "failed", f"Configured Ollama model is not installed: {ollama_model}.")
        else:
            recommended_model = str(ollama_status.get("recommended_model") or "").strip()
            if recommended_model:
                add_check(
                    "ollama_model", "passed", f"Gateway will use the recommended local model {recommended_model}."
                )
            else:
                add_check("ollama_model", "warning", "No Ollama model is selected yet.")
        add_check("launch_path", "passed", "Gateway can launch the Ollama bridge on send.")

    if str(snapshot.get("mode") or "") == "LIVE":
        if str(snapshot.get("presence") or "") == "IDLE":
            add_check("live_path", "passed", "Live listener is connected.")
        elif str(snapshot.get("reachability") or "") == "attach_required":
            add_check("live_path", "warning", "Reconnect the attached session before sending.")
        elif str(snapshot.get("presence") or "") in {"STALE", "OFFLINE"}:
            add_check("live_path", "failed", str(snapshot.get("confidence_detail") or _reachability_copy(snapshot)))
    elif str(snapshot.get("mode") or "") == "ON-DEMAND" and not has_check("launch_ready"):
        add_check("launch_ready", "passed", "Gateway can launch this runtime on send.")

    if send_test:
        try:
            sent = _send_gateway_test_to_managed_agent(name)
            message_id = None
            if isinstance(sent.get("message"), dict):
                message_id = sent["message"].get("id")
            add_check("test_send", "passed", f"Gateway test message sent{f' ({message_id})' if message_id else ''}.")
        except Exception as exc:
            add_check("test_send", "failed", f"Gateway test send failed: {exc}")

    status = _doctor_result_status(checks)
    completed_at = datetime.now(timezone.utc).isoformat()
    result = {
        "status": status,
        "completed_at": completed_at,
        "checks": checks,
        "summary": _doctor_summary(checks, status),
    }
    annotated = _store_doctor_result(name, result)
    return {
        "name": name,
        "status": status,
        "completed_at": completed_at,
        "summary": result["summary"],
        "checks": checks,
        "agent": annotated,
    }


def _parse_iso8601(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(value: object) -> int | None:
    parsed = _parse_iso8601(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))


def _format_age(seconds: object) -> str:
    if seconds is None:
        return "-"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def _format_timestamp(value: object) -> str:
    return _format_age(_age_seconds(value))


def _state_text(state: object) -> Text:
    label = str(state or "unknown").lower()
    style = _STATE_STYLES.get(label, "white")
    return Text(f"● {label}", style=style)


def _presence_text(presence: object) -> Text:
    label = str(presence or "OFFLINE").upper()
    style = _PRESENCE_STYLES.get(label, "white")
    return Text(label, style=style)


def _confidence_text(confidence: object) -> Text:
    label = str(confidence or "MEDIUM").upper()
    style = _CONFIDENCE_STYLES.get(label, "white")
    return Text(label, style=style)


def _mode_text(mode: object) -> Text:
    label = str(mode or "ON-DEMAND").upper()
    style = {
        "LIVE": "green",
        "ON-DEMAND": "cyan",
        "INBOX": "blue",
    }.get(label, "white")
    return Text(label, style=style)


def _reply_text(reply: object) -> Text:
    label = str(reply or "REPLY").upper()
    style = {
        "REPLY": "green",
        "SUMMARY": "yellow",
        "SILENT": "dim",
    }.get(label, "white")
    return Text(label, style=style)


def _reachability_copy(agent: dict) -> str:
    reachability = str(agent.get("reachability") or "unavailable")
    mode = str(agent.get("mode") or "")
    if reachability == "live_now":
        return "Live listener ready to claim work."
    if reachability == "queue_available":
        return "Gateway can safely queue work now."
    if reachability == "launch_available":
        return "Gateway can launch this runtime on send."
    if reachability == "attach_required":
        return "Reconnect the attached session before sending."
    if mode == "INBOX":
        return "Queue path is unavailable."
    return "Gateway does not currently have a working path."


def _agent_template_label(agent: dict) -> str:
    return str(agent.get("template_label") or agent.get("runtime_type") or "-")


def _agent_type_label(agent: dict) -> str:
    return str(agent.get("asset_type_label") or "Connected Asset")


def _agent_output_label(agent: dict) -> str:
    return str(agent.get("output_label") or agent.get("reply") or "Reply")


def _metric_panel(label: str, value: object, *, tone: str = "cyan", subtitle: str | None = None) -> Panel:
    body = Text()
    body.append(str(value), style=f"bold {tone}")
    body.append(f"\n{label}", style="dim")
    if subtitle:
        body.append(f"\n{subtitle}", style="dim")
    return Panel(body, border_style=tone, padding=(1, 2))


def _sorted_agents(agents: list[dict]) -> list[dict]:
    return sorted(
        agents,
        key=lambda agent: (
            _PRESENCE_ORDER.get(str(agent.get("presence") or "").upper(), 99),
            str(agent.get("name") or "").lower(),
        ),
    )


def _render_gateway_overview(payload: dict) -> Panel:
    gateway = payload.get("gateway") or {}
    ui = payload.get("ui") or {}
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column(ratio=2)
    grid.add_column(style="bold")
    grid.add_column(ratio=2)
    grid.add_row(
        "Gateway",
        str(gateway.get("gateway_id") or "-")[:8],
        "Daemon",
        "running" if payload["daemon"]["running"] else "stopped",
    )
    grid.add_row("User", str(payload.get("user") or "-"), "Base URL", str(payload.get("base_url") or "-"))
    space_label = str(payload.get("space_name") or payload.get("space_id") or "-")
    grid.add_row("Space", space_label, "PID", str(payload["daemon"].get("pid") or "-"))
    grid.add_row("UI", str(ui.get("url") or "-"), "UI PID", str(ui.get("pid") or "-"))
    grid.add_row(
        "Session",
        "connected" if payload.get("connected") else "disconnected",
        "Last Reconcile",
        _format_timestamp(gateway.get("last_reconcile_at")),
    )
    return Panel(grid, title="Gateway Overview", border_style="cyan")


def _render_agent_table(agents: list[dict]) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column("Agent", style="bold")
    table.add_column("Type")
    table.add_column("Mode")
    table.add_column("Presence")
    table.add_column("Output")
    table.add_column("Confidence")
    table.add_column("Acting As")
    table.add_column("Current Space")
    table.add_column("Queue", justify="right")
    table.add_column("Seen", justify="right")
    table.add_column("Activity", overflow="fold")
    if not agents:
        table.add_row(
            "No managed agents",
            "-",
            Text("ON-DEMAND", style="dim"),
            Text("OFFLINE", style="dim"),
            Text("Reply", style="dim"),
            Text("MEDIUM", style="dim"),
            "-",
            "-",
            "0",
            "-",
            "-",
        )
        return table
    for agent in _sorted_agents(agents):
        activity = str(
            agent.get("current_activity")
            or agent.get("confidence_detail")
            or agent.get("current_tool")
            or agent.get("last_reply_preview")
            or "-"
        )
        table.add_row(
            f"@{agent.get('name')}",
            _agent_type_label(agent),
            _mode_text(agent.get("mode")),
            _presence_text(agent.get("presence")),
            Text(
                _agent_output_label(agent),
                style="green" if str(agent.get("output_label") or "").lower() == "reply" else "yellow",
            ),
            _confidence_text(agent.get("confidence")),
            str(agent.get("acting_agent_name") or agent.get("name") or "-"),
            str(agent.get("active_space_name") or agent.get("active_space_id") or agent.get("space_id") or "-"),
            str(agent.get("backlog_depth") or 0),
            _format_age(agent.get("last_seen_age_seconds")),
            activity,
        )
    return table


def _render_activity_table(activity: list[dict]) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column("When", justify="right", no_wrap=True)
    table.add_column("Event", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    if not activity:
        table.add_row("-", "idle", "-", "No activity yet")
        return table
    for item in activity:
        detail = (
            item.get("activity_message")
            or item.get("reply_preview")
            or item.get("tool_name")
            or item.get("error")
            or item.get("message_id")
            or "-"
        )
        agent_name = item.get("agent_name")
        table.add_row(
            _format_timestamp(item.get("ts")),
            str(item.get("event") or "-"),
            f"@{agent_name}" if agent_name else "-",
            str(detail),
        )
    return table


def _render_alert_table(alerts: list[dict]) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column("Level", no_wrap=True)
    table.add_column("Alert", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    if not alerts:
        table.add_row("info", "No active alerts", "-", "Gateway looks healthy.")
        return table
    for item in alerts:
        severity = str(item.get("severity") or "info").lower()
        style = {"error": "red", "warning": "yellow", "info": "cyan"}.get(severity, "white")
        agent_name = str(item.get("agent_name") or "")
        table.add_row(
            Text(severity, style=style),
            str(item.get("title") or "-"),
            f"@{agent_name}" if agent_name else "-",
            str(item.get("detail") or "-"),
        )
    return table


def _render_gateway_dashboard(payload: dict) -> Group:
    agents = payload.get("agents", [])
    summary = payload.get("summary", {})
    queue_depth = sum(int(agent.get("backlog_depth") or 0) for agent in agents)
    metrics = Columns(
        [
            _metric_panel("managed agents", summary.get("managed_agents", 0), tone="cyan"),
            _metric_panel("live", summary.get("live_agents", 0), tone="green"),
            _metric_panel("on-demand", summary.get("on_demand_agents", 0), tone="blue"),
            _metric_panel("inbox", summary.get("inbox_agents", 0), tone="cyan"),
            _metric_panel("pending approvals", summary.get("pending_approvals", 0), tone="yellow"),
            _metric_panel("low confidence", summary.get("low_confidence_agents", 0), tone="yellow"),
            _metric_panel("blocked", summary.get("blocked_agents", 0), tone="red"),
            _metric_panel("queue depth", queue_depth, tone="blue"),
        ],
        expand=True,
        equal=True,
    )
    return Group(
        _render_gateway_overview(payload),
        metrics,
        Panel(_render_alert_table(payload.get("alerts", [])), title="Alerts", border_style="red"),
        Panel(_render_agent_table(agents), title="Managed Agents", border_style="green"),
        Panel(
            _render_activity_table(payload.get("recent_activity", [])), title="Recent Activity", border_style="magenta"
        ),
    )


def _render_gateway_ui_page(*, refresh_ms: int) -> str:
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ax gateway ui</title>
  <style>
    :root {
      --bg: #081018;
      --panel: #0e1a24;
      --panel-2: #111f2b;
      --line: #1d3342;
      --text: #e7f7ff;
      --muted: #93afbf;
      --cyan: #47e7ff;
      --green: #53f977;
      --yellow: #f1d45f;
      --red: #ff6e6e;
      --blue: #5c98ff;
      --magenta: #ff5fe6;
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
      --radius: 20px;
      --radius-sm: 14px;
      --mono: "SFMono-Regular", "Menlo", "Monaco", "Consolas", monospace;
      --sans: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(71, 231, 255, 0.18), transparent 32%),
        radial-gradient(circle at top right, rgba(92, 152, 255, 0.16), transparent 28%),
        linear-gradient(180deg, #071019 0%, #0b131c 100%);
      color: var(--text);
      font-family: var(--sans);
    }

    .shell {
      width: min(1400px, calc(100vw - 32px));
      margin: 20px auto 40px;
      display: grid;
      gap: 16px;
    }

    .panel {
      background: linear-gradient(180deg, rgba(14, 26, 36, 0.96), rgba(10, 21, 29, 0.96));
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 18px 22px 0;
      font-family: var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--cyan);
      font-size: 13px;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .panel-body {
      padding: 18px 22px 22px;
    }

    .hero {
      display: grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 16px;
    }

    .hero-copy h1 {
      margin: 0 0 10px;
      font-size: clamp(28px, 3.3vw, 52px);
      line-height: 0.95;
      letter-spacing: -0.03em;
    }

    .hero-copy p {
      margin: 0;
      max-width: 44rem;
      color: var(--muted);
      line-height: 1.55;
      font-size: 15px;
    }

    .hero-meta {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .meta-chip {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--line);
      background: rgba(6, 17, 24, 0.6);
      font-family: var(--mono);
      font-size: 13px;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 16px;
    }

    .metric {
      padding: 18px;
      border-radius: var(--radius);
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.78);
    }

    .metric strong {
      display: block;
      font-size: 34px;
      margin-bottom: 4px;
      font-family: var(--mono);
    }

    .metric span {
      color: var(--muted);
      font-size: 14px;
    }

    .metric.cyan strong { color: var(--cyan); }
    .metric.green strong { color: var(--green); }
    .metric.yellow strong { color: var(--yellow); }
    .metric.red strong { color: var(--red); }
    .metric.blue strong { color: var(--blue); }

    .metric.red span,
    .metric.yellow span {
      color: var(--text);
    }

    .dashboard {
      display: grid;
      grid-template-columns: minmax(0, 1.3fr) minmax(360px, 0.9fr);
      gap: 16px;
    }

    .alerts-list {
      display: grid;
      gap: 12px;
    }

    .alert-card {
      padding: 14px 16px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.7);
    }

    .alert-card.warning {
      border-color: rgba(241, 212, 95, 0.45);
      background: rgba(241, 212, 95, 0.08);
    }

    .alert-card.error {
      border-color: rgba(255, 110, 110, 0.45);
      background: rgba(255, 110, 110, 0.08);
    }

    .alert-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 6px;
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .alert-body {
      color: var(--muted);
      line-height: 1.5;
      font-size: 14px;
    }

    .control-grid {
      display: grid;
      grid-template-columns: minmax(280px, 0.95fr) minmax(0, 1.05fr);
      gap: 16px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }

    th {
      text-align: left;
      padding: 0 0 10px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      border-bottom: 1px solid var(--line);
    }

    td {
      padding: 12px 0;
      border-bottom: 1px solid rgba(29, 51, 66, 0.45);
      vertical-align: top;
    }

    tbody tr:last-child td {
      border-bottom: none;
    }

    .agent-button {
      width: 100%;
      border: 1px solid transparent;
      background: transparent;
      color: inherit;
      text-align: left;
      padding: 10px 12px;
      border-radius: 12px;
      transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
      cursor: pointer;
    }

    .agent-button:hover,
    .agent-button.is-active {
      background: rgba(71, 231, 255, 0.08);
      border-color: rgba(71, 231, 255, 0.35);
      transform: translateY(-1px);
    }

    .agent-name {
      font-family: var(--mono);
      font-weight: 700;
      margin-bottom: 4px;
    }

    .agent-meta,
    .caption,
    .detail-list dd,
    .event-detail {
      color: var(--muted);
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 11px;
      border-radius: 999px;
      border: 1px solid currentColor;
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .status-live,
    .status-idle,
    .status-reply,
    .status-high { color: var(--green); }
    .status-on-demand,
    .status-queued,
    .status-medium { color: var(--cyan); }
    .status-inbox { color: var(--blue); }
    .status-summary,
    .status-blocked,
    .status-stale,
    .status-low { color: var(--yellow); }
    .status-error,
    .status-blocked { color: var(--red); }
    .status-offline,
    .status-silent { color: var(--muted); }

    .detail-card {
      display: grid;
      gap: 16px;
    }

    .action-row,
    .form-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    .control-group {
      display: grid;
      gap: 8px;
    }

    label {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    input,
    select,
    textarea,
    button {
      width: 100%;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.9);
      color: var(--text);
      font: inherit;
      padding: 12px 14px;
    }

    textarea {
      min-height: 96px;
      resize: vertical;
    }

    button {
      width: auto;
      cursor: pointer;
      font-family: var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }

    button:hover {
      transform: translateY(-1px);
      border-color: rgba(71, 231, 255, 0.35);
      background: rgba(71, 231, 255, 0.08);
    }

    button.danger:hover {
      border-color: rgba(255, 110, 110, 0.35);
      background: rgba(255, 110, 110, 0.08);
    }

    button.ghost {
      background: transparent;
      border-color: rgba(71, 231, 255, 0.22);
      color: var(--muted);
    }

    .flash {
      min-height: 24px;
      color: var(--muted);
      font-size: 13px;
    }

    .flash.error {
      color: var(--red);
    }

    .flash.success {
      color: var(--green);
    }

    .flash.warning {
      color: var(--yellow);
    }

    .runtime-info {
      display: grid;
      gap: 12px;
      padding: 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.58);
    }

    .runtime-info h3 {
      margin: 0;
      font-size: 16px;
      font-family: var(--mono);
    }

    .runtime-info p {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
      font-size: 14px;
    }

    .runtime-info summary {
      cursor: pointer;
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text);
      list-style: none;
    }

    .runtime-info summary::-webkit-details-marker {
      display: none;
    }

    .signal-grid {
      display: grid;
      gap: 10px;
    }

    .signal-grid div {
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(6, 17, 24, 0.55);
    }

    .signal-grid strong {
      display: block;
      margin-bottom: 6px;
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--cyan);
    }

    .detail-list {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 20px;
      margin: 0;
    }

    .detail-list div {
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.58);
    }

    .detail-list dt {
      margin: 0 0 6px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .detail-list dd {
      margin: 0;
      line-height: 1.45;
      word-break: break-word;
    }

    .event-list {
      display: grid;
      gap: 10px;
    }

    .event-item {
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.58);
    }

    .event-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 6px;
      font-family: var(--mono);
      font-size: 12px;
      color: var(--text);
    }

    .event-detail {
      font-size: 14px;
      line-height: 1.45;
    }

    .copyable-block {
      position: relative;
    }

    .copyable-block pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: inherit;
      color: inherit;
    }

    .empty {
      padding: 18px;
      border-radius: 14px;
      border: 1px dashed var(--line);
      color: var(--muted);
      text-align: center;
    }

    .footer-note {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
    }

    .footer-note code {
      font-family: var(--mono);
      color: var(--text);
    }

    .badge {
      display: inline-block;
      padding: 6px 9px;
      border-radius: 999px;
      background: rgba(71, 231, 255, 0.08);
      color: var(--cyan);
      border: 1px solid rgba(71, 231, 255, 0.22);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    @media (max-width: 1100px) {
      .hero,
      .dashboard {
        grid-template-columns: 1fr;
      }

      .metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 720px) {
      .shell {
        width: min(100vw - 16px, 100%);
        margin: 8px auto 24px;
      }

      .metrics,
      .detail-list {
        grid-template-columns: 1fr;
      }

      .panel-header,
      .panel-body {
        padding-left: 16px;
        padding-right: 16px;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="panel">
      <div class="panel-body hero">
        <div class="hero-copy">
          <div class="badge">Gateway Control Plane · Agent Operated</div>
          <h1>One local Gateway. Every agent in one place.</h1>
          <p>
            This dashboard is served locally by <code>ax gateway ui</code> and reads the
            same Gateway state model as the terminal watch view. The browser is a human
            view over the same local control plane that setup agents use through the CLI
            and local API instead of maintaining separate logic.
          </p>
        </div>
      <div id="overview" class="hero-meta"></div>
      </div>
    </section>

    <section id="metrics" class="metrics"></section>

    <section class="panel">
      <div class="panel-header">
        <span>Alerts</span>
        <span id="alert-summary" class="caption">loading…</span>
      </div>
      <div id="alerts-feed" class="panel-body">
        <div class="empty">Waiting for Gateway alerts…</div>
      </div>
    </section>

    <section class="control-grid">
      <section class="panel">
        <div class="panel-header">
          <span>Gateway Agent Setup</span>
          <span id="setup-mode-chip" class="caption">agent skill · create</span>
        </div>
        <div class="panel-body">
          <form id="add-agent-form" class="detail-card">
            <p class="caption">
              This form mirrors the <code>gateway-agent-setup</code> skill. Agents and humans
              should use the same Gateway-native setup, doctor, and update flow.
            </p>
            <div class="form-grid">
              <div class="control-group">
                <label for="agent-name">Name</label>
                <input id="agent-name" name="name" placeholder="hermes-bot" required />
              </div>
              <div class="control-group">
                <label for="agent-type">Agent Type</label>
                <select id="agent-type" name="template_id">
                </select>
              </div>
            </div>
            <div id="runtime-help" class="runtime-info">
              <h3>Loading agent type…</h3>
            </div>
            <details id="advanced-launch" class="runtime-info" style="display:none;">
              <summary>Advanced launch settings</summary>
              <p>
                Most setups should leave this alone. These fields exist so we can override
                the default launch command while debugging or building new adapters.
              </p>
              <div class="form-grid">
                <div class="control-group" id="exec-command-group">
                  <label for="agent-exec">Command Override</label>
                  <input id="agent-exec" name="exec_command" placeholder="python3 examples/hermes_sentinel/hermes_bridge.py" />
                </div>
                <div class="control-group" id="workdir-group">
                  <label for="agent-workdir">Working Directory Override</label>
                  <input id="agent-workdir" name="workdir" placeholder="/absolute/path/to/workdir" />
                </div>
                <div class="control-group" id="ollama-model-group" style="display:none;">
                  <label for="agent-ollama-model">Ollama Model</label>
                  <input id="agent-ollama-model" name="ollama_model" list="ollama-model-options" placeholder="gemma4:latest" />
                  <datalist id="ollama-model-options"></datalist>
                  <div id="ollama-model-caption" class="caption"></div>
                </div>
              </div>
            </details>
            <div class="action-row">
              <button id="add-agent-submit" type="submit">Add Agent</button>
              <button id="add-agent-cancel" type="button" class="ghost" style="display:none;">Cancel Edit</button>
            </div>
            <div id="add-agent-flash" class="flash"></div>
          </form>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <span>Custom Message</span>
          <span id="quick-send-chip" class="caption">splunk · datadog · cron · manual</span>
        </div>
        <div class="panel-body">
          <form id="send-form" class="detail-card">
            <p class="caption">
              Use <strong>Send Agent Test</strong> for the standard validation path.
              Use this form for custom payloads, alerts, and scheduled-job style messages.
            </p>
            <div class="form-grid">
              <div class="control-group">
                <label for="send-to">To</label>
                <input id="send-to" name="to" placeholder="codex" />
              </div>
              <div class="control-group">
                <label for="send-parent-id">Parent ID</label>
                <input id="send-parent-id" name="parent_id" placeholder="optional thread parent" />
              </div>
            </div>
            <div class="control-group">
              <label for="send-content">Message</label>
              <textarea id="send-content" name="content" placeholder="Send a custom payload through Gateway: Datadog alert, Splunk event, cron reminder, or manual task"></textarea>
            </div>
            <div class="action-row">
              <button type="submit">Send Custom Message</button>
            </div>
            <div id="send-flash" class="flash"></div>
          </form>
        </div>
      </section>
    </section>

    <section class="dashboard">
      <section class="panel">
        <div class="panel-header">
          <span>Managed Agents</span>
          <span id="managed-summary" class="caption">loading…</span>
        </div>
        <div class="panel-body">
          <table>
            <thead>
              <tr>
                <th>Agent</th>
                <th>Type</th>
                <th>Mode</th>
                <th>Presence</th>
                <th>Output</th>
                <th>Confidence</th>
                <th>Queue</th>
                <th>Seen</th>
                <th>Activity</th>
              </tr>
            </thead>
            <tbody id="agent-rows">
              <tr><td colspan="9"><div class="empty">Waiting for Gateway state…</div></td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <span>Agent Drill-In</span>
          <div class="header-actions">
            <button id="refresh-toggle" type="button" class="ghost">Pause Refresh</button>
            <span id="selected-agent-chip" class="caption">select an agent</span>
          </div>
        </div>
        <div id="agent-detail" class="panel-body">
          <div class="empty">Choose a managed agent to inspect live detail.</div>
        </div>
      </section>
    </section>

    <section class="panel">
      <div class="panel-header">
        <span>Recent Activity</span>
        <span class="caption">auto-refresh every __REFRESH_MS__ ms</span>
      </div>
      <div id="activity-feed" class="panel-body">
        <div class="empty">Waiting for activity…</div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-body footer-note">
        <span>Local status API: <code>/api/status</code> and <code>/api/agents/&lt;name&gt;</code></span>
        <span>Setup skill: <code>skills/gateway-agent-setup/SKILL.md</code> · Terminal parity: <code>uv run ax gateway watch</code></span>
      </div>
    </section>
  </main>

  <script>
    const refreshMs = __REFRESH_MS__;
    let selectedAgent = null;
    let agentTemplates = [];
    let autoRefreshPaused = false;
    let setupMode = "create";
    let setupTarget = null;

    async function apiRequest(path, options = {}) {
      const response = await fetch(path, {
        cache: "no-store",
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
      });
      const isJson = (response.headers.get("Content-Type") || "").includes("application/json");
      const payload = isJson ? await response.json() : null;
      if (!response.ok) {
        throw new Error(payload?.error || `request failed (${response.status})`);
      }
      return payload;
    }

    function setFlash(id, message, kind = "") {
      const node = document.getElementById(id);
      node.className = `flash ${kind}`.trim();
      node.textContent = message || "";
    }

    function applySetupMode() {
      const chip = document.getElementById("setup-mode-chip");
      const submitButton = document.getElementById("add-agent-submit");
      const cancelButton = document.getElementById("add-agent-cancel");
      const nameInput = document.getElementById("agent-name");
      const editing = setupMode === "update" && setupTarget;
      chip.textContent = editing ? `agent skill · editing @${setupTarget}` : "agent skill · create";
      submitButton.textContent = editing ? "Update Agent" : "Add Agent";
      cancelButton.style.display = editing ? "inline-flex" : "none";
      nameInput.readOnly = Boolean(editing);
    }

    function resetSetupForm() {
      const form = document.getElementById("add-agent-form");
      setupMode = "create";
      setupTarget = null;
      form.reset();
      document.getElementById("agent-type").value = "echo_test";
      renderTemplateHelp("echo_test");
      applySetupMode();
    }

    async function loadAgentIntoSetupForm(name) {
      const detail = await apiRequest(`/api/agents/${encodeURIComponent(name)}`);
      const agent = detail.agent || {};
      const nameInput = document.getElementById("agent-name");
      const typeInput = document.getElementById("agent-type");
      const execInput = document.getElementById("agent-exec");
      const workdirInput = document.getElementById("agent-workdir");
      const ollamaModelInput = document.getElementById("agent-ollama-model");

      setupMode = "update";
      setupTarget = agent.name || name;
      nameInput.value = agent.name || name;
      if (agent.template_id) {
        typeInput.value = agent.template_id;
        renderTemplateHelp(agent.template_id);
      }
      execInput.value = agent.exec_command || "";
      workdirInput.value = agent.workdir || "";
      ollamaModelInput.value = agent.ollama_model || "";
      applySetupMode();
      setFlash("add-agent-flash", `Editing @${setupTarget}`, "success");
      document.getElementById("add-agent-form").scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function refreshButtonLabel() {
      const button = document.getElementById("refresh-toggle");
      if (!button) return;
      button.textContent = autoRefreshPaused ? "Resume Refresh" : "Pause Refresh";
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function formatAge(seconds) {
      if (seconds === null || seconds === undefined || seconds === "" || Number.isNaN(Number(seconds))) {
        return "-";
      }
      const total = Math.max(0, Number(seconds));
      if (total < 60) return `${Math.floor(total)}s`;
      const minutes = Math.floor(total / 60);
      const secs = Math.floor(total % 60);
      if (minutes < 60) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
      const hours = Math.floor(minutes / 60);
      const mins = minutes % 60;
      if (hours < 24) return `${hours}h ${String(mins).padStart(2, "0")}m`;
      const days = Math.floor(hours / 24);
      const remHours = hours % 24;
      return `${days}d ${String(remHours).padStart(2, "0")}h`;
    }

    function stateClass(state) {
      return `status-${String(state || "stopped").toLowerCase()}`;
    }

    function detailText(item) {
      return item?.activity_message || item?.reply_preview || item?.tool_name || item?.error || item?.message_id || "-";
    }

    function getTemplateDefinition(templateId) {
      return agentTemplates.find((item) => item.id === templateId) || null;
    }

    function renderTemplateOptions() {
      const select = document.getElementById("agent-type");
      if (!agentTemplates.length) {
        select.innerHTML = `<option value="echo_test">Echo (Test)</option>`;
        return;
      }
      select.innerHTML = agentTemplates.map((item) => {
        const suffix = item.availability === "coming_soon" ? " (Soon)" : "";
        const disabled = item.launchable ? "" : " disabled";
        return `<option value="${escapeHtml(item.id)}"${disabled}>${escapeHtml(item.label + suffix)}</option>`;
      }).join("");
    }

    function renderTemplateHelp(templateId) {
      const definition = getTemplateDefinition(templateId);
      const help = document.getElementById("runtime-help");
      const advancedLaunch = document.getElementById("advanced-launch");
      const submitButton = document.getElementById("add-agent-submit");
      const agentNameInput = document.getElementById("agent-name");
      const execGroup = document.getElementById("exec-command-group");
      const workdirGroup = document.getElementById("workdir-group");
      const ollamaModelGroup = document.getElementById("ollama-model-group");
      const execInput = document.getElementById("agent-exec");
      const workdirInput = document.getElementById("agent-workdir");
      const ollamaModelInput = document.getElementById("agent-ollama-model");
      const ollamaModelOptions = document.getElementById("ollama-model-options");
      const ollamaModelCaption = document.getElementById("ollama-model-caption");
      if (!definition) {
        help.innerHTML = `<h3>Unknown agent type</h3><p>No template definition found.</p>`;
        advancedLaunch.style.display = "none";
        submitButton.disabled = true;
        return;
      }

      const defaults = definition.defaults || {};
      const advanced = definition.advanced || {};
      const supportsOverride = Boolean(advanced.supports_command_override);
      const supportsOllamaModel = definition.id === "ollama";
      const availableOllamaModels = Array.isArray(definition.ollama_available_models) ? definition.ollama_available_models : [];
      const recommendedOllamaModel = definition.ollama_recommended_model || defaults.ollama_model || "";
      advancedLaunch.style.display = supportsOverride ? "grid" : "none";
      execGroup.style.display = supportsOverride ? "grid" : "none";
      workdirGroup.style.display = supportsOverride ? "grid" : "none";
      ollamaModelGroup.style.display = supportsOllamaModel ? "grid" : "none";
      submitButton.disabled = !definition.launchable;

      execInput.placeholder = defaults.exec_command || execInput.placeholder;
      workdirInput.placeholder = defaults.workdir || workdirInput.placeholder;
      ollamaModelInput.placeholder = "gemma4:latest";
      ollamaModelOptions.innerHTML = availableOllamaModels
        .map((item) => `<option value="${escapeHtml(item)}"></option>`)
        .join("");

      if (supportsOverride) {
        execInput.value = defaults.exec_command || "";
        workdirInput.value = defaults.workdir || "";
      }
      if (supportsOllamaModel) {
        ollamaModelInput.value = ollamaModelInput.value || recommendedOllamaModel || "";
        ollamaModelCaption.style.display = "block";
        ollamaModelCaption.textContent = definition.ollama_summary
          || (availableOllamaModels.length
            ? `Installed models: ${availableOllamaModels.join(", ")}`
            : "Gateway could not verify local Ollama models yet.");
      }
      if (!supportsOverride) {
        execInput.value = "";
        workdirInput.value = "";
      }
      if (!supportsOllamaModel) {
        ollamaModelInput.value = "";
        ollamaModelCaption.textContent = "";
        ollamaModelCaption.style.display = "none";
        ollamaModelOptions.innerHTML = "";
      }

      agentNameInput.placeholder = definition.suggested_name || agentNameInput.placeholder;

      const whatYouNeed = (definition.what_you_need || []).length
        ? `<div><strong>What you'll need</strong>${definition.what_you_need.map((note) => `<div>${escapeHtml(note)}</div>`).join("")}</div>`
        : `<div><strong>What you'll need</strong><div>Nothing extra. This one is ready to go.</div></div>`;
      const launchMode = definition.launchable ? "ready to add" : "coming soon";
      const recommendedTest = definition.recommended_test_message
        ? `<div><strong>Recommended test</strong><div>${escapeHtml(definition.recommended_test_message)}</div></div>`
        : "";
      const setupSkill = definition.setup_skill
        ? `<div><strong>Setup skill</strong><div>${escapeHtml(definition.setup_skill)} · ${escapeHtml(definition.setup_skill_path || "")}</div></div>`
        : "";

      help.innerHTML = `
        <h3>${escapeHtml(definition.label)}</h3>
        <p>${escapeHtml(definition.description || "")}</p>
        <div class="signal-grid">
          <div><strong>Type</strong>${escapeHtml(definition.asset_type_label || "-")}</div>
          <div><strong>Output</strong>${escapeHtml(definition.output_label || "-")}</div>
          <div><strong>Intake</strong>${escapeHtml(definition.intake_model || "-")}</div>
          <div><strong>Telemetry</strong>${escapeHtml(definition.telemetry_shape || "-")}</div>
          <div><strong>Why pick this</strong>${escapeHtml(definition.operator_summary || "-")}</div>
          <div><strong>Status</strong>${escapeHtml(definition.availability || "-")} · ${escapeHtml(launchMode)}</div>
          <div><strong>Model</strong>${escapeHtml(definition.id === "ollama" ? (definition.ollama_summary || "Use Ollama Model to pick a local model.") : "-")}</div>
          <div><strong>Delivery</strong>${escapeHtml(definition.signals?.delivery || "-")}</div>
          <div><strong>Liveness</strong>${escapeHtml(definition.signals?.liveness || "-")}</div>
          <div><strong>Activity</strong>${escapeHtml(definition.signals?.activity || "-")}</div>
          <div><strong>Tools</strong>${escapeHtml(definition.signals?.tools || "-")}</div>
          ${setupSkill}
          ${recommendedTest}
          ${whatYouNeed}
        </div>
      `;
    }

    async function loadTemplates() {
      const payload = await apiRequest("/api/templates");
      agentTemplates = payload.templates || [];
      renderTemplateOptions();
      renderTemplateHelp(document.getElementById("agent-type").value || "echo_test");
    }

    function renderOverview(payload) {
      const gateway = payload.gateway || {};
      const overview = document.getElementById("overview");
      overview.innerHTML = `
        <div class="meta-chip"><span>Gateway</span><strong>${escapeHtml(String(gateway.gateway_id || "-").slice(0, 8))}</strong></div>
        <div class="meta-chip"><span>Daemon</span><strong>${payload.daemon?.running ? "running" : "stopped"}</strong></div>
        <div class="meta-chip"><span>Base URL</span><strong>${escapeHtml(payload.base_url || "-")}</strong></div>
        <div class="meta-chip"><span>User</span><strong>${escapeHtml(payload.user || "-")}</strong></div>
        <div class="meta-chip"><span>Space</span><strong>${escapeHtml(payload.space_name || payload.space_id || "-")}</strong></div>
      `;
    }

    function renderMetrics(payload) {
      const agents = payload.agents || [];
      const summary = payload.summary || {};
      const queueDepth = agents.reduce((sum, agent) => sum + Number(agent.backlog_depth || 0), 0);
      const metrics = [
        ["managed agents", summary.managed_agents ?? 0, "cyan"],
        ["live", summary.live_agents ?? 0, "green"],
        ["on-demand", summary.on_demand_agents ?? 0, "blue"],
        ["inbox", summary.inbox_agents ?? 0, "cyan"],
        ["pending approvals", summary.pending_approvals ?? 0, "yellow"],
        ["low confidence", summary.low_confidence_agents ?? 0, "yellow"],
        ["blocked", summary.blocked_agents ?? 0, "red"],
        ["queue depth", queueDepth, "blue"],
      ];
      document.getElementById("metrics").innerHTML = metrics.map(([label, value, tone]) => `
        <article class="metric ${tone}">
          <strong>${escapeHtml(value)}</strong>
          <span>${escapeHtml(label)}</span>
        </article>
      `).join("");
    }

    function renderAlerts(payload) {
      const alerts = payload.alerts || [];
      document.getElementById("alert-summary").textContent = alerts.length
        ? `${alerts.length} active alert${alerts.length === 1 ? "" : "s"}`
        : "all clear";
      const feed = document.getElementById("alerts-feed");
      if (!alerts.length) {
        feed.innerHTML = `<div class="empty">No active Gateway alerts.</div>`;
        return;
      }
      feed.innerHTML = `<div class="alerts-list">${
        alerts.map((item) => `
          <div class="alert-card ${escapeHtml(item.severity || "info")}">
            <div class="alert-head">
              <span>${escapeHtml(item.severity || "info")}</span>
              <span>${escapeHtml(item.agent_name ? "@" + item.agent_name : "gateway")}</span>
            </div>
            <div><strong>${escapeHtml(item.title || "-")}</strong></div>
            <div class="alert-body">${escapeHtml(item.detail || "-")}</div>
          </div>
        `).join("")
      }</div>`;
    }

    function renderAgents(payload) {
      const agents = payload.agents || [];
      const tbody = document.getElementById("agent-rows");
      document.getElementById("managed-summary").textContent = `${agents.length} managed agent${agents.length === 1 ? "" : "s"}`;
      if (!agents.length) {
        tbody.innerHTML = `<tr><td colspan="9"><div class="empty">No managed agents yet.</div></td></tr>`;
        return;
      }
      tbody.innerHTML = agents.map((agent) => {
        const activity = agent.current_activity || agent.confidence_detail || agent.current_tool || agent.last_reply_preview || "-";
        const active = selectedAgent && selectedAgent.toLowerCase() === String(agent.name || "").toLowerCase();
        return `
          <tr>
            <td colspan="8">
              <button class="agent-button ${active ? "is-active" : ""}" data-agent-name="${escapeHtml(agent.name || "")}">
                <table>
                  <tbody>
                    <tr>
                      <td style="width:16%">
                        <div class="agent-name">@${escapeHtml(agent.name || "-")}</div>
                        <div class="agent-meta">${escapeHtml(agent.template_label || agent.runtime_type || "-")}</div>
                      </td>
                      <td style="width:12%">${escapeHtml(agent.asset_type_label || "Connected Asset")}</td>
                      <td style="width:8%"><span class="status-pill ${stateClass(agent.mode)}">${escapeHtml(agent.mode || "ON-DEMAND")}</span></td>
                      <td style="width:8%"><span class="status-pill ${stateClass(agent.presence)}">${escapeHtml(agent.presence || "OFFLINE")}</span></td>
                      <td style="width:8%">${escapeHtml(agent.output_label || agent.reply || "Reply")}</td>
                      <td style="width:10%"><span class="status-pill ${stateClass(agent.confidence)}">${escapeHtml(agent.confidence || "MEDIUM")}</span></td>
                      <td style="width:10%">${escapeHtml(agent.acting_agent_name || agent.name || "-")}</td>
                      <td style="width:12%">${escapeHtml(agent.active_space_name || agent.active_space_id || agent.space_id || "-")}</td>
                      <td style="width:6%">${escapeHtml(agent.backlog_depth || 0)}</td>
                      <td style="width:8%">${escapeHtml(formatAge(agent.last_seen_age_seconds))}</td>
                      <td style="width:22%">${escapeHtml(activity)}</td>
                    </tr>
                  </tbody>
                </table>
              </button>
            </td>
          </tr>
        `;
      }).join("");
    }

    function renderActivity(payload) {
      const activity = payload.recent_activity || [];
      const feed = document.getElementById("activity-feed");
      if (!activity.length) {
        feed.innerHTML = `<div class="empty">No recent Gateway activity.</div>`;
        return;
      }
      feed.innerHTML = `<div class="event-list">${
        activity.map((item) => `
          <div class="event-item">
            <div class="event-head">
              <span>${escapeHtml(item.event || "-")}</span>
              <span>${escapeHtml(formatAge(item.ts ? Math.max(0, ((Date.now() - Date.parse(item.ts)) / 1000)) : null))}</span>
            </div>
            <div class="event-detail">@${escapeHtml(item.agent_name || "-")} · ${escapeHtml(detailText(item))}</div>
          </div>
        `).join("")
      }</div>`;
    }

    function renderAgentDetail(detail) {
      const panel = document.getElementById("agent-detail");
      const chip = document.getElementById("selected-agent-chip");
      const sendChip = document.getElementById("quick-send-chip");
      if (!detail || !detail.agent) {
        chip.textContent = "select an agent";
        sendChip.textContent = "select an agent";
        panel.innerHTML = `<div class="empty">Choose a managed agent to inspect live detail.</div>`;
        return;
      }
      const agent = detail.agent;
      chip.textContent = `@${agent.name}`;
      sendChip.textContent = `custom send as @${agent.name}`;
      const events = detail.recent_activity || [];
      const lastReply = escapeHtml(agent.last_reply_preview || "-");
      const lastReplyCopy = encodeURIComponent(String(agent.last_reply_preview || "-"));
      panel.innerHTML = `
        <div class="detail-card">
          <div>
            <div class="agent-name">@${escapeHtml(agent.name || "-")}</div>
            <div class="caption">${escapeHtml(agent.asset_type_label || "Connected Asset")} · ${escapeHtml(agent.template_label || agent.runtime_type || "-")} · ${escapeHtml(agent.transport || "-")}</div>
          </div>
          <div class="action-row">
            <button type="button" class="ghost" data-agent-action="edit" data-agent-name="${escapeHtml(agent.name || "")}">Edit Setup</button>
            <button type="button" data-agent-action="test" data-agent-name="${escapeHtml(agent.name || "")}">Send Agent Test</button>
            <button type="button" data-agent-action="doctor" data-agent-name="${escapeHtml(agent.name || "")}">Doctor</button>
            <button type="button" data-agent-action="start" data-agent-name="${escapeHtml(agent.name || "")}">Start</button>
            <button type="button" data-agent-action="stop" data-agent-name="${escapeHtml(agent.name || "")}">Stop</button>
            <button type="button" class="danger" data-agent-action="remove" data-agent-name="${escapeHtml(agent.name || "")}">Remove</button>
          </div>
          <div id="detail-flash" class="flash"></div>
          <dl class="detail-list">
            <div><dt>Type</dt><dd>${escapeHtml(agent.asset_type_label || "-")}</dd></div>
            <div><dt>Template</dt><dd>${escapeHtml(agent.template_label || agent.runtime_type || "-")}</dd></div>
            <div><dt>Mode</dt><dd>${escapeHtml(agent.mode || "-")}</dd></div>
            <div><dt>Presence</dt><dd>${escapeHtml(agent.presence || "-")}</dd></div>
            <div><dt>Output</dt><dd>${escapeHtml(agent.output_label || agent.reply || "-")}</dd></div>
            <div><dt>Confidence</dt><dd>${escapeHtml(agent.confidence || "-")}</dd></div>
            <div><dt>Asset Class</dt><dd>${escapeHtml(agent.asset_class || "-")}</dd></div>
            <div><dt>Intake</dt><dd>${escapeHtml(agent.intake_model || "-")}</dd></div>
            <div><dt>Trigger</dt><dd>${escapeHtml((agent.trigger_sources || [])[0] || "-")}</dd></div>
            <div><dt>Return</dt><dd>${escapeHtml((agent.return_paths || [])[0] || "-")}</dd></div>
            <div><dt>Telemetry</dt><dd>${escapeHtml(agent.telemetry_shape || "-")}</dd></div>
            <div><dt>Runtime Model</dt><dd>${escapeHtml(agent.ollama_model || "-")}</dd></div>
            <div><dt>Attestation</dt><dd>${escapeHtml(agent.attestation_state || "-")}</dd></div>
            <div><dt>Approval</dt><dd>${escapeHtml(agent.approval_state || "-")}</dd></div>
            <div><dt>Acting As</dt><dd>${escapeHtml(agent.acting_agent_name || "-")}</dd></div>
            <div><dt>Identity Status</dt><dd>${escapeHtml(agent.identity_status || "-")}</dd></div>
            <div><dt>Environment</dt><dd>${escapeHtml(agent.environment_label || agent.base_url || "-")}</dd></div>
            <div><dt>Environment Status</dt><dd>${escapeHtml(agent.environment_status || "-")}</dd></div>
            <div><dt>Current Space</dt><dd>${escapeHtml(agent.active_space_name || agent.active_space_id || "-")}</dd></div>
            <div><dt>Space Status</dt><dd>${escapeHtml(agent.space_status || "-")}</dd></div>
            <div><dt>Default Space</dt><dd>${escapeHtml(agent.default_space_name || agent.default_space_id || "-")}</dd></div>
            <div><dt>Allowed Spaces</dt><dd>${escapeHtml(agent.allowed_space_count || 0)}</dd></div>
            <div><dt>Install</dt><dd>${escapeHtml(agent.install_id || "-")}</dd></div>
            <div><dt>Runtime Instance</dt><dd>${escapeHtml(agent.runtime_instance_id || "-")}</dd></div>
            <div><dt>Reachability</dt><dd>${escapeHtml(agent.reachability || "-")}</dd></div>
            <div><dt>Reason</dt><dd>${escapeHtml(agent.confidence_reason || "-")}</dd></div>
            <div><dt>Confidence Detail</dt><dd>${escapeHtml(agent.confidence_detail || "-")}</dd></div>
            <div><dt>Queue</dt><dd>${escapeHtml(agent.backlog_depth || 0)}</dd></div>
            <div><dt>Seen</dt><dd>${escapeHtml(formatAge(agent.last_seen_age_seconds))}</dd></div>
            <div><dt>Phase</dt><dd>${escapeHtml(agent.current_status || "-")}</dd></div>
            <div><dt>Activity</dt><dd>${escapeHtml(agent.current_activity || "-")}</dd></div>
            <div><dt>Processed</dt><dd>${escapeHtml(agent.processed_count || 0)}</dd></div>
            <div class="copyable-block">
              <dt>Last Reply</dt>
              <dd><pre>${lastReply}</pre></dd>
              <button type="button" class="ghost" data-copy-text="${lastReplyCopy}">Copy</button>
            </div>
            <div><dt>Last Error</dt><dd>${escapeHtml(agent.last_error || "-")}</dd></div>
            <div><dt>Doctor</dt><dd>${escapeHtml(agent.last_successful_doctor_at || "-")}</dd></div>
            <div><dt>Doctor Result</dt><dd>${escapeHtml(agent.last_doctor_result?.status || "-")}</dd></div>
            <div><dt>Effective</dt><dd>${escapeHtml(agent.effective_state || "-")}</dd></div>
            <div><dt>Workdir</dt><dd>${escapeHtml(agent.workdir || "-")}</dd></div>
            <div><dt>Exec</dt><dd>${escapeHtml(agent.exec_command || "-")}</dd></div>
          </dl>
          <div>
            <div class="panel-header" style="padding:0 0 12px;"><span>Recent Agent Activity</span></div>
            ${
              events.length
                ? `<div class="event-list">${
                    events.map((item) => `
                      <div class="event-item">
                        <div class="event-head">
                          <span>${escapeHtml(item.event || "-")}</span>
                          <span>${escapeHtml(formatAge(item.ts ? Math.max(0, ((Date.now() - Date.parse(item.ts)) / 1000)) : null))}</span>
                        </div>
                        <div class="event-detail">${escapeHtml(detailText(item))}</div>
                      </div>
                    `).join("")
                  }</div>`
                : `<div class="empty">No recent agent activity yet.</div>`
            }
          </div>
        </div>
      `;
    }

    async function loadStatus() {
      const payload = await apiRequest("/api/status");
      renderOverview(payload);
      renderMetrics(payload);
      renderAlerts(payload);
      renderAgents(payload);
      renderActivity(payload);
      if (!selectedAgent && payload.agents?.length) {
        selectedAgent = payload.agents[0].name;
      }
      if (selectedAgent) {
        await loadAgentDetail(selectedAgent);
      } else {
        renderAgentDetail(null);
      }
    }

    async function loadAgentDetail(name) {
      try {
        const payload = await apiRequest(`/api/agents/${encodeURIComponent(name)}`);
        renderAgentDetail(payload);
      } catch {
        renderAgentDetail(null);
      }
    }

    async function tick(force = false) {
      if (!force && autoRefreshPaused) {
        return;
      }
      const selection = window.getSelection ? String(window.getSelection() || "") : "";
      if (!force && selection.trim()) {
        return;
      }
      const active = document.activeElement;
      if (!force && active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)) {
        return;
      }
      try {
        await loadStatus();
      } catch (error) {
        document.getElementById("activity-feed").innerHTML = `<div class="empty">Gateway UI lost contact with the local status API: ${escapeHtml(error.message || error)}</div>`;
      }
    }

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-agent-name]");
      if (!button) return;
      if (button.hasAttribute("data-agent-action")) return;
      selectedAgent = button.getAttribute("data-agent-name");
      tick();
    });

    document.addEventListener("click", async (event) => {
      const copyButton = event.target.closest("[data-copy-text]");
      if (copyButton) {
        const text = decodeURIComponent(copyButton.getAttribute("data-copy-text") || "");
        try {
          await navigator.clipboard.writeText(text);
          setFlash("detail-flash", "Copied last reply.", "success");
        } catch {
          setFlash("detail-flash", "Could not copy to clipboard.", "warning");
        }
        return;
      }
      const button = event.target.closest("[data-agent-action]");
      if (!button) return;
      const action = button.getAttribute("data-agent-action");
      const agentName = button.getAttribute("data-agent-name");
      try {
        if (action === "edit") {
          await loadAgentIntoSetupForm(agentName);
        } else if (action === "remove") {
          await apiRequest(`/api/agents/${encodeURIComponent(agentName)}`, { method: "DELETE" });
          selectedAgent = null;
        } else if (action === "doctor") {
          const result = await apiRequest(`/api/agents/${encodeURIComponent(agentName)}/doctor`, { method: "POST", body: "{}" });
          selectedAgent = agentName;
          setFlash("detail-flash", `Doctor ${result.status} for @${agentName}`, result.status === "failed" ? "error" : (result.status === "warning" ? "warning" : "success"));
        } else if (action === "test") {
          const result = await apiRequest(`/api/agents/${encodeURIComponent(agentName)}/test`, { method: "POST", body: "{}" });
          selectedAgent = agentName;
          setFlash("detail-flash", `Test sent to @${result.target_agent}`, "success");
        } else {
          await apiRequest(`/api/agents/${encodeURIComponent(agentName)}/${action}`, { method: "POST", body: "{}" });
          selectedAgent = agentName;
          setFlash("detail-flash", `${action} requested for @${agentName}`, "success");
        }
        await tick(true);
      } catch (error) {
        setFlash("detail-flash", error.message || String(error), "error");
      }
    });

    document.getElementById("refresh-toggle").addEventListener("click", () => {
      autoRefreshPaused = !autoRefreshPaused;
      refreshButtonLabel();
      if (!autoRefreshPaused) {
        tick(true);
      }
    });

    document.getElementById("agent-type").addEventListener("change", (event) => {
      renderTemplateHelp(event.target.value);
    });

    document.getElementById("add-agent-cancel").addEventListener("click", () => {
      resetSetupForm();
      setFlash("add-agent-flash", "Setup form reset.", "warning");
    });

    document.getElementById("add-agent-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = new FormData(form);
      const payload = {
        name: String(data.get("name") || "").trim(),
        template_id: String(data.get("template_id") || "echo_test"),
        exec_command: String(data.get("exec_command") || "").trim(),
        workdir: String(data.get("workdir") || "").trim(),
        ollama_model: String(data.get("ollama_model") || "").trim(),
        start: true,
      };
      try {
        const updateMode = setupMode === "update" && setupTarget;
        const result = await apiRequest(
          updateMode ? `/api/agents/${encodeURIComponent(setupTarget)}` : "/api/agents",
          {
            method: updateMode ? "PUT" : "POST",
            body: JSON.stringify(payload),
          },
        );
        setFlash("add-agent-flash", `${updateMode ? "Updated" : "Added"} @${result.name}`, "success");
        selectedAgent = result.name;
        resetSetupForm();
        await tick();
      } catch (error) {
        setFlash("add-agent-flash", error.message || String(error), "error");
      }
    });

    document.getElementById("send-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!selectedAgent) {
        setFlash("send-flash", "Select a managed agent first.", "error");
        return;
      }
      const form = event.currentTarget;
      const data = new FormData(form);
      const payload = {
        to: String(data.get("to") || "").trim(),
        parent_id: String(data.get("parent_id") || "").trim(),
        content: String(data.get("content") || "").trim(),
      };
      try {
        const result = await apiRequest(`/api/agents/${encodeURIComponent(selectedAgent)}/send`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        setFlash("send-flash", `Sent as @${result.agent}`, "success");
        form.content.value = "";
        await tick(true);
      } catch (error) {
        setFlash("send-flash", error.message || String(error), "error");
      }
    });

    async function boot() {
      try {
        await loadTemplates();
      } catch (error) {
        setFlash("add-agent-flash", error.message || String(error), "error");
      }
      applySetupMode();
      refreshButtonLabel();
      await tick(true);
      window.setInterval(tick, refreshMs);
    }

    boot();
  </script>
</body>
</html>
"""
    return template.replace("__REFRESH_MS__", str(refresh_ms))


class _GatewayUiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _write_json_response(handler: BaseHTTPRequestHandler, payload: dict, *, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _write_html_response(handler: BaseHTTPRequestHandler, payload: str) -> None:
    body = payload.encode("utf-8")
    handler.send_response(HTTPStatus.OK.value)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_request(handler: BaseHTTPRequestHandler) -> dict:
    content_length = int(handler.headers.get("Content-Length", "0") or 0)
    if content_length <= 0:
        return {}
    raw = handler.rfile.read(content_length)
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object.")
    return payload


def _build_gateway_ui_handler(*, activity_limit: int, refresh_ms: int):
    class GatewayUiHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                _write_html_response(self, _render_gateway_ui_page(refresh_ms=refresh_ms))
                return
            if parsed.path == "/healthz":
                _write_json_response(self, {"ok": True})
                return
            if parsed.path == "/api/status":
                _write_json_response(self, _status_payload(activity_limit=activity_limit))
                return
            if parsed.path == "/api/runtime-types":
                _write_json_response(self, _runtime_types_payload())
                return
            if parsed.path == "/api/templates":
                _write_json_response(self, _agent_templates_payload())
                return
            if parsed.path.startswith("/api/agents/"):
                name = unquote(parsed.path.removeprefix("/api/agents/")).strip()
                payload = _agent_detail_payload(name, activity_limit=activity_limit)
                if payload is None:
                    _write_json_response(
                        self,
                        {"error": f"Managed agent not found: {name}"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                _write_json_response(self, payload)
                return
            _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                body = _read_json_request(self)
                if parsed.path == "/api/agents":
                    payload = _register_managed_agent(
                        name=str(body.get("name") or "").strip(),
                        template_id=str(body.get("template_id") or "").strip() or None,
                        runtime_type=str(body.get("runtime_type") or "").strip() or None,
                        exec_cmd=str(body.get("exec_command") or "").strip() or None,
                        workdir=str(body.get("workdir") or "").strip() or None,
                        ollama_model=str(body.get("ollama_model") or "").strip() or None,
                        space_id=str(body.get("space_id") or "").strip() or None,
                        audience=str(body.get("audience") or "both"),
                        description=str(body.get("description") or "").strip() or None,
                        model=str(body.get("model") or "").strip() or None,
                        start=bool(body.get("start", True)),
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/start") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/start")).strip()
                    payload = _set_managed_agent_desired_state(name, "running")
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/stop") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/stop")).strip()
                    payload = _set_managed_agent_desired_state(name, "stopped")
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/send") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/send")).strip()
                    payload = _send_from_managed_agent(
                        name=name,
                        content=str(body.get("content") or ""),
                        to=str(body.get("to") or "").strip() or None,
                        parent_id=str(body.get("parent_id") or "").strip() or None,
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/test") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/test")).strip()
                    payload = _send_gateway_test_to_managed_agent(
                        name,
                        content=str(body.get("content") or "").strip() or None,
                        author=str(body.get("author") or "agent").strip() or "agent",
                        sender_agent=str(body.get("sender_agent") or "").strip() or None,
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/doctor") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/doctor")).strip()
                    payload = _run_gateway_doctor(
                        name,
                        send_test=bool(body.get("send_test", False)),
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except LookupError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except typer.Exit as exc:
                status = HTTPStatus.BAD_REQUEST if int(exc.exit_code or 1) == 1 else HTTPStatus.OK
                _write_json_response(self, {"error": "request failed"}, status=status)
            except Exception as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_PUT(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                body = _read_json_request(self)
                if parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/")).strip()
                    payload = _update_managed_agent(
                        name=name,
                        template_id=str(body.get("template_id") or "").strip() or None,
                        runtime_type=str(body.get("runtime_type") or "").strip() or None,
                        exec_cmd=str(body.get("exec_command") or "") if "exec_command" in body else _UNSET,
                        workdir=str(body.get("workdir") or "") if "workdir" in body else _UNSET,
                        ollama_model=str(body.get("ollama_model") or "") if "ollama_model" in body else _UNSET,
                        description=str(body.get("description") or "").strip() or None,
                        model=str(body.get("model") or "").strip() or None,
                        desired_state=str(body.get("desired_state") or "").strip() or None,
                    )
                    _write_json_response(self, payload)
                    return
                _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except LookupError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except typer.Exit as exc:
                status = HTTPStatus.BAD_REQUEST if int(exc.exit_code or 1) == 1 else HTTPStatus.OK
                _write_json_response(self, {"error": "request failed"}, status=status)
            except Exception as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/agents/"):
                name = unquote(parsed.path.removeprefix("/api/agents/")).strip()
                try:
                    payload = _remove_managed_agent(name)
                    _write_json_response(self, payload)
                except LookupError as exc:
                    _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    return GatewayUiHandler


def _render_agent_detail(entry: dict, *, activity: list[dict]) -> Group:
    overview = Table.grid(expand=True, padding=(0, 2))
    overview.add_column(style="bold")
    overview.add_column(ratio=2)
    overview.add_column(style="bold")
    overview.add_column(ratio=2)
    overview.add_row("Agent", f"@{entry.get('name')}", "Type", _agent_type_label(entry))
    overview.add_row("Template", _agent_template_label(entry), "Output", _agent_output_label(entry))
    overview.add_row("Mode", str(entry.get("mode") or "-"), "Presence", str(entry.get("presence") or "-"))
    overview.add_row("Reply", str(entry.get("reply") or "-"), "Confidence", str(entry.get("confidence") or "-"))
    overview.add_row(
        "Asset Class", str(entry.get("asset_class") or "-"), "Intake", str(entry.get("intake_model") or "-")
    )
    overview.add_row(
        "Trigger",
        str((entry.get("trigger_sources") or [None])[0] or "-"),
        "Return",
        str((entry.get("return_paths") or [None])[0] or "-"),
    )
    overview.add_row(
        "Telemetry", str(entry.get("telemetry_shape") or "-"), "Worker", str(entry.get("worker_model") or "-")
    )
    overview.add_row(
        "Attestation", str(entry.get("attestation_state") or "-"), "Approval", str(entry.get("approval_state") or "-")
    )
    overview.add_row(
        "Acting As", str(entry.get("acting_agent_name") or "-"), "Identity", str(entry.get("identity_status") or "-")
    )
    overview.add_row(
        "Environment",
        str(entry.get("environment_label") or entry.get("base_url") or "-"),
        "Env Status",
        str(entry.get("environment_status") or "-"),
    )
    overview.add_row(
        "Current Space",
        str(entry.get("active_space_name") or entry.get("active_space_id") or "-"),
        "Space Status",
        str(entry.get("space_status") or "-"),
    )
    overview.add_row(
        "Default Space",
        str(entry.get("default_space_name") or entry.get("default_space_id") or "-"),
        "Allowed Spaces",
        str(entry.get("allowed_space_count") or 0),
    )
    overview.add_row(
        "Install", str(entry.get("install_id") or "-"), "Runtime Instance", str(entry.get("runtime_instance_id") or "-")
    )
    overview.add_row("Reachability", _reachability_copy(entry), "Reason", str(entry.get("confidence_reason") or "-"))
    overview.add_row(
        "Desired", str(entry.get("desired_state") or "-"), "Effective", str(entry.get("effective_state") or "-")
    )
    overview.add_row(
        "Connected", "yes" if entry.get("connected") else "no", "Queue", str(entry.get("backlog_depth") or 0)
    )
    overview.add_row(
        "Seen",
        _format_age(entry.get("last_seen_age_seconds")),
        "Reconnect",
        _format_age(entry.get("reconnect_backoff_seconds")),
    )
    overview.add_row(
        "Processed", str(entry.get("processed_count") or 0), "Dropped", str(entry.get("dropped_count") or 0)
    )
    overview.add_row(
        "Last Work",
        _format_timestamp(entry.get("last_work_received_at")),
        "Completed",
        _format_timestamp(entry.get("last_work_completed_at")),
    )
    overview.add_row(
        "Phase", str(entry.get("current_status") or "-"), "Activity", str(entry.get("current_activity") or "-")
    )
    overview.add_row("Tool", str(entry.get("current_tool") or "-"), "Adapter", str(entry.get("runtime_type") or "-"))
    overview.add_row(
        "Cred Source", str(entry.get("credential_source") or "-"), "Space", str(entry.get("space_id") or "-")
    )
    overview.add_row(
        "Agent ID", str(entry.get("agent_id") or "-"), "Last Reply", str(entry.get("last_reply_preview") or "-")
    )
    overview.add_row(
        "Last Error",
        str(entry.get("last_error") or "-"),
        "Confidence Detail",
        str(entry.get("confidence_detail") or "-"),
    )
    overview.add_row(
        "Doctor",
        str(entry.get("last_successful_doctor_at") or "-"),
        "Doctor Status",
        str(
            (entry.get("last_doctor_result") or {}).get("status")
            if isinstance(entry.get("last_doctor_result"), dict)
            else "-"
        ),
    )

    paths = Table.grid(expand=True, padding=(0, 2))
    paths.add_column(style="bold")
    paths.add_column(ratio=3)
    paths.add_row("Token File", str(entry.get("token_file") or "-"))
    paths.add_row("Workdir", str(entry.get("workdir") or "-"))
    paths.add_row("Exec", str(entry.get("exec_command") or "-"))
    paths.add_row("Added", _format_timestamp(entry.get("added_at")))

    return Group(
        Panel(overview, title=f"Managed Agent · @{entry.get('name')}", border_style="cyan"),
        Panel(paths, title="Runtime Details", border_style="blue"),
        Panel(_render_activity_table(activity), title="Recent Agent Activity", border_style="magenta"),
    )


@app.command("login")
def login(
    token: str = typer.Option(
        None, "--token", "-t", help="User PAT (prompted or reused from axctl login when omitted)"
    ),
    base_url: str = typer.Option(
        None, "--url", "-u", help="API base URL (defaults to existing axctl login or paxai.app)"
    ),
    space_id: str = typer.Option(None, "--space-id", "-s", help="Optional default space for managed agents"),
    as_json: bool = JSON_OPTION,
):
    """Store the Gateway bootstrap session.

    The Gateway keeps the user PAT centrally and uses it to mint agent PATs for
    managed runtimes. Managed runtimes themselves never receive the PAT or JWT.
    """
    resolved_token = _resolve_gateway_login_token(token)
    if not resolved_token.startswith("axp_u_"):
        err_console.print("[red]Gateway bootstrap requires a user PAT (axp_u_).[/red]")
        raise typer.Exit(1)
    resolved_base_url = base_url or resolve_user_base_url() or auth_cmd.DEFAULT_LOGIN_BASE_URL

    err_console.print(f"[cyan]Verifying Gateway login against {resolved_base_url}...[/cyan]")
    from ..token_cache import TokenExchanger

    try:
        exchanger = TokenExchanger(resolved_base_url, resolved_token)
        exchanger.get_token(
            "user_access",
            scope="messages tasks context agents spaces search",
            force_refresh=True,
        )
        client = AxClient(base_url=resolved_base_url, token=resolved_token)
        me = client.whoami()
    except Exception as exc:
        err_console.print(f"[red]Gateway login failed:[/red] {exc}")
        raise typer.Exit(1)

    selected_space = space_id
    selected_space_name = None
    if not selected_space:
        try:
            spaces = client.list_spaces()
            space_list = spaces.get("spaces", spaces) if isinstance(spaces, dict) else spaces
            selected = auth_cmd._select_login_space([s for s in space_list if isinstance(s, dict)])
            if selected:
                selected_space = auth_cmd._candidate_space_id(selected)
                selected_space_name = str(selected.get("name") or selected_space)
        except Exception:
            selected_space = None
    elif selected_space:
        try:
            spaces = client.list_spaces()
            space_list = spaces.get("spaces", spaces) if isinstance(spaces, dict) else spaces
            selected_space_name = next(
                (
                    str(item.get("name") or selected_space)
                    for item in space_list
                    if isinstance(item, dict) and auth_cmd._candidate_space_id(item) == selected_space
                ),
                None,
            )
        except Exception:
            selected_space_name = None

    payload = {
        "token": resolved_token,
        "base_url": resolved_base_url,
        "principal_type": "user",
        "space_id": selected_space,
        "space_name": selected_space_name,
        "username": me.get("username"),
        "email": me.get("email"),
        "saved_at": None,
    }
    path = save_gateway_session(payload)
    registry = load_gateway_registry()
    registry.setdefault("gateway", {})
    registry["gateway"]["session_connected"] = True
    save_gateway_registry(registry)
    record_gateway_activity(
        "gateway_login", username=me.get("username"), base_url=resolved_base_url, space_id=selected_space
    )

    result = {
        "session_path": str(path),
        "base_url": resolved_base_url,
        "space_id": selected_space,
        "space_name": selected_space_name,
        "username": me.get("username"),
        "email": me.get("email"),
    }
    if as_json:
        print_json(result)
    else:
        err_console.print(f"[green]Gateway login saved:[/green] {path}")
        for key, value in result.items():
            err_console.print(f"  {key} = {value}")


@app.command("status")
def status(as_json: bool = JSON_OPTION):
    """Show Gateway status, daemon state, and managed runtimes."""
    payload = _status_payload()
    if as_json:
        print_json(payload)
        return

    err_console.print("[bold]ax gateway status[/bold]")
    err_console.print(f"  gateway_dir = {payload['gateway_dir']}")
    err_console.print(f"  connected   = {payload['connected']}")
    err_console.print(f"  daemon      = {'running' if payload['daemon']['running'] else 'stopped'}")
    if payload["daemon"]["pid"]:
        err_console.print(f"  pid         = {payload['daemon']['pid']}")
    err_console.print(f"  ui          = {'running' if payload['ui']['running'] else 'stopped'}")
    if payload["ui"]["pid"]:
        err_console.print(f"  ui_pid      = {payload['ui']['pid']}")
    err_console.print(f"  ui_url      = {payload['ui']['url']}")
    err_console.print(f"  base_url    = {payload['base_url']}")
    err_console.print(f"  space_id    = {payload['space_id']}")
    if payload.get("space_name"):
        err_console.print(f"  space_name  = {payload['space_name']}")
    err_console.print(f"  user        = {payload['user']}")
    err_console.print(f"  agents      = {payload['summary']['managed_agents']}")
    err_console.print(f"  live        = {payload['summary']['live_agents']}")
    err_console.print(f"  on_demand   = {payload['summary']['on_demand_agents']}")
    err_console.print(f"  inbox       = {payload['summary']['inbox_agents']}")
    err_console.print(f"  alerts      = {payload['summary'].get('alert_count', 0)}")
    err_console.print(f"  approvals   = {payload['summary'].get('pending_approvals', 0)} pending")
    if payload.get("alerts"):
        print_table(
            ["Level", "Alert", "Agent", "Detail"],
            payload["alerts"],
            keys=["severity", "title", "agent_name", "detail"],
        )
    if payload["agents"]:
        print_table(
            [
                "Agent",
                "Type",
                "Mode",
                "Presence",
                "Output",
                "Confidence",
                "Acting As",
                "Current Space",
                "Seen",
                "Backlog",
                "Reason",
            ],
            [
                {**agent, "type": _agent_type_label(agent), "output": _agent_output_label(agent)}
                for agent in payload["agents"]
            ],
            keys=[
                "name",
                "type",
                "mode",
                "presence",
                "output",
                "confidence",
                "acting_agent_name",
                "active_space_name",
                "last_seen_age_seconds",
                "backlog_depth",
                "confidence_reason",
            ],
        )
    if payload["recent_activity"]:
        print_table(
            ["Time", "Event", "Agent", "Message", "Preview"],
            payload["recent_activity"],
            keys=["ts", "event", "agent_name", "message_id", "reply_preview"],
        )


@app.command("runtime-types")
def runtime_types(as_json: bool = JSON_OPTION):
    """List advanced/internal Gateway runtime backends."""
    payload = _runtime_types_payload()
    if as_json:
        print_json(payload)
        return
    rows = []
    for item in payload["runtime_types"]:
        rows.append(
            {
                "id": item["id"],
                "label": item["label"],
                "kind": item.get("kind"),
                "activity": item.get("signals", {}).get("activity"),
                "tools": item.get("signals", {}).get("tools"),
            }
        )
    print_table(
        ["Type", "Label", "Kind", "Activity Signal", "Tool Signal"],
        rows,
        keys=["id", "label", "kind", "activity", "tools"],
    )


@app.command("templates")
def templates(as_json: bool = JSON_OPTION):
    """List Gateway agent templates and what signals they provide."""
    payload = _agent_templates_payload()
    if as_json:
        print_json(payload)
        return
    rows = []
    for item in payload["templates"]:
        rows.append(
            {
                "id": item["id"],
                "label": item["label"],
                "type": item.get("asset_type_label"),
                "output": item.get("output_label"),
                "availability": item.get("availability"),
                "summary": item.get("operator_summary"),
                "activity": item.get("signals", {}).get("activity"),
            }
        )
    print_table(
        ["Template", "Label", "Type", "Output", "Status", "Why Pick It", "Activity Signal"],
        rows,
        keys=["id", "label", "type", "output", "availability", "summary", "activity"],
    )


def _gateway_cli_argv(*args: str) -> list[str]:
    current_argv0 = str(sys.argv[0] or "").strip()
    if current_argv0:
        current_path = Path(current_argv0).expanduser()
        if current_path.exists() and current_path.name in {"ax", "axctl"}:
            return [str(current_path.resolve()), *args]
    python_bin = Path(sys.executable).resolve().parent
    for candidate in (python_bin / "ax", python_bin / "axctl"):
        if candidate.exists():
            return [str(candidate), *args]
    resolved = shutil.which("ax") or shutil.which("axctl")
    if resolved:
        return [resolved, *args]
    command = "import sys; from ax_cli.main import main; sys.argv = ['ax'] + sys.argv[1:]; main()"
    return [sys.executable, "-c", command, *args]


def _spawn_gateway_background_process(command: list[str], *, log_path: Path) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            cwd=str(Path.cwd()),
            start_new_session=True,
            close_fds=True,
        )
    return process


def _tail_log_lines(path: Path, *, lines: int = 12) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    chunks = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(chunks[-lines:])


def _wait_for_daemon_ready(process: subprocess.Popen[bytes], *, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        if daemon_status().get("running") or active_gateway_pid():
            return True
        time.sleep(0.1)
    return process.poll() is None and bool(daemon_status().get("running") or active_gateway_pid())


def _wait_for_ui_ready(process: subprocess.Popen[bytes], *, host: str, port: int, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.1)
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _terminate_pids(pids: list[int], *, timeout: float = 8.0) -> tuple[list[int], list[int]]:
    requested: list[int] = []
    forced: list[int] = []
    for pid in sorted(set(pids)):
        try:
            os.kill(pid, signal.SIGTERM)
            requested.append(pid)
        except ProcessLookupError:
            continue
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [pid for pid in requested if gateway_core._pid_alive(pid)]
        if not alive:
            return requested, forced
        time.sleep(0.1)
    for pid in requested:
        if not gateway_core._pid_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            forced.append(pid)
        except ProcessLookupError:
            continue
    return requested, forced


@app.command("ui")
def ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind the local Gateway UI"),
    port: int = typer.Option(8765, "--port", help="Port for the local Gateway UI"),
    activity_limit: int = typer.Option(24, "--activity-limit", help="Number of recent events to expose in the UI"),
    refresh: float = typer.Option(2.0, "--refresh", help="Browser auto-refresh interval in seconds"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the local UI in a browser"),
):
    """Serve a local Gateway web UI."""
    refresh_ms = max(250, int(refresh * 1000))
    handler = _build_gateway_ui_handler(activity_limit=activity_limit, refresh_ms=refresh_ms)
    try:
        server = _GatewayUiServer((host, port), handler)
    except OSError as exc:
        err_console.print(f"[red]Failed to start Gateway UI:[/red] {exc}")
        raise typer.Exit(1)

    url = f"http://{host}:{server.server_port}"
    err_console.print("[bold]ax gateway ui[/bold] — local Gateway dashboard")
    err_console.print(f"  url      = {url}")
    err_console.print(f"  refresh  = {refresh_ms}ms")
    err_console.print(f"  source   = {gateway_dir()}")
    err_console.print("  stop     = Ctrl-C")
    write_gateway_ui_state(pid=os.getpid(), host=host, port=server.server_port)
    record_gateway_activity("gateway_ui_started", pid=os.getpid(), host=host, port=server.server_port, url=url)
    if open_browser:
        try:
            webbrowser.open_new_tab(url)
        except Exception:
            err_console.print("[yellow]Could not open a browser automatically.[/yellow]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        err_console.print("[yellow]Gateway UI stopped.[/yellow]")
    finally:
        record_gateway_activity("gateway_ui_stopped", pid=os.getpid(), host=host, port=server.server_port, url=url)
        clear_gateway_ui_state(os.getpid())
        server.server_close()


@app.command("start")
def start(
    poll_interval: float = typer.Option(1.0, "--poll-interval", help="Registry reconcile interval in seconds"),
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind the local Gateway UI"),
    port: int = typer.Option(8765, "--port", help="Port for the local Gateway UI"),
    activity_limit: int = typer.Option(24, "--activity-limit", help="Number of recent events to expose in the UI"),
    refresh: float = typer.Option(2.0, "--refresh", help="Browser auto-refresh interval in seconds"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the local UI in a browser"),
):
    """Start the Gateway daemon and local UI in the background."""
    session = load_gateway_session()
    daemon_pid = active_gateway_pid()
    ui_pid = active_gateway_ui_pid()
    daemon_started = False
    ui_started = False
    daemon_note: str | None = None

    if daemon_pid is None:
        if session:
            daemon_process = _spawn_gateway_background_process(
                _gateway_cli_argv("gateway", "run", "--poll-interval", str(poll_interval)),
                log_path=daemon_log_path(),
            )
            if _wait_for_daemon_ready(daemon_process):
                daemon_pid = active_gateway_pid() or daemon_process.pid
                daemon_started = True
            else:
                detail = _tail_log_lines(daemon_log_path())
                err_console.print(
                    f"[red]Failed to start Gateway daemon.[/red] {detail or 'Check gateway.log for details.'}"
                )
                raise typer.Exit(1)
        else:
            daemon_note = "Gateway is not logged in yet; the UI can still start in disconnected mode."

    if ui_pid is None:
        ui_process = _spawn_gateway_background_process(
            _gateway_cli_argv(
                "gateway",
                "ui",
                "--host",
                host,
                "--port",
                str(port),
                "--activity-limit",
                str(activity_limit),
                "--refresh",
                str(refresh),
                "--no-open",
            ),
            log_path=ui_log_path(),
        )
        if _wait_for_ui_ready(ui_process, host=host, port=port):
            ui_pid = active_gateway_ui_pid() or ui_process.pid
            ui_started = True
        else:
            detail = _tail_log_lines(ui_log_path())
            if daemon_started and daemon_pid:
                _terminate_pids([daemon_pid])
                gateway_core.clear_gateway_pid()
            err_console.print(f"[red]Failed to start Gateway UI.[/red] {detail or 'Check gateway-ui.log for details.'}")
            raise typer.Exit(1)

    ui_meta = ui_status()
    if open_browser and ui_meta.get("running"):
        try:
            webbrowser.open_new_tab(str(ui_meta.get("url") or f"http://{host}:{port}"))
        except Exception:
            err_console.print("[yellow]Could not open a browser automatically.[/yellow]")

    err_console.print("[bold]ax gateway start[/bold]")
    err_console.print(f"  daemon    = {'started' if daemon_started else 'running' if daemon_pid else 'not started'}")
    if daemon_pid:
        err_console.print(f"  daemon_pid= {daemon_pid}")
    err_console.print(f"  ui        = {'started' if ui_started else 'running' if ui_pid else 'not started'}")
    if ui_pid:
        err_console.print(f"  ui_pid    = {ui_pid}")
    err_console.print(f"  url       = {ui_meta.get('url') or f'http://{host}:{port}'}")
    err_console.print(f"  logs      = {daemon_log_path()}")
    err_console.print(f"  ui_logs   = {ui_log_path()}")
    if daemon_note:
        err_console.print(f"[yellow]{daemon_note}[/yellow]")


@app.command("stop")
def stop():
    """Stop the background Gateway daemon and local UI."""
    daemon_pids = active_gateway_pids()
    ui_pids = active_gateway_ui_pids()
    if not daemon_pids and not ui_pids:
        clear_gateway_ui_state()
        gateway_core.clear_gateway_pid()
        err_console.print("[yellow]Gateway daemon and UI are already stopped.[/yellow]")
        return

    ui_requested, ui_forced = _terminate_pids(ui_pids)
    daemon_requested, daemon_forced = _terminate_pids(daemon_pids)
    clear_gateway_ui_state()
    gateway_core.clear_gateway_pid()
    record_gateway_activity(
        "gateway_services_stopped",
        daemon_pids=daemon_requested,
        ui_pids=ui_requested,
        daemon_forced=daemon_forced,
        ui_forced=ui_forced,
    )

    err_console.print("[bold]ax gateway stop[/bold]")
    err_console.print(f"  daemon = {daemon_requested or []}")
    err_console.print(f"  ui     = {ui_requested or []}")
    if daemon_forced or ui_forced:
        err_console.print(f"[yellow]Forced kill:[/yellow] daemon={daemon_forced or []} ui={ui_forced or []}")


@app.command("watch")
def watch(
    interval: float = typer.Option(2.0, "--interval", "-n", help="Dashboard refresh interval in seconds"),
    activity_limit: int = typer.Option(8, "--activity-limit", help="Number of recent events to display"),
    once: bool = typer.Option(False, "--once", help="Render one dashboard frame and exit"),
):
    """Watch the Gateway in a live terminal dashboard."""

    def render_dashboard() -> Group:
        return _render_gateway_dashboard(_status_payload(activity_limit=activity_limit))

    if once:
        console.print(render_dashboard())
        return

    try:
        with Live(render_dashboard(), console=console, screen=True, auto_refresh=False) as live:
            while True:
                live.update(render_dashboard(), refresh=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        err_console.print("[yellow]Gateway watch stopped.[/yellow]")


@app.command("run")
def run(
    poll_interval: float = typer.Option(1.0, "--poll-interval", help="Registry reconcile interval in seconds"),
    once: bool = typer.Option(False, "--once", help="Run one reconcile pass and exit"),
):
    """Run the foreground Gateway supervisor."""
    _load_gateway_session_or_exit()
    err_console.print("[bold]ax gateway[/bold] — local control plane")
    err_console.print(f"  state_dir = {gateway_dir()}")
    err_console.print(f"  interval  = {poll_interval}s")
    err_console.print(f"  mode      = {'single-pass' if once else 'foreground'}")
    daemon = GatewayDaemon(logger=lambda msg: err_console.print(f"[dim]{msg}[/dim]"), poll_interval=poll_interval)
    try:
        daemon.run(once=once)
    except RuntimeError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        daemon.stop()
        err_console.print("[yellow]Gateway stopped.[/yellow]")


@approvals_app.command("list")
def list_approvals(
    status: str | None = typer.Option(None, "--status", help="Optional filter: pending | approved | rejected"),
    as_json: bool = JSON_OPTION,
):
    """List local Gateway approval requests."""
    payload = _approval_rows_payload(status=status)
    if as_json:
        print_json(payload)
        return
    err_console.print("[bold]ax gateway approvals list[/bold]")
    err_console.print(f"  approvals = {payload['count']}")
    err_console.print(f"  pending   = {payload['pending']}")
    if not payload["approvals"]:
        err_console.print("[dim]No Gateway approvals found.[/dim]")
        return
    print_table(
        ["Approval", "Asset", "Kind", "Status", "Risk", "Reason", "Requested"],
        payload["approvals"],
        keys=["approval_id", "asset_id", "approval_kind", "status", "risk", "reason", "requested_at"],
    )


@approvals_app.command("show")
def show_approval(
    approval_id: str = typer.Argument(..., help="Approval request id"),
    as_json: bool = JSON_OPTION,
):
    """Show one local Gateway approval request."""
    try:
        payload = _approval_detail_payload(approval_id)
    except LookupError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json(payload)
        return
    approval = payload["approval"]
    print_table(
        ["Field", "Value"],
        [
            {"field": "approval_id", "value": approval.get("approval_id")},
            {"field": "asset_id", "value": approval.get("asset_id")},
            {"field": "gateway_id", "value": approval.get("gateway_id")},
            {"field": "install_id", "value": approval.get("install_id")},
            {"field": "kind", "value": approval.get("approval_kind")},
            {"field": "status", "value": approval.get("status")},
            {"field": "risk", "value": approval.get("risk")},
            {"field": "action", "value": approval.get("action")},
            {"field": "resource", "value": approval.get("resource")},
            {"field": "reason", "value": approval.get("reason")},
            {"field": "requested_at", "value": approval.get("requested_at")},
            {"field": "decided_at", "value": approval.get("decided_at")},
            {"field": "decision_scope", "value": approval.get("decision_scope")},
        ],
        keys=["field", "value"],
    )
    candidate = approval.get("candidate_binding") if isinstance(approval.get("candidate_binding"), dict) else None
    if candidate:
        print_table(
            ["Candidate Field", "Value"],
            [
                {"field": "path", "value": candidate.get("path")},
                {"field": "binding_type", "value": candidate.get("binding_type")},
                {"field": "launch_spec_hash", "value": candidate.get("launch_spec_hash")},
                {"field": "candidate_signature", "value": candidate.get("candidate_signature")},
            ],
            keys=["field", "value"],
        )


@approvals_app.command("approve")
def approve_approval(
    approval_id: str = typer.Argument(..., help="Approval request id"),
    scope: str = typer.Option("asset", "--scope", help="Recorded approval scope: once | asset | gateway"),
    as_json: bool = JSON_OPTION,
):
    """Approve a local Gateway binding request."""
    try:
        payload = approve_gateway_approval(approval_id, scope=scope)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json(payload)
        return
    approval = payload["approval"]
    err_console.print(f"[green]Approved:[/green] {approval['approval_id']}")
    err_console.print(f"  asset = {approval.get('asset_id')}")
    err_console.print(f"  scope = {approval.get('decision_scope')}")


@approvals_app.command("deny")
def deny_approval(
    approval_id: str = typer.Argument(..., help="Approval request id"),
    as_json: bool = JSON_OPTION,
):
    """Deny a local Gateway binding request."""
    try:
        payload = deny_gateway_approval(approval_id)
    except LookupError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json({"approval": payload})
        return
    err_console.print(f"[yellow]Denied:[/yellow] {payload['approval_id']}")
    err_console.print(f"  asset = {payload.get('asset_id')}")


@agents_app.command("add")
def add_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    template_id: str = typer.Option(
        None, "--template", help="Agent template: echo_test | ollama | hermes | sentinel_cli | claude_code_channel"
    ),
    runtime_type: str = typer.Option(
        None, "--type", help="Advanced/internal runtime backend: echo | exec | hermes_sentinel | sentinel_cli | inbox"
    ),
    exec_cmd: str = typer.Option(None, "--exec", help="Advanced override for exec-based templates"),
    workdir: str = typer.Option(None, "--workdir", help="Advanced working directory override"),
    ollama_model: str = typer.Option(None, "--ollama-model", help="Ollama model override for the Ollama template"),
    space_id: str = typer.Option(None, "--space-id", help="Target space (defaults to gateway session)"),
    audience: str = typer.Option("both", "--audience", help="Minted PAT audience"),
    description: str = typer.Option(None, "--description", help="Create/update description"),
    model: str = typer.Option(None, "--model", help="Create/update model"),
    start: bool = typer.Option(True, "--start/--no-start", help="Desired running state after registration"),
    as_json: bool = JSON_OPTION,
):
    """Register a managed agent and mint a Gateway-owned PAT for it."""
    selected_template = template_id or ("echo_test" if not runtime_type else None)
    try:
        entry = _register_managed_agent(
            name=name,
            template_id=selected_template,
            runtime_type=runtime_type,
            exec_cmd=exec_cmd,
            workdir=workdir,
            ollama_model=ollama_model,
            space_id=space_id,
            audience=audience,
            description=description,
            model=model,
            start=start,
        )
    except (ValueError, LookupError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(entry)
    else:
        err_console.print(f"[green]Managed agent ready:[/green] @{name}")
        if entry.get("template_label"):
            err_console.print(f"  type = {entry['template_label']}")
        if entry.get("asset_type_label"):
            err_console.print(f"  asset = {entry['asset_type_label']}")
        err_console.print(f"  desired_state = {entry['desired_state']}")
        err_console.print(f"  token_file = {entry['token_file']}")


@agents_app.command("update")
def update_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    template_id: str = typer.Option(None, "--template", help="Replace the agent template"),
    runtime_type: str = typer.Option(
        None,
        "--type",
        help="Advanced/internal runtime backend override: echo | exec | hermes_sentinel | sentinel_cli | inbox",
    ),
    exec_cmd: str = typer.Option(None, "--exec", help="Advanced override for exec-based templates"),
    workdir: str = typer.Option(None, "--workdir", help="Advanced working directory override"),
    ollama_model: str = typer.Option(None, "--ollama-model", help="Ollama model override for the Ollama template"),
    description: str = typer.Option(None, "--description", help="Update platform agent description"),
    model: str = typer.Option(None, "--model", help="Update platform agent model"),
    desired_state: str = typer.Option(None, "--desired-state", help="running | stopped"),
    as_json: bool = JSON_OPTION,
):
    """Update a managed agent without redoing Gateway bootstrap."""
    try:
        entry = _update_managed_agent(
            name=name,
            template_id=template_id,
            runtime_type=runtime_type,
            exec_cmd=exec_cmd if exec_cmd is not None else _UNSET,
            workdir=workdir if workdir is not None else _UNSET,
            ollama_model=ollama_model if ollama_model is not None else _UNSET,
            description=description,
            model=model,
            desired_state=desired_state,
        )
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(entry)
        return
    err_console.print(f"[green]Managed agent updated:[/green] @{name}")
    err_console.print(f"  type = {entry.get('template_label') or entry.get('runtime_type')}")
    err_console.print(f"  desired_state = {entry.get('desired_state')}")


@agents_app.command("list")
def list_agents(as_json: bool = JSON_OPTION):
    """List Gateway-managed agents."""
    agents = _status_payload()["agents"]
    if as_json:
        print_json({"agents": agents, "count": len(agents)})
        return
    print_table(
        ["Agent", "Type", "Mode", "Presence", "Output", "Confidence", "Space"],
        [{**agent, "type": _agent_type_label(agent), "output": _agent_output_label(agent)} for agent in agents],
        keys=["name", "type", "mode", "presence", "output", "confidence", "space_id"],
    )


@agents_app.command("show")
def show_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    activity_limit: int = typer.Option(12, "--activity-limit", help="Number of recent agent events to display"),
    as_json: bool = JSON_OPTION,
):
    """Show one managed agent in detail."""
    result = _agent_detail_payload(name, activity_limit=activity_limit)
    if result is None:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    if as_json:
        print_json(result)
        return
    console.print(_render_agent_detail(result["agent"], activity=result["recent_activity"]))


@agents_app.command("test")
def test_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    message: str = typer.Option(None, "--message", help="Override the recommended Gateway test prompt"),
    author: str = typer.Option("agent", "--author", help="Who should author the test message: agent | user"),
    sender_agent: str = typer.Option(None, "--sender-agent", help="Managed sender identity to use when --author agent"),
    as_json: bool = JSON_OPTION,
):
    """Send a Gateway-authored test message to one managed agent."""
    try:
        result = _send_gateway_test_to_managed_agent(name, content=message, author=author, sender_agent=sender_agent)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return

    err_console.print(f"[green]Gateway test sent:[/green] @{result['target_agent']}")
    err_console.print(f"  prompt = {result['recommended_prompt']}")
    message_payload = result.get("message") or {}
    if isinstance(message_payload, dict) and message_payload.get("id"):
        err_console.print(f"  message_id = {message_payload['id']}")


@agents_app.command("doctor")
def doctor_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    send_test: bool = typer.Option(False, "--send-test", help="Also send a Gateway-authored smoke test"),
    as_json: bool = JSON_OPTION,
):
    """Run Gateway Doctor checks for one managed agent."""
    try:
        result = _run_gateway_doctor(name, send_test=send_test)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return

    tone = {"passed": "green", "warning": "yellow", "failed": "red"}.get(result["status"], "cyan")
    err_console.print(f"[{tone}]Gateway Doctor {result['status']}:[/{tone}] @{name}")
    err_console.print(f"  summary = {result['summary']}")
    print_table(["Check", "Status", "Detail"], result["checks"], keys=["name", "status", "detail"])


@agents_app.command("send")
def send_as_agent(
    name: str = typer.Argument(..., help="Managed agent name to send as"),
    content: str = typer.Argument(..., help="Message content"),
    to: str = typer.Option(None, "--to", help="Prepend a mention like @codex automatically"),
    parent_id: str = typer.Option(None, "--parent-id", help="Reply inside an existing thread"),
    as_json: bool = JSON_OPTION,
):
    """Send a message as a Gateway-managed agent."""
    try:
        result = _send_from_managed_agent(name=name, content=content, to=to, parent_id=parent_id)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return
    err_console.print(f"[green]Sent as managed agent:[/green] @{result['agent']}")
    if isinstance(result["message"], dict) and result["message"].get("id"):
        err_console.print(f"  id = {result['message']['id']}")
    err_console.print(f"  content = {result['content']}")


@agents_app.command("start")
def start_agent(name: str = typer.Argument(..., help="Managed agent name")):
    """Set a managed agent's desired state to running."""
    try:
        _set_managed_agent_desired_state(name, "running")
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    err_console.print(f"[green]Desired state set to running:[/green] @{name}")


@agents_app.command("stop")
def stop_agent(name: str = typer.Argument(..., help="Managed agent name")):
    """Set a managed agent's desired state to stopped."""
    try:
        _set_managed_agent_desired_state(name, "stopped")
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    err_console.print(f"[green]Desired state set to stopped:[/green] @{name}")


@agents_app.command("remove")
def remove_agent(name: str = typer.Argument(..., help="Managed agent name")):
    """Remove a managed agent from local Gateway control."""
    try:
        _remove_managed_agent(name)
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    err_console.print(f"[green]Removed managed agent:[/green] @{name}")
