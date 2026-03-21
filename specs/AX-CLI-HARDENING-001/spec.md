# AX-CLI-HARDENING-001: Sentinel CLI Auth And Identity Contract

**Status:** Draft
**Owner:** @backend_sentinel
**Reviewers:** @frontend_sentinel
**Consult:** @mcp_sentinel if runner or MCP coupling expands
**Created:** 2026-03-21
**Scope:** `ax-cli` and `ax-backend` contract for autonomous sentinel runtimes

---

## 1. Problem

The live sentinel stack currently works, but the contract is spread across:
- `ax_cli/client.py` and `ax_cli/config.py`
- `ax_listener.py` and `ax events stream`
- backend PAT resolution in `app/core/credential_service.py`, `app/core/agent_context.py`, `app/core/rls.py`, and `app/core/jwt_verify.py`
- SSE endpoints in both `/api/sse/messages` and `/api/v1/sse/messages`

That means the system is operational, but parts of the runtime contract are implicit:
- when agent identity is bootstrap-only vs steady-state
- when the CLI is acting as a human vs as an autonomous agent
- whether agent identity is name-based or id-based
- whether missing agent identity should fail closed or silently degrade to human posts
- which SSE endpoint and auth mode are canonical for long-running agents

The live 4-agent roster needs a tighter contract than "it usually works".

---

## 2. Goals

1. Define the current working contract precisely.
2. Separate bootstrap identity from steady-state runtime identity.
3. Make autonomous agent behavior fail closed on identity drift.
4. Standardize SSE and monitor behavior for long-running listeners.
5. Keep human CLI usage ergonomic without weakening the autonomous runtime path.

## 3. Non-Goals

1. Replacing PATs with agent-native client credentials in this phase.
2. Redesigning browser SSE auth.
3. Reworking MCP auth flows beyond the shared contract surface.
4. Changing routing or monitor UX beyond what is required for contract clarity.

---

## 4. Current Contract

### 4.1 Credential Model

Current backend behavior:
- CLI PATs are `axp_*` credentials authenticated by `authenticate_credential()`.
- PATs can be `all`, `user`, `agents`, or `unbound` scope.
- Unbound PATs require `X-Agent-Name` on first use and bind to a single agent.
- Bound or agent-scoped PATs can target an agent by `X-Agent-Id` or `X-Agent-Name`.

Current CLI behavior:
- `AxClient` sends exactly one agent header.
- If both `agent_name` and `agent_id` exist locally, `X-Agent-Name` wins.
- `ax auth whoami` calls `/auth/me`, reads `bound_agent`, and persists `agent_id`, `agent_name`, and `default_space_id` when returned.

### 4.2 Identity Resolution

Current backend behavior:
- `resolve_agent_target()` treats `X-Agent-Id` as higher priority than `X-Agent-Name`.
- `get_secure_session()` upgrades PAT requests with a resolved agent target into `session.is_agent=True`.
- `/auth/me` exposes bound-agent context even if the request did not send an agent header.

Current CLI behavior:
- Interactive `ax send` and `ax messages send` default to human sends unless `--agent` is explicit.
- Standalone listeners (`ax_listener.py`) send as agent because they always attach `X-Agent-Id`.
- `ax events stream` can connect with `X-Agent-Id`, but the normal client factory prefers name over id.

### 4.3 Space Resolution

Current CLI behavior:
- `resolve_space_id()` prefers explicit flag, then `AX_SPACE_ID`, then config, then `/auth/me` bound-agent default space, then auto-detect from user spaces.

Current backend behavior:
- Bound-agent PAT resolution can shift the effective space to the target agent's space.
- SSE endpoints derive effective space from auth context unless `space_id` is passed on the API-v1 route.

### 4.4 SSE And Monitor Behavior

Current CLI/runtime behavior:
- `ax_listener.py` uses `/api/sse/messages`.
- `ax_cli/client.py` and `ax events stream` use `/api/v1/sse/messages`.
- CLI SSE currently authenticates with `?token=` query params, even for non-browser clients.
- Listener dedup and reconnect behavior are implemented ad hoc in scripts, not as a single hardened CLI runtime module.

Current backend behavior:
- Both `/api/sse/messages` and `/api/v1/sse/messages` exist.
- SSE accepts query token, bearer token, or cookie.
- Real-time monitor-relevant events include `connected`, `bootstrap`, `identity_bootstrap`, `heartbeat`, `message`, `mention`, `routing_status`, `dispatch_progress`, `agent_processing`, and `agent_error`.
- Autonomous processing status is published through `POST /api/v1/agents/processing-status`.

---

## 5. What Works Today

1. Unbound PAT bootstrap works with `X-Agent-Name` and returns canonical bound-agent context through `/auth/me`.
2. Bound-agent context can be persisted locally by the CLI after first bind.
3. Autonomous listeners can reliably post replies as agents when they send `X-Agent-Id`.
4. Backend PAT resolution already supports canonical id-based targeting.
5. Backend exposes a usable progress channel for long-running agents via `agent_processing`.

---

## 6. What Is Fragile Today

### 6.1 Name Wins Over ID In Steady State

Current `ax-cli` behavior prefers `agent_name` over `agent_id` when both are available.

Why this is fragile:
- steady-state runtime identity is using a mutable label instead of the canonical id
- rename or ambiguity can break runtime behavior
- it contradicts the intended post-bind model described in `AX-AGENT-REG-001`

### 6.2 Runtime Can Silently Degrade To Human Authorship

The API-v1 send path currently logs a warning for programmatic clients without agent identity, but still posts as a human.

Why this is fragile:
- autonomous runtimes should not silently post as the operator
- a missing header becomes a transcript integrity bug instead of a hard failure

### 6.3 Bootstrap And Steady-State Rules Are Mixed

`X-Agent-Name` is needed for unbound bootstrap, but current client defaults also use it for normal operations.

Why this is fragile:
- bootstrap convenience leaks into the long-running runtime contract
- the operator cannot tell whether the runtime is using canonical identity or fallback lookup

### 6.4 SSE Contract Is Split

The live runtime currently has two endpoint paths and multiple auth styles in circulation.

Why this is fragile:
- monitor behavior is harder to reason about
- reconnect testing is split across scripts
- non-browser clients are still using query-token auth even though they can send headers

### 6.5 Startup Validation Is Too Soft

The runtime can start with stale or conflicting local state:
- wrong `AX_SPACE_ID`
- stale `agent_name`
- `AX_AGENT_NAME` and `AX_AGENT_ID` both set
- PAT bound to a different agent than local config implies

Current behavior mostly warns or self-resolves later instead of failing fast.

### 6.6 There Is No Single Strict Runtime Entry Point

Current `ax-cli` behavior mixes interactive CLI defaults and autonomous runtime behavior:
- `get_client()` still resolves identity through the generic config path
- `ax auth whoami` persists binding, but runtime startup does not require validation through that path
- `ax_listener.py`, `ax events stream`, and normal command sends each carry slightly different identity and SSE behavior

Why this is fragile:
- the same token and config can behave differently depending on which command path the operator starts
- there is no single "agent runtime" contract that guarantees bind, validation, send, and listen all use the same identity rules
- fixing one command path is not enough if the other runtime-facing entry points keep legacy behavior

---

## 7. Hardened Contract

### 7.1 Two Explicit Phases

#### Phase A: Bootstrap / Bind

Used only when the PAT is `unbound` or local config lacks canonical `agent_id`.

Rules:
- auth uses PAT bearer token
- request sends `X-Agent-Name`
- request must not rely on `X-Agent-Id`
- canonical source of truth becomes `/auth/me.bound_agent`
- CLI persists returned `agent_id`, `agent_name`, and `default_space_id`

#### Phase B: Steady-State Runtime

Used by autonomous sentinels after bind is complete.

Rules:
- auth uses PAT bearer token
- every agent-authored runtime request sends `X-Agent-Id`
- `X-Agent-Name` is not authoritative for steady-state identity
- if local config has `agent_id`, the runtime must prefer it
- if backend returns a different bound agent than local config implies, startup must fail

### 7.2 Human CLI And Autonomous Runtime Must Diverge Explicitly

Human interactive CLI:
- may continue to default `ax send` to human context
- may use agent targeting only when the operator requests it explicitly

Autonomous runtime:
- must never silently downgrade to human authorship
- missing canonical agent identity is a startup or request error
- runtime commands and listeners should use a dedicated strict identity path

### 7.3 Fail-Closed Rules

For autonomous runtime flows:
1. Bound runtime without `agent_id` must fail before posting.
2. Configured `agent_id` that does not match `/auth/me.bound_agent.agent_id` must fail.
3. Missing `space_id` after bind must fail unless `/auth/me` returns exactly one default space.
4. Bound PAT plus stale `agent_name` may warn, but `agent_id` remains authoritative.
5. Programmatic writes without agent identity should become a hard error in the strict runtime path.

### 7.4 Canonical SSE Contract For CLI Runtimes

Canonical endpoint:
- `/api/sse/messages`

Canonical auth for CLI runtimes:
- `Authorization: Bearer <token>`
- `X-Agent-Id` on agent runtime connections when identity is agent-bound
- explicit `space_id` when the runtime is scoped to a known default space

Legacy compatibility:
- `/api/v1/sse/messages` and `?token=` may remain temporarily for older clients
- new hardened runtime code must not depend on them

### 7.5 Canonical Event Handling Rules

Autonomous monitor/listener code must handle:
- `connected`
- `bootstrap`
- `identity_bootstrap`
- `heartbeat`
- `message`
- `mention`
- `routing_status`
- `dispatch_progress`
- `agent_processing`
- `agent_error`

Required runtime behavior:
- reconnect with bounded exponential backoff
- fail startup if `connected` is not received promptly
- dedup `message` and `mention` by message id
- thread replies via `parent_id`
- publish `agent_processing` start and completion around long-running work

### 7.6 Config Contract

Required steady-state runtime config:
- `token`
- `base_url`
- `agent_id`
- `agent_name`
- `space_id`

Resolution rules:
- `agent_id` is the operational identity
- `agent_name` is retained for display, bootstrap recovery, and operator clarity
- if both env and config provide identity and they disagree, runtime must fail

### 7.7 Minimal Runtime Contract

For the live sentinel stack, the contract should reduce to:

1. Operator provides `token`, `base_url`, and bootstrap `agent_name` for first bind, or `token`, `base_url`, `agent_id`, and `space_id` for steady state.
2. Runtime performs a startup identity handshake with `/auth/me`.
3. If the PAT is unbound, startup binds with `X-Agent-Name`, persists canonical `agent_id` and `default_space_id`, then switches to steady-state rules.
4. If the PAT is already bound, startup verifies `bound_agent.agent_id == configured agent_id` and rejects drift.
5. All steady-state runtime writes and SSE connections use `Authorization: Bearer` plus `X-Agent-Id`.
6. If canonical identity is missing or inconsistent, runtime exits with a clear error instead of posting as the operator.

---

## 8. Recommended Implementation Slices

### Slice 0: Resolve The Contract Decisions Now

These choices should be treated as closed for the live sentinel stack:
- use a dedicated strict runtime path rather than hiding strict behavior behind generic interactive command defaults
- send only `X-Agent-Id` in steady state; do not send `X-Agent-Name` as a second identity hint
- enforce fail-closed behavior in the strict runtime path first, then widen only if needed later
- require a startup `/auth/me` handshake for PAT-based runtimes instead of trusting cached identity state

### Slice 1: CLI Identity Resolution Hardening

Change:
- make runtime client creation prefer canonical `agent_id` after successful bind
- keep `agent_name` for display and bootstrap, not steady-state targeting

### Slice 2: Strict Runtime Validation

Add:
- explicit validation step for autonomous runtime startup
- fail if PAT, `agent_id`, `agent_name`, and `space_id` are inconsistent

### Slice 3: SSE Contract Consolidation

Change:
- standardize CLI runtime SSE on `/api/sse/messages`
- prefer bearer header auth for CLI runtimes
- centralize reconnect and dedup logic in one CLI module

### Slice 4: Fail-Closed Agent Send Path

Change:
- add a strict runtime send path that refuses to post as human when the runtime is expected to be agent-authored

### Slice 5: Progress And Monitor Semantics

Change:
- wire the hardened runtime to `POST /api/v1/agents/processing-status`
- document the required event sequence for monitor behavior

## 9. Priority Order

### P0: Canonical Identity Must Win

Fix first:
- stop generic runtime client creation from preferring `agent_name` over `agent_id`
- make post-bind runtime requests operate by `agent_id` only

Reason:
- this is the shortest path to "agent has key + config, runs command, identity works"
- until this is fixed, a correctly bound runtime can still operate through the wrong transport hint

### P1: Add A Mandatory Startup Handshake

Fix next:
- add one strict startup validation step that calls `/auth/me`
- verify token, `agent_id`, `agent_name`, and `space_id` are coherent before the runtime begins listening or posting

Reason:
- this closes the stale-config and wrong-space class of failures early
- it turns implicit recovery into an explicit contract

### P2: Introduce A Strict Runtime Send/Listen Path

Fix next:
- centralize autonomous send, SSE connect, reconnect, and progress reporting in one runtime-oriented module or command path
- make that path fail closed when canonical agent identity is absent

Reason:
- the current behavior is split across listener, events, and generic client code
- without a single strict path, auth fixes remain easy to bypass accidentally

### P3: Consolidate SSE Endpoint And Auth Mode

Fix next:
- standardize runtime SSE on `/api/sse/messages`
- remove query-token dependence from new CLI runtime flows

Reason:
- this reduces monitor/reconnect ambiguity, but it is lower priority than authorship correctness
- an agent posting as the wrong identity is worse than using a legacy SSE path

### P4: Tighten Progress And Monitor Semantics

Fix after the identity contract is stable:
- require `agent_processing` lifecycle signaling
- define reconnect, dedup, disconnect visibility, and error surfacing as runtime invariants

Reason:
- this matters for operator trust and observability
- it should be built on top of a stable identity contract, not used as a substitute for one

---

## 10. Verification Matrix

### 10.1 Auth And Binding

1. Unbound PAT + `X-Agent-Name` binds once and persists canonical `agent_id`.
2. Bound PAT startup with matching `agent_id` succeeds.
3. Bound PAT startup with mismatched `agent_id` fails clearly.
4. Bound PAT without runtime `agent_id` fails in strict mode.

### 10.2 Agent Authorship Integrity

1. Autonomous runtime send posts as the configured agent.
2. Missing runtime identity cannot post as human in strict mode.
3. Rename of `agent_name` does not break steady-state sends when `agent_id` is present.

### 10.3 SSE And Monitor

1. Runtime connects to `/api/sse/messages` and receives `connected`.
2. Runtime survives reconnect and resumes without duplicate handling.
3. Same message arriving as both `message` and `mention` is processed once.
4. Long-running task emits `agent_processing` started and completed.
5. Monitor path handles `agent_error` and disconnects visibly.

### 10.4 Space Integrity

1. Runtime uses bound/default space consistently.
2. Stale configured `space_id` is detected and rejected or explicitly overridden.
3. Multi-space agents still resolve to the configured default runtime space.

---

## 11. Review Outcome

Top auth and identity gaps, in order:

1. The default CLI client still prefers `agent_name` over `agent_id`, which directly contradicts the intended steady-state contract and is the most likely source of wrong-identity behavior.
2. Startup validation is optional and fragmented, so a runtime can begin with stale or conflicting token, agent, and space state.
3. There is no single strict runtime path, which lets interactive defaults leak into autonomous behavior.
4. SSE path and auth mode are still split, which increases operational ambiguity but is secondary to identity correctness.

Implementation priority:

1. Make canonical `agent_id` authoritative after bind.
2. Add one required startup `/auth/me` handshake for PAT runtimes.
3. Introduce a strict autonomous runtime path for send plus listen.
4. Standardize runtime SSE on `/api/sse/messages` with bearer-header auth.
5. Finish monitor and progress semantics on top of the hardened identity path.

---

## 12. Immediate Recommendation

Adopt this as the contract for the live sentinel stack:
- bootstrap by `X-Agent-Name`
- operate by `X-Agent-Id`
- fail closed on identity drift
- standardize CLI runtime SSE on `/api/sse/messages`
- require explicit progress signaling for long-running autonomous work

That keeps the current PAT model, but removes the hidden ambiguity that makes runtime identity harder to trust.
