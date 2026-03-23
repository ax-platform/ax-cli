"""ax auth — identity and token management."""
import typer
import httpx

from ..config import (
    clear_agent_binding, get_client, save_agent_binding, save_token, resolve_token, resolve_agent_name,
    _global_config_dir, _local_config_dir, _save_config, _load_local_config,
)
from ..output import JSON_OPTION, print_json, print_kv, handle_error, console

app = typer.Typer(name="auth", help="Authentication & identity", no_args_is_help=True)
token_app = typer.Typer(name="token", help="Token management", no_args_is_help=True)
app.add_typer(token_app, name="token")


@app.command()
def whoami(
    as_json: bool = JSON_OPTION,
    as_user: bool = typer.Option(
        False,
        "--as-user",
        help="Ignore the saved agent binding and inspect the underlying user/admin identity",
    ),
):
    """Show current identity.

    Default mode is agent-first: the saved binding is part of the identity.
    Use --as-user only for explicit admin/bootstrap workflows.
    """
    client = get_client(as_user=as_user)
    try:
        data = client.whoami()
    except httpx.HTTPStatusError as e:
        handle_error(e)

    bound = data.get("bound_agent")
    if bound:
        data["resolved_space_id"] = bound.get("default_space_id", "none")
        saved_binding = save_agent_binding(
            agent_id=bound.get("agent_id"),
            agent_name=bound.get("agent_name"),
            space_id=bound.get("default_space_id"),
        )
        if saved_binding:
            data["saved_agent_binding"] = True
    else:
        from ..config import resolve_space_id
        try:
            space_id = resolve_space_id(client, explicit=None)
            data["resolved_space_id"] = space_id
        except SystemExit:
            data["resolved_space_id"] = "unresolved (set AX_SPACE_ID or use --space-id)"

    # Show resolved agent name
    resolved = resolve_agent_name(client=client)
    if resolved:
        data["resolved_agent"] = resolved

    # Show local config path if it exists
    local = _local_config_dir()
    if local and (local / "config.toml").exists():
        data["local_config"] = str(local / "config.toml")

    if as_json:
        print_json(data)
    else:
        print_kv(data)


@app.command()
def bind(
    agent: str = typer.Option(None, "--agent", help="Default agent name for bootstrap/display"),
    agent_id: str = typer.Option(None, "--agent-id", help="Default agent UUID (canonical when known)"),
    space_id: str = typer.Option(None, "--space-id", help="Persist the default space alongside the agent binding"),
    local: bool = typer.Option(False, "--local", help="Save the binding to project-local .ax/config.toml"),
):
    """Persist the default agent identity for agent-first CLI use."""
    if not agent and not agent_id:
        typer.echo("Error: provide --agent, --agent-id, or both.", err=True)
        raise typer.Exit(1)

    save_agent_binding(
        agent_id=agent_id,
        agent_name=agent,
        space_id=space_id,
        local_preferred=local,
    )

    target = "project-local .ax/config.toml" if local else str(_global_config_dir() / "config.toml")
    console.print(f"[green]Saved agent binding to {target}[/green]")
    if agent_id:
        console.print(f"  agent_id = {agent_id}")
    if agent:
        console.print(f"  agent_name = {agent}")
    if space_id:
        console.print(f"  space_id = {space_id}")


@app.command()
def unbind(
    local: bool = typer.Option(False, "--local", help="Clear the project-local binding instead of ~/.ax/config.toml"),
):
    """Clear the saved default agent binding."""
    changed = clear_agent_binding(local=local)
    if changed:
        target = "project-local .ax/config.toml" if local else str(_global_config_dir() / "config.toml")
        console.print(f"[green]Cleared saved agent binding from {target}[/green]")
    else:
        console.print("[yellow]No saved agent binding found.[/yellow]")


@app.command("init")
def init(
    token: str = typer.Option(None, "--token", "-t", help="PAT token"),
    base_url: str = typer.Option("http://localhost:8001", "--url", "-u", help="API base URL"),
    agent_name: str = typer.Option(None, "--agent", "-a", help="Default agent name"),
    agent_id: str = typer.Option(None, "--agent-id", help="Default agent ID (canonical for agent-bound PATs)"),
    space_id: str = typer.Option(None, "--space-id", "-s", help="Default space ID"),
):
    """Set up a project-local .ax/config.toml in the current repo.

    Stores everything locally — token, URL, agent, space. No flags needed after init.
    Add .ax/ to .gitignore — credentials stay out of version control.

    Examples:
        ax auth init --token axp_u_... --agent orion --agent-id 70c1b445-...
        ax auth init --token axp_u_... --url https://dev.paxai.app --agent canvas
    """
    local = _local_config_dir(create=True)
    if not local:
        typer.echo("Error: Cannot determine a project directory for local config.", err=True)
        raise typer.Exit(1)

    cfg = _load_local_config()

    if token:
        cfg["token"] = token
    if base_url:
        cfg["base_url"] = base_url
    if agent_name:
        cfg["agent_name"] = agent_name
    if agent_id:
        cfg["agent_id"] = agent_id
    if space_id:
        cfg["space_id"] = space_id

    if not cfg:
        typer.echo("Error: Provide at least --agent or --space-id.", err=True)
        raise typer.Exit(1)

    _save_config(cfg, local=True)
    config_path = local / "config.toml"
    console.print(f"[green]Saved:[/green] {config_path}")
    for k, v in cfg.items():
        if k == "token":
            v = v[:6] + "..." + v[-4:] if len(v) > 10 else "***"
        console.print(f"  {k} = {v}")

    # Check .gitignore when available
    root = local.parent
    gitignore = root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        if ".ax/" not in content and ".ax" not in content:
            console.print(f"\n[yellow]Reminder:[/yellow] Add .ax/ to {gitignore}")
    elif (root / ".git").exists():
        console.print(f"\n[yellow]Reminder:[/yellow] Add .ax/ to .gitignore")


@token_app.command("set")
def token_set(token: str = typer.Argument(..., help="PAT token (axp_u_...)")):
    """Save token to ~/.ax/config.toml."""
    save_token(token)
    typer.echo(f"Token saved to {_global_config_dir() / 'config.toml'}")


@token_app.command("show")
def token_show():
    """Show saved token (masked)."""
    token = resolve_token()
    if not token:
        typer.echo("No token configured.", err=True)
        raise typer.Exit(1)
    if len(token) > 10:
        masked = token[:6] + "..." + token[-4:]
    else:
        masked = token[:2] + "..." + token[-2:]
    typer.echo(masked)
