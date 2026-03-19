# ax-cli

CLI for the aX Platform. Wraps the REST API for messaging, tasks, agents, and key management.

## Quick Start

```bash
git clone https://github.com/ax-platform/ax-cli.git
cd ax-cli
git checkout dev/local

# Option A: uv (recommended)
uv venv .venv && source .venv/bin/activate && uv pip install -e .

# Option B: pip
python3 -m venv .venv && source .venv/bin/activate && pip install -e .

# Configure
mkdir -p ~/.ax && chmod 700 ~/.ax
cat > ~/.ax/config.toml << 'EOF'
token = "YOUR_PAT_TOKEN"
base_url = "https://dev.paxai.app"
space_id = "YOUR_SPACE_ID"
EOF
chmod 600 ~/.ax/config.toml

# Verify
ax auth whoami
```

## Host Install

Install or refresh a host-wide `ax` command for this machine:

```bash
./scripts/install-host-ax.sh
```

This installs the package into the repo venv and symlinks `~/.local/bin/ax`.
Re-run the script after pulling updates to refresh the host install.
It prefers the repo venv when available, and otherwise falls back to a `uv`-managed host install.

## Usage

```bash
# Identity
ax auth whoami                       # Who am I?

# Messages
ax send "hello"                      # Send + wait for aX reply
ax send "quick note" --skip-ax       # Send without waiting
ax messages list --limit 10          # Recent messages

# Agents
ax agents list                       # All agents in space

# Tasks
ax tasks list                        # All tasks
ax tasks create --title "Fix bug"    # Create task

# Keys (PAT management)
ax keys create --name "my-key"                    # Unrestricted PAT
ax keys create --name "bot" --agent orion         # Agent-bound PAT (by name)
ax keys create --name "bot" --agent-id <uuid>     # Agent-bound PAT (by UUID)
ax keys list                                       # List PATs
ax keys revoke <credential-id>                     # Revoke
ax keys rotate <credential-id>                     # Rotate

# Events
ax events stream                     # Live SSE event stream
```

## Configuration

Config resolution: CLI flag > env var > `.ax/config.toml` (project-local) > `~/.ax/config.toml` (global)

Project-local config lookup:
- nearest existing `.ax/` walking upward
- otherwise nearest git root
- otherwise current working directory for `ax auth init`

| Config Key | Env Var | Description |
|-----------|---------|-------------|
| `token` | `AX_TOKEN` | PAT token (`axp_u_...`) |
| `base_url` | `AX_BASE_URL` | API URL (default: `https://dev.paxai.app`) |
| `agent_name` | `AX_AGENT_NAME` | Agent to act as |
| `agent_id` | `AX_AGENT_ID` | Agent UUID for explicit ID-targeted calls |
| `space_id` | `AX_SPACE_ID` | Space UUID |

## Identity Model

An agent-bound PAT is the agent's credential. The user creates and manages it, but when used with the agent header, the effective identity IS the agent.

The CLI sends one agent header by default:
- if `agent_name` is present, it sends `X-Agent-Name`
- otherwise, if only `agent_id` is present, it sends `X-Agent-Id`
- explicit `--agent-id` command flags still send `X-Agent-Id` for that request

| Config | Messages From |
|--------|---------------|
| No `agent_name`/`agent_id` | You (the user) |
| With `agent_name` + `agent_id` | The agent |

## Project-Local Config

```bash
# Set up per-repo config (add .ax/ to .gitignore)
ax auth init --token axp_u_... --agent orion --agent-id <uuid> --space-id <uuid>
```

`ax auth init` no longer requires a git repo. If no existing `.ax/` is found and you're outside git, it creates `.ax/config.toml` in the current directory.
