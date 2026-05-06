# Repo-Scoped Claude Code Channel Agents

Provisioning standard for per-repo Claude Code Channel agents on the
local-Gateway control plane. Goal: setup is **boring** — predictable
naming, one identity per workdir, no surprises about which agent a
Claude Code session is acting as.

This is the operating-procedure companion to
`docs/multi-agent-validation-runbook.md`. The validation runbook proves
the surfaces work; this doc tells operators how to provision the agents
they will actually use day-to-day.

## TL;DR

- **One agent per primary runnable tree.** Workdir is the identity scope.
- **Naming convention: `cc_<repo>`** for new Claude Code Channel agents.
  Boring on purpose — `cc_frontend`, `cc_backend`, `cc_mcp`, `cc_gateway`.
- **Transport: Claude Code Channel (live listener, SSE push)** for
  actively-developed workdirs; **pass-through (polling mailbox)** for
  Codex-style or unattended automation identities. Do not bind both
  transports to the same workdir.
- **Setup is one command per repo:** `ax channel setup` from inside the
  workdir. Approval (if prompted) at `http://127.0.0.1:8765`.
- **Stand them up after Gateway validation.** Use throwaway `validate_*`
  agents to prove the runbook; provision the real `cc_*` agents on a
  known-good base.

## Recommended Agent Roster

| Workdir | Agent name | Transport | Rationale |
|---|---|---|---|
| `~/claude_home/ax-frontend-extract` | `cc_frontend` | Claude Code Channel | Active development; live SSE delivery while a session is attached |
| `~/claude_home/ax-backend-extract` | `cc_backend` | Claude Code Channel | Same |
| `~/claude_home/ax-mcp-server-extract` | `cc_mcp` (or keep `widget_smith` as legacy) | Claude Code Channel | Same |
| `~/claude_home/ax-gateway` | `cc_gateway` (or keep `cli_god` as legacy) | Claude Code Channel | Same |
| Codex / automation sentinels | `codex_supervisor`, `pulse-cc`, `qa_sentinel`, etc. | Pass-through (mailbox) | Sessions are batchy / one-off; polling fits |

Existing legacy identities (`widget_smith`, `cli_god`) **stay as-is** to
avoid disruption. New repos and new operator boxes follow the `cc_*`
convention. Migration of the legacy names is opt-in, not forced.

The Codex-style pass-through agents (`codex_supervisor`, `pulse-cc`,
`mac_frontend`, `mac_backend`, `qa_sentinel`) are intentionally a
**different transport** and a **different identity** from the `cc_*`
agents above. They share workspace context but not identity. See
"Avoiding the Identity-Split Bug" below.

## Setup — Exact Commands

For each workdir:

```bash
# 1. Confirm Gateway is up.
ax gateway status            # daemon = running

# 2. From inside the target workdir.
cd /Users/jacob/claude_home/ax-frontend-extract
ax channel setup --agent cc_frontend

# 3. If prompted for approval, open the URL it points at
#    (typically http://127.0.0.1:8765) and approve the binding.
#    The approval signs the workdir fingerprint to this agent name.

# 4. Verify the workdir is correctly bound.
cat .ax/config.toml          # agent_name = "cc_frontend", workdir = current dir
ls .mcp.json                 # ax-channel server entry referencing cc_frontend.env
ls ~/.claude/channels/ax-channel/cc_frontend.env

# 5. Verify Gateway sees the agent registered.
ax gateway agents list | grep cc_frontend
```

Repeat verbatim for `cc_backend`, `cc_mcp`, `cc_gateway`, swapping the
workdir + agent name. The setup writes three artifacts per repo:

- `<workdir>/.ax/config.toml` — workdir-scoped identity record (agent name,
  Gateway URL, mode=local). **No PAT in this file.**
- `<workdir>/.mcp.json` — Claude Code MCP server registration; references
  the env-file path so credentials never appear in version control.
- `~/.claude/channels/ax-channel/<agent>.env` — Gateway-owned env file
  holding the channel auth material. Mode `0600`. Operator-readable only.

## Avoiding the Identity-Split Bug

The class of bug: one workdir collapses into multiple agent identities,
or one identity spreads across multiple workdirs. Symptoms include
operator confusion ("which agent did I just send a message to?"), audit
trails that point at the wrong actor, and Gateway treating a repeated
session as a new agent each time.

Defenses, in order of importance:

1. **One agent per workdir.** Don't run `ax channel setup` twice in the
   same workdir with different agent names. The first run binds the
   workdir's fingerprint; a second attempt with a different name should
   refuse via `Gateway identity mismatch: this local origin is already
   registered as @<existing>`. If you actually need to re-bind, remove
   the existing registry row first (`ax gateway agents remove <name>` or
   `ax gateway agents archive <name>` — archive is preferred for
   reversibility once #147 lands).

2. **Don't copy `.ax/config.toml` between repos.** It carries the
   workdir absolute path; copying it stamps a different workdir with
   the wrong identity. If you fork a repo, run `ax channel setup`
   fresh — don't shortcut by copying.

3. **Workdir is the identity boundary, not the git repo.** `.ax/`
   discovery walks up from CWD and stops at the nearest `.ax/`
   directory; it does not respect `.git` boundaries. Two checkouts of
   the same repo on different paths are two different agent identities,
   by design. Two different repos sharing a parent dir would resolve to
   the same identity if the parent has `.ax/`. So: keep `.ax/` at the
   repo root, never higher.

4. **Don't put runtime PATs in `~/.ax/config.toml`.** The global config
   file is for user-setup defaults only. Mixing user PATs into it
   leaks them into agent runtime credentials and breaks the trust
   boundary between bootstrap user and runtime agent.

5. **Channel and pass-through transports do not coexist on the same
   workdir.** If you want a workdir to participate in both the live
   channel and the polling mailbox, the answer is to upgrade the agent
   to the unified-transport model (post-Gateway-MCP-spec direction). For
   now: pick one transport per workdir.

The existing `_find_local_origin_collision` check in
`ax_cli/commands/gateway.py:359` enforces (1) at registration time. If
you hit `Gateway identity mismatch`, that's the system protecting you;
do not work around it without thinking through the implications.

## Doorbell — Inbound Notification Behavior

The supervisor's framing — "live interrupt/SSE/channel delivery vs
scheduled polling" — is the right product question. The answer is
**both**, but split by session class:

### Claude Code Channel agents (`cc_*`) — live SSE while attached, queued otherwise

While a Claude Code session is attached to the agent (the `axctl
channel` MCP server is running and Gateway shows the agent as
`Live Listener / LIVE`):

- Inbound mentions deliver via SSE through `ax-channel` immediately.
- Operator sees `notifications/claude/channel` events live in the
  attached session.
- The dashboard's `LAST ACTIVITY` updates within seconds.

When no session is attached (Claude Code closed, MCP server not
running):

- Gateway flips the agent's effective state to `idle/offline`.
- Inbound messages are queued — they are **not** dropped.
- On the next session attach, the channel drains the queue and the
  newly-attached session sees a backlog of missed messages.
- A doorbell-on-attach signal is the right UX (a one-line "you missed
  N messages while away; here they are"); this surfaces today as the
  unread count badge on the dashboard but should also reach the
  attached session as an introductory system message. **Tracked as a
  follow-up; not blocking this provisioning standard.**

### Pass-through agents (Codex / automation sentinels) — polled mailbox

Pass-through agents do not maintain an open SSE connection. They are
inbox-shaped:

- Inbound messages queue against the agent's mailbox.
- The Codex (or other) operator polls via `ax gateway local inbox` (or
  the equivalent API) at whatever cadence makes sense for the workflow.
- "Doorbell" for these is the unread count returned by the inbox API
  call — there is no push.

This is the right model for Codex specifically because Codex sessions
are not always-on listeners; they start up, do bounded work, exit. An
SSE listener would be wasted on a session that won't be there to
receive.

### Why not unify

We discussed (with @widget_smith on the Gateway MCP design thread) a
future state where one identity multiplexes both transports: live SSE
for inbound notifications + request/response tool surface, with the
polling mailbox as a fallback when no listener is attached. That is the
right long-term direction and is captured in the stdio-MCP spec ask.
But it is **not what we are shipping today**. Today: pick one
transport per agent, separate identities for separate transports.
Don't pre-build the multiplexed shape until the spec lands and is
validated.

## Provisioning Order

Stand the `cc_*` agents up **after** the Gateway validation runbook
passes on the relevant PRs. Specifically:

1. Run `docs/multi-agent-validation-runbook.md` against the test
   environment using throwaway `validate_echo`, `validate_hermes`,
   and `validate_cc_channel` agents. These prove the Gateway control
   plane works end-to-end.
2. Once the runbook passes and the gating PRs (#148, #147) merge,
   archive the throwaway agents (`ax gateway agents archive validate_*`).
3. Provision `cc_frontend`, `cc_backend`, `cc_mcp`, `cc_gateway` per
   the Setup section above against the validated Gateway.
4. Confirm operator UX: open Claude Code in each workdir, send a
   message from one to another, watch the dashboard. Each workdir
   should resolve to its own `cc_*` agent in `LAST ACTIVITY` lines.

Standing them up before validation conflates "is the agent set up
right?" with "does Gateway work?" — making bug triage harder. The
two-phase order keeps each question answered separately.

## Outstanding Questions / Known Gaps

These are recorded so they don't get lost but are not blocking
provisioning the standard above:

- **Auto-degradation when a Claude Code session detaches.** Today,
  detaching does not change the agent's transport. The agent's
  effective state goes idle, but the registry still classifies it as
  Live Listener. A clean detach handshake that flips the agent to
  pass-through-mode (so messages keep delivering against a polled
  mailbox while the session is gone) would close the offline-message
  gap. Tracked as a follow-up.
- **Doorbell-on-attach system message.** Surface a "missed N messages
  while away" line to a freshly-attached session.
- **Migration tooling for legacy names.** No automated path from
  `widget_smith` → `cc_mcp` or `cli_god` → `cc_gateway`. Manual rename
  via remove + re-bind is the only path today. Fine for the small
  number of legacy identities; would matter more if the population
  grew.
- **`pulse-cc`/`cli_god` style identity split**: cleaner separation
  between Claude Code channel identities (`cc_*`) and Codex
  pass-through identities (`pulse-cc`, etc.) is what the naming
  convention enforces going forward. The existing population is fine
  as-is; the convention prevents new collisions.

## Cross-References

- `docs/multi-agent-validation-runbook.md` — proves Gateway works
  before these agents are provisioned against it.
- `docs/agent-authentication.md` — credential lifecycle and the
  user/agent identity boundary.
- `docs/credential-security.md` — env-file handling, why PATs never
  appear in `.ax/config.toml` or `.mcp.json`.
- `CLAUDE.md` — workspace identity boundary rules.
