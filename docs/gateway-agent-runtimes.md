# Gateway Agent Runtimes

Gateway is the management plane for local agents. It should not force every
agent brain into a new runtime shape.

The proven local setup before Gateway was:

- Long-running sentinel listeners in `/home/ax-agent/agents`, launched by
  scripts such as `start_hermes_sentinel.sh`.
- Hermes-backed coding agents using `claude_agent_v2.py --runtime hermes_sdk`
  with Codex/OpenAI models.
- Claude Code sessions connected through `axctl channel` using agent-bound
  profiles.
- Per-agent workdirs, notes, and local instructions under
  `/home/ax-agent/agents/<name>/`.

Gateway keeps those pieces, but moves operator management into one place:

- Mint and store agent-bound credentials.
- Bind identity to device, workdir, runtime type, and launch spec.
- Start, stop, and observe local runtimes.
- Show liveness, queue state, activity, and tool signals.
- Provide a single CLI/UI for dev, staging, and production operators.

## Current PR88 State

PR88 has enough Gateway plumbing to register agents, mint tokens, show status,
queue passive inbox work, run simple built-in runtimes, and run command bridges
that emit `AX_GATEWAY_EVENT` progress lines. It also has the first
Gateway-supervised Hermes sentinel runtime, which preserves the old long-running
listener behavior instead of launching a new model process per message.

Current useful modes:

- `echo`: prove Gateway delivery and UI status.
- `inbox`: prove queueing and manual acknowledgement paths.
- `exec`: run probes or one-shot bridges that explicitly persist any state they
  need.
- `hermes_sentinel`: Gateway-supervised long-running Hermes listener using the
  old `claude_agent_v2.py --runtime hermes_sdk` behavior.

Target modes still to implement:

- `claude_code_channel`: Gateway-registered attached Claude Code channel with
  health/liveness tracking around `axctl channel`.

Use `hermes_sentinel` for coding sentinel QA. Avoid using a one-shot `exec`
bridge as proof that `dev_sentinel` is fixed. It can prove Gateway dispatch,
but not the session continuity that made the old sentinel setup useful.

## Preferred Runtime Patterns

### Hermes Sentinel

Use Hermes for coding sentinels that need tool use, repo access, session
continuity, and rich activity. On this host, the preferred model family is the
Codex/OpenAI path, for example `codex:gpt-5.5` when available.

The old working launcher shape is:

```bash
/home/ax-agent/agents/start_hermes_sentinel.sh dev_sentinel \
  --runtime hermes_sdk \
  --model codex:gpt-5.5
```

The Gateway-managed shape preserves that runtime behavior:

```bash
ax gateway agents add dev_sentinel \
  --template hermes \
  --workdir /home/ax-agent/agents/dev_sentinel

ax gateway agents start dev_sentinel
ax gateway agents show dev_sentinel
```

Gateway should supervise the long-running listener process. The listener still
owns the Hermes session, runtime plugin, message queue, and tool callbacks. The
Gateway owns the credentials, process lifecycle, binding verification, and
operator status.

Do not treat the one-shot `examples/hermes_sentinel/hermes_bridge.py` demo as
the production sentinel pattern. It is useful for proving that a Gateway command
bridge can call Hermes, but it creates a fresh agent per message and does not
match the old sentinel continuity model.

### Claude Code Channel

Use Claude Code channels for agents backed by a Claude subscription. The channel
is an attached live session, not a headless per-message subprocess.

The old working launcher shape is:

```bash
cd /home/ax-agent/channel
eval "$(axctl profile env <agent-profile>)"
exec axctl channel --agent <agent-name> --space-id <space-id>
```

Claude Code then runs with the channel MCP server loaded:

```bash
claude --strict-mcp-config \
  --mcp-config .mcp.json \
  --dangerously-load-development-channels server:ax-channel
```

The Gateway-managed shape should register and monitor this attached runtime:

```bash
ax gateway agents add orion \
  --template claude_code_channel \
  --workdir /home/ax-agent/channel

ax gateway agents show orion
```

Gateway should know which local Claude Code channel is attached, which
agent-bound token/profile it uses, and whether it is stale or healthy. The
channel remains responsible for delivering messages into Claude Code and for
emitting `working` and `completed` processing signals.

### Command Bridge

Use command bridges for simple adapters, demos, and smoke tests.

```bash
ax gateway agents add echo-bot --type echo
ax gateway agents add probe \
  --type exec \
  --exec "python3 examples/gateway_probe/probe_bridge.py"
```

Command bridges are valuable for probes and simple integrations. They are not
the preferred shape for coding sentinels because a per-message command loses
important in-process state unless the bridge explicitly persists and resumes it.

## Signal Contract

Every inbound message should have a visible delivery signal before the final
reply. This is how operators know work did not disappear into a black hole.

Minimum signals:

- `picked_up` or `working`: the runtime received the message.
- `thinking`: the model/runtime started processing.
- `tool_call`: the runtime is using a tool, with a useful tool name or summary
  when available.
- `completed`: the runtime finished and either replied or explicitly queued the
  work.
- `error`: the runtime failed and the operator should inspect logs.

Hermes sentinels should preserve the old behavior from `claude_agent_v2.py`:
tool callbacks update the activity bubble with real work, such as reading a
file, running a command, searching, or writing a note.

Claude Code channels should at least emit delivery and completion. Richer tool
signals depend on what Claude Code exposes through the channel.

## Migration From CLI-Managed Agents

1. Inventory the old agent directory, launcher, workdir, model, and profile.
2. Register the agent in Gateway without changing its platform identity.
3. Mint or attach an agent-bound credential owned by Gateway.
4. Store the launch spec in Gateway: runtime family, workdir, command/profile,
   and expected environment.
5. Start the same runtime through Gateway supervision.
6. Verify that the first inbound message gets a visible pickup/activity signal.
7. Verify continuity with a two-message memory test in the same thread.
8. Keep the old systemd/CLI launcher disabled once Gateway supervision is
   stable, so only one listener receives each message.

The important rule is one live receive path per agent. If the old CLI listener
and Gateway both listen for the same agent identity, messages can route through
different paths and create the stale/missing-context behavior seen during the
Gateway migration.

## Dev Server Notes

For `dev.paxai.app`, prefer building and testing against development agents
first. A good first continuity test is:

```text
@dev_sentinel remember the word cobalt and reply briefly.
@dev_sentinel what word did I ask you to remember?
```

Expected result:

- The original message shows Gateway pickup/working activity quickly.
- The runtime uses the Hermes session from the first turn on the second turn.
- The reply remembers `cobalt`.
- Tool activity appears when the agent reads files, writes notes, or runs
  commands.

If the second reply has no memory of the first, the agent is still being
cold-started per message or messages are reaching multiple receive paths.
