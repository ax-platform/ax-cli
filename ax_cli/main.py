"""aX Platform CLI — Typer app with subcommand registration."""
import sys
from typing import Optional

import httpx
import typer

from .commands import auth, keys, agents, messages, tasks, events

app = typer.Typer(name="ax", help="aX Platform CLI", no_args_is_help=True)
app.add_typer(auth.app, name="auth")
app.add_typer(keys.app, name="keys")
app.add_typer(agents.app, name="agents")
app.add_typer(messages.app, name="messages")
app.add_typer(tasks.app, name="tasks")
app.add_typer(events.app, name="events")


@app.command("send")
def send_shortcut(
    content: str = typer.Argument(..., help="Message to send"),
    wait: bool = typer.Option(True, "--wait/--skip-ax", "-w", help="Wait for aX response (default: yes)"),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Max seconds to wait"),
    reply_to: Optional[str] = typer.Option(None, "--reply-to", "-r", help="Reply to message ID (thread)"),
    agent: Optional[str] = typer.Option(None, "--agent", "-a", help="Send as agent (X-Agent-Name)"),
    as_user: bool = typer.Option(
        False,
        "--as-user",
        help="Send as the underlying user/admin identity instead of the bound agent",
    ),
    space_id: Optional[str] = typer.Option(None, "--space-id", "-s", help="Override default space"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Send a message and wait for aX's response by default. Use --skip-ax to send only."""
    messages.send(
        content=content,
        wait=wait,
        timeout=timeout,
        agent_id=None,
        agent_name=agent,
        as_user=as_user,
        channel="main",
        parent=reply_to,
        space_id=space_id,
        as_json=as_json,
    )


def main():
    """Entry point with global error handling."""
    try:
        app()
    except httpx.ConnectError:
        typer.echo("Error: cannot reach aX API. Is the server running?", err=True)
        sys.exit(1)
