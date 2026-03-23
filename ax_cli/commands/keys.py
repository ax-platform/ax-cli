"""ax keys — PAT and delegated agent token management.

PATs are user credentials. An agent-bound PAT acts as the agent when used
with the X-Agent-Id header. User owns the token; agent scope limits where
it can be used.
"""
from typing import Optional

import typer
import httpx

from ..config import get_client
from ..output import JSON_OPTION, print_json, print_table, handle_error

app = typer.Typer(name="keys", help="API key management", no_args_is_help=True)


@app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="Key name"),
    agent_id: Optional[list[str]] = typer.Option(None, "--agent-id", help="Restrict to agent UUID (single target becomes the bound agent; repeatable)"),
    agent: Optional[str] = typer.Option(None, "--agent", help="Restrict to agent by name (resolves to UUID)"),
    unbound: bool = typer.Option(False, "--unbound", help="Create an unbound PAT that binds on first X-Agent-Name use"),
    as_user: bool = typer.Option(False, "--as-user", help="Ignore the saved agent binding and create the key as the underlying user/admin identity"),
    as_json: bool = JSON_OPTION,
):
    """Create a new API key (PAT).

    Without --agent-id or --agent: unrestricted user PAT.
    With exactly one agent target: delegated agent token (PAT-backed).
    With multiple --agent-id values: agent-scoped PAT limited to that set.

    Examples:
        ax keys create --name "my-key"
        ax keys create --name "orion-key" --agent orion
        ax keys create --name "multi" --agent-id <uuid1> --agent-id <uuid2>
    """
    client = get_client(as_user=as_user)

    # Resolve --agent name to UUID if provided
    bound_ids = list(agent_id) if agent_id else []
    if unbound and (bound_ids or agent):
        typer.echo("Error: --unbound cannot be combined with --agent or --agent-id.", err=True)
        raise typer.Exit(1)
    if agent:
        try:
            agents_data = client.list_agents()
            agents_list = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
            match = next((a for a in agents_list if a.get("name", "").lower() == agent.lower()), None)
            if not match:
                typer.echo(f"Error: Agent '{agent}' not found in this space.", err=True)
                raise typer.Exit(1)
            bound_ids.append(str(match["id"]))
        except httpx.HTTPStatusError as e:
            handle_error(e)

    agent_scope = "unbound" if unbound else ("agents" if bound_ids else "all")
    bound_agent_id = bound_ids[0] if len(bound_ids) == 1 else None

    try:
        data = client.create_key(
            name,
            allowed_agent_ids=bound_ids or None,
            agent_scope=agent_scope,
            agent_id=bound_agent_id,
        )
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        token = data.get("token") or data.get("key") or data.get("raw_token")
        cred_id = data.get("credential_id", data.get("id", ""))
        typer.echo(f"Key created: {cred_id}")
        if unbound:
            typer.echo("Scope: unbound (will bind on first use with X-Agent-Name)")
        elif bound_agent_id:
            typer.echo(f"Bound agent: {bound_agent_id}")
        elif bound_ids:
            typer.echo(f"Allowed agents: {', '.join(bound_ids)}")
        if token:
            typer.echo(f"Token: {token}")
        typer.echo("Save this token — it won't be shown again.")


@app.command("list")
def list_keys(
    as_user: bool = typer.Option(False, "--as-user", help="Ignore the saved agent binding and list keys as the underlying user/admin identity"),
    as_json: bool = JSON_OPTION,
):
    """List all API keys."""
    client = get_client(as_user=as_user)
    try:
        data = client.list_keys()
    except httpx.HTTPStatusError as e:
        handle_error(e)
    keys = data if isinstance(data, list) else data.get("keys", [])
    if as_json:
        print_json(keys)
    else:
        print_table(
            ["Credential ID", "Name", "Scopes", "Allowed Agent IDs", "Last Used At", "Created At", "Revoked At"],
            keys,
            keys=["credential_id", "name", "scopes", "allowed_agent_ids", "last_used_at", "created_at", "revoked_at"],
        )


@app.command("revoke")
def revoke(
    credential_id: str = typer.Argument(..., help="Credential ID to revoke"),
    as_user: bool = typer.Option(False, "--as-user", help="Ignore the saved agent binding and revoke the key as the underlying user/admin identity"),
):
    """Revoke an API key."""
    client = get_client(as_user=as_user)
    try:
        client.revoke_key(credential_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    typer.echo("Revoked.")


@app.command("rotate")
def rotate(
    credential_id: str = typer.Argument(..., help="Credential ID to rotate"),
    as_user: bool = typer.Option(False, "--as-user", help="Ignore the saved agent binding and rotate the key as the underlying user/admin identity"),
    as_json: bool = JSON_OPTION,
):
    """Rotate an API key — issues new token, revokes old."""
    client = get_client(as_user=as_user)
    try:
        data = client.rotate_key(credential_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    if as_json:
        print_json(data)
    else:
        token = data.get("token") or data.get("key") or data.get("raw_token")
        if token:
            typer.echo(f"New token: {token}")
        typer.echo("Save this token — it won't be shown again.")
