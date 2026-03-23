# AX-CLI-SKILLS-001: CLI + Skills Operating Model

**Status:** Draft
**Owner:** @wire_tap
**Created:** 2026-03-22
**Scope:** ax-cli help/docs, local skill packaging, MCP guidance

---

## 1. Problem

`ax` is becoming a primary operator surface for autonomous agents, but the usage model is still too implicit.
Users need a clear answer to three questions:

1. When should I use `ax`?
2. When should I use a skill?
3. When should I use MCP directly?

Without that split, the platform feels inconsistent and agents drift between stale MCP patterns, ad hoc prompts, and CLI behavior that is not predictable enough.

## 2. Decision

The supported model is:

- `ax` is the boring control plane.
- Skills are the focused execution layer.
- MCP is an integration surface, not the default operator UX.

### 2.1 `ax`

Use `ax` for:

- identity and agent binding
- messaging / concierge routing
- task and agent inspection
- credential management
- event streaming / status checks

The default mode is agent-first. A saved agent binding is the normal steady state.
`--as-user` remains available, but only as an explicit admin/bootstrap escape hatch.

### 2.2 Skills

Use skills for:

- specialized local workflows
- deterministic multi-step procedures
- domain-specific guidance that should not live in the base prompt
- packaging a repeatable operator pattern around `ax`

The initial skill for this model is an `ax` control-plane skill that teaches:

- bind the correct agent first
- use `ax` for routing and control
- use skills for execution
- use native HTTP + OAuth or MCP `agent_keys` for supported MCP paths
- do not revive legacy `mcp-remote` proxy instructions

### 2.3 MCP

Use MCP when the tool surface itself is the product or when a remote client needs MCP-native access.

Supported guidance:

- remote interactive MCP: native HTTP + OAuth
- true headless MCP: backend `agent_keys` / `client_credentials`
- legacy `mcp-remote`: deprecated compatibility fallback only

## 3. Acceptance Criteria

1. CLI help and README explain the `ax + skills + MCP` split plainly.
2. CLI help presents agent-first binding as the normal workflow.
3. A local skill exists for the `ax` control-plane workflow.
4. The skill points users to CLI help instead of duplicating large command manuals.
5. The skill explicitly warns against legacy `mcp-remote` as the default setup.

## 4. Non-Goals

- Replacing MCP with skills
- Replacing `ax` with skills
- Supporting legacy `mcp-remote` as a first-class path again
