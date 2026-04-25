# CLI-SURFACE-INVENTORY-001: axctl Verb Inventory + Gaps vs MCP

**Status:** Skeleton (pre-staged 2026-04-25 — populate Saturday AM)
**Owner:** @orion (absorbing from cli_sentinel due to silence on `653cae21`)
**Source task:** [`653cae21`](aX) — CLI surface inventory — every axctl verb, args, output, gaps vs MCP
**Sprint:** Gateway Sprint 1 (Trifecta Parity), umbrella [`d21e60ea`](aX)
**Date:** 2026-04-25
**Companion:** [MCP surface inventory `6699321c`](aX) → mcp_sentinel — they merge into the parity gap doc

## Method

For each verb-row: read `axctl <command> [<sub>] --help`, the relevant `ax_cli/commands/<group>.py`, and the underlying REST endpoint hit. Record:

- **Command path** — full `axctl ... <verb>` form
- **Required args** — positional + required options
- **Optional flags** — common flags only; full set in the source
- **Output shape** — default columns + `--json` keys
- **Auth scope** — user PAT / agent-bound PAT / either
- **REST endpoint** — what it actually hits
- **MCP equivalent** — does the MCP server expose the same verb? same params? same output? — **filled by cross-referencing mcp_sentinel's `6699321c` artifact when it lands**

Acceptance criteria for the merged inventory: every CLI row has a paired MCP row (or "MCP gap" note); every MCP row has a paired CLI row (or "CLI gap" note). The gap doc is the diff.

## Skeleton — populate Saturday AM

### Top-level shortcuts

| Command | Required | Optional flags | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|---|
| `axctl bootstrap-agent` | NAME | `--runtime`, `--workdir`, `--scope` | text + `--json` | user PAT | `/api/v1/agents` (POST) + `/api/v1/keys` | TBD |
| `axctl handoff` | TASK | `--to`, `--watch`, `--timeout` | text | user/agent | `/api/v1/tasks` + SSE | TBD |
| `axctl login` | — | `--token`, `--url`, `--env` | confirmation | bootstraps user PAT | `/auth/...` | n/a (CLI-only) |
| `axctl send` | CONTENT | `--skip-ax`, `--space-id`, `--wait`, `--file` | reply or none | agent | `/api/v1/messages` (POST) | `messages(action='send')` |

### `axctl auth`

| Subcommand | Args | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|
| `auth doctor` | — | report | any | various GET | TBD |
| `auth whoami` | — | identity | any | `/api/v1/auth/whoami` | `whoami(action='show')` |
| `auth init` | — | interactive setup | n/a | n/a | n/a (CLI-only) |
| `auth exchange` | — | JWT | PAT | `POST /auth/exchange` | n/a |
| `auth token` | (sub) | various | any | `POST /auth/...` | n/a |

### `axctl keys`

| Subcommand | Args | Output | Auth | REST |
|---|---|---|---|---|
| `keys create` | NAME | key info | user | `POST /api/v1/keys` |
| `keys list` | — | table | user | `GET /api/v1/keys` |
| `keys revoke` | KEY_ID | confirmation | user | `DELETE /api/v1/keys/{id}` |
| `keys rotate` | KEY_ID | new key | user | `POST /api/v1/keys/{id}/rotate` |

### `axctl credentials`

| Subcommand | Args | Output | Auth | REST |
|---|---|---|---|---|
| `credentials issue-agent-pat` | AGENT_ID | PAT | user | `POST /api/v1/credentials/issue/agent-pat` |
| `credentials issue-enrollment` | — | enrollment token | user | `POST /api/v1/credentials/issue/enrollment` |
| `credentials revoke` | CRED_ID | confirmation | user | `DELETE /api/v1/credentials/{id}` |
| `credentials audit` | — | log | user | `GET /api/v1/credentials/audit` |
| `credentials list` | — | table | user | `GET /api/v1/credentials` |

### `axctl agents`

| Subcommand | Args | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|
| `agents list` | — | table or JSON | any | `GET /api/v1/agents` | `agents(action='list')` |
| `agents ping` | NAME | round-trip | any | `POST /api/v1/messages` (probe) | n/a |
| `agents discover` | (filters) | matches | any | `GET /api/v1/agents?...` | TBD |
| `agents create` | NAME | created agent | user | `POST /api/v1/agents` | `agents(action='create')` (HITL) |
| `agents get` | NAME | full record | any | `GET /api/v1/agents/{id}` | `agents(action='get')` |
| `agents update` | NAME | updated record | user | `PATCH /api/v1/agents/{id}` | TBD |
| `agents delete` | NAME | confirmation | user | `DELETE /api/v1/agents/{id}` | TBD |
| `agents status` | NAME | presence record | any | `GET /api/v1/agents/{id}` | TBD — gap closes when AVAIL-CONTRACT lands `agents(action='check')` |
| `agents tools` | NAME | tool list | any | `GET /api/v1/agents/{id}/tools` | TBD |
| `agents avatar` | NAME | avatar URL | any | `GET /api/v1/agents/{id}/avatar` | TBD |

### `axctl apps`

| Subcommand | Args | Output | REST |
|---|---|---|---|
| `apps list` | — | table | `GET /api/v1/apps` |
| `apps signal` | KIND | signal payload | `POST /api/v1/apps/signal` |

### `axctl messages`

| Subcommand | Args | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|
| `messages send` | CONTENT | reply or skip | any | `POST /api/v1/messages` | `messages(action='send')` |
| `messages list` | — | table | any | `GET /api/v1/messages` | `messages(action='list')` |
| `messages read` | (id?) | confirmation | any | `POST /api/v1/messages/read` | TBD |
| `messages get` | ID | full record | any | `GET /api/v1/messages/{id}` | `messages(action='get')` |
| `messages edit` | ID | updated | author | `PATCH /api/v1/messages/{id}` | TBD |
| `messages delete` | ID | confirmation | author | `DELETE /api/v1/messages/{id}` | TBD |
| `messages search` | QUERY | matches | any | `GET /api/v1/messages?q=...` | TBD |

### `axctl alerts`

| Subcommand | Args | Output | REST |
|---|---|---|---|
| `alerts send` | KIND | alert payload | `POST /api/v1/alerts` |
| `alerts reminder` | (subs) | reminder ops | `POST /api/v1/alerts/reminder/*` |
| `alerts ack` | ID | ack | `POST /api/v1/alerts/{id}/ack` |
| `alerts resolve` | ID | resolved | `POST /api/v1/alerts/{id}/resolve` |
| `alerts snooze` | ID | snoozed | `POST /api/v1/alerts/{id}/snooze` |
| `alerts state` | — | current alerts | `GET /api/v1/alerts` |

### `axctl reminders`

| Subcommand | Args | Output | REST |
|---|---|---|---|
| `reminders add` | (params) | reminder | `POST /api/v1/reminders` (or local-only) |
| `reminders list` | — | table | local or `GET /api/v1/reminders` |
| `reminders disable` | ID | disabled | local or `PATCH ...` |
| `reminders run` | — | run loop | local-only — TBD if surfaced via MCP |

### `axctl tasks`

| Subcommand | Args | Output | Auth | REST | MCP equivalent |
|---|---|---|---|---|---|
| `tasks create` | TITLE | created | user | `POST /api/v1/tasks` | `tasks(action='create')` |
| `tasks list` | — | table | any | `GET /api/v1/tasks` | `tasks(action='list')` |
| `tasks get` | ID | full record | any | `GET /api/v1/tasks/{id}` | `tasks(action='get')` |
| `tasks update` | ID | updated | author/admin | `PATCH /api/v1/tasks/{id}` | TBD |

### `axctl events`

| Subcommand | Args | Output | REST |
|---|---|---|---|
| `events stream` | (filters) | streaming text | `GET /api/sse/messages` |

### `axctl listen`, `axctl watch`, `axctl upload`

| Command | Args | Output | Notes |
|---|---|---|---|
| `axctl listen` | — | streaming mention handler | `/api/sse/messages` + reply via `POST /api/v1/messages` |
| `axctl watch` | (mode) | blocks until match | SSE filter helper, no MCP equivalent |
| `axctl upload file` | PATH | upload + transcript signal | `POST /api/v1/uploads` + signal message |

### `axctl context`

| Subcommand | Args | Output | REST | MCP equivalent |
|---|---|---|---|---|
| `context upload-file` | PATH | reference | `POST /api/v1/context/upload` (vault flag) | `context(action='upload')` |
| `context fetch-url` | URL | reference | `POST /api/v1/context/fetch-url` | TBD |
| `context set` | KEY VALUE | confirmation | `POST /api/v1/context` | `context(action='set')` |
| `context get` | KEY | value | `GET /api/v1/context/{key}` | `context(action='get')` |
| `context list` | — | table | `GET /api/v1/context` | `context(action='list')` |
| `context delete` | KEY | confirmation | `DELETE /api/v1/context/{key}` | TBD |
| `context download` | KEY | local file | `GET /api/v1/uploads/files/...` | TBD |
| `context load` | KEY | private cache | local | n/a |
| `context preview` | KEY | preview text | local | n/a |

### `axctl profile`

| Subcommand | Args | Output | Notes |
|---|---|---|---|
| `profile add` | NAME | new profile | local-only |
| `profile use` | NAME | active profile | local-only |
| `profile list` | — | table | local-only |
| `profile verify` | NAME | fingerprint check | local-only |
| `profile remove` | NAME | confirmation | local-only |
| `profile env` | NAME | env-var dump | local-only — for shell sourcing |

### `axctl spaces`

| Subcommand | Args | Output | REST | MCP equivalent |
|---|---|---|---|---|
| `spaces list` | — | table | `GET /api/v1/spaces` | `spaces(action='list')` |
| `spaces create` | NAME | new space | `POST /api/v1/spaces` | TBD |
| `spaces get` | ID | full record | `GET /api/v1/spaces/{id}` | `spaces(action='get')` |
| `spaces members` | SPACE | member list | `GET /api/v1/spaces/{id}/members` | TBD |

### `axctl channel`

| Command | Args | Output | Notes |
|---|---|---|---|
| `axctl channel` | — | MCP stdio bridge | local-only — runs the channel bridge that this orion session uses |

### `axctl gateway`

| Subcommand | Args | Output | Notes |
|---|---|---|---|
| `gateway login` | — | session bootstrap | writes `~/.ax/gateway/session.json` |
| `gateway status` | — | daemon + agents | reads registry; **profile-drift bug `7f44c5ab`** noted |
| `gateway runtime-types` | — | list | catalog of advanced runtimes |
| `gateway templates` | — | template list | starter agent templates |
| `gateway ui` | — | local dashboard | http://127.0.0.1:8765 |
| `gateway start` | — | bg daemon | spawn `gateway run` + UI |
| `gateway stop` | — | shutdown | kill daemon |
| `gateway watch` | — | live terminal dashboard | reads activity.jsonl |
| `gateway run` | — | foreground supervisor | direct invocation (no detach) |
| `gateway agents` | (sub) | manage runtimes | `add`, `remove`, `list`, etc. |
| `gateway approvals` | (sub) | review HITL | per-binding approval flow |

### `axctl token`

| Subcommand | Args | Output | REST |
|---|---|---|---|
| `token mint` | (params) | minted token | `POST /auth/exchange` (or admin-mint endpoint) |

### `axctl qa`

| Subcommand | Args | Output | Notes |
|---|---|---|---|
| `qa contracts` | — | contract test results | runs against integration tips |
| `qa preflight` | — | pre-deploy check | local + remote checks |
| `qa widgets` | — | widget regression | MCP widget visual smoke |
| `qa matrix` | — | environment matrix | crosses ax-cli / backend / mcp |

## Cross-cutting flags

These appear on most or all CLI commands; the inventory tables don't repeat them per row:

- `--json` — machine-readable output (every list/get supports this)
- `--space-id` / `-s` — override default space for the call
- `--agent` / `-a` — override active agent identity
- `--token` / `--token-file` — override credential resolution
- `--profile` — switch named profile for one invocation
- `--env` — switch environment (`AX_ENV`)
- `--help` — typer-generated, available at every level

## Gaps vs MCP — to fill once `6699321c` lands

When mcp_sentinel's MCP inventory artifact arrives, this section becomes the **diff doc**:

- For every CLI verb without an MCP equivalent: gap row, owner = mcp_sentinel to assess
- For every MCP tool action without a CLI equivalent: gap row, owner = me / cli_sentinel to assess
- Output-shape mismatches (CLI table vs MCP JSON) flagged separately
- Auth-scope mismatches (CLI accepts user OR agent, MCP narrower) flagged separately

Rough expectation: high overlap on `messages`, `tasks`, `agents`, `spaces`, `context`. Wider gaps on `gateway` (CLI-only — Gateway control is local), `profile` (CLI-only — local credential mgmt), `channel` (CLI-only — local bridge), `qa` (CLI-only — local regression). MCP-only territory: agent-routing helpers / cloud-agent-context / dispatch hooks.

## Decision log

- **2026-04-25** — Skeleton pre-staged tonight per cipher's pulse advice. Saturday AM populate.
- (subsequent decisions land here.)
