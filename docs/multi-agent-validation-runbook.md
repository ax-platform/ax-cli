# Multi-Agent Gateway Validation Runbook

Operator-driven validation of the existing Gateway control plane before any
new feature work (Gateway MCP spec, stdio transport, etc.) builds on top.

This runbook is the merge gate for:

- **PR #148** — `fix/gateway-spaces-hygiene` (registry repair, `/api/spaces`
  fallback, `spaces list`, UI label).
- **PR #147** — `feat/gateway-archive-restore` (archive/restore lifecycle +
  race-safe registry merge).
- **PR #145** — `feat/gateway-strands` (Strands runtime template, outside
  contributor).
- **PR #129** — `fix/hermes-sentinel-verbose` (Hermes verbose flag, outside
  contributor).

Outside-contributor PRs (#145, #129) get the same validation pass as our own
work before merge — runbook does not distinguish.

## Why This Exists

We have been shipping unit tests + targeted live smoke. We have not validated
the integrated story: multiple live agents of different runtime types, message
exchange across all directions, dashboard / CLI / API agreement, archive /
restore round-trips with operator-visible affordances. This runbook closes that
gap.

The rule is: do not merge anything to `main` that has not been driven through
this runbook. Outside-contributor PRs included.

## Prerequisites And Version Proof

Before any scenario:

```bash
# 1. Confirm which branch the running daemon was launched from.
cd /Users/jacob/claude_home/ax-gateway
git rev-parse --abbrev-ref HEAD            # branch
git rev-parse --short HEAD                 # commit
git status --porcelain | head -1           # dirty marker

# 2. Confirm the editable install resolves to this checkout.
pip show axctl | grep -E "Editable project location"

# 3. Confirm the daemon is running and serving.
ax gateway status
curl -s -m 3 -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8765/

# 4. Confirm the daemon process was started AFTER your last branch switch.
ps -o lstart= -p "$(cat ~/.ax/gateway/*.pid 2>/dev/null | head -1)" 2>/dev/null
```

**Pass criteria:** branch matches the PR under test, `pip show axctl` Editable
location is `/Users/jacob/claude_home/ax-gateway`, daemon HTTP returns 200, and
the daemon was started after the branch switch (otherwise the running code is
stale and every scenario below is invalid).

**If validating PR #148 + PR #147 together:** create a combined branch
`validate/spaces-hygiene-plus-archive-restore` by merging both into a fresh
branch off `main`; record the merged commit in your evidence trail.

**Known gap:** there is no in-UI version indicator yet (tracked separately).
Until that lands, the manual `git rev-parse` step is the only proof.

## Test Environment Setup

Three agent runtimes must be exercised — Echo (the simplest hosted runtime),
Hermes (sentinel runtime with claude-code subprocess), and Claude Code Channel
(attached live-listener through `axctl channel`).

```bash
# Pick a clean test space ID (ideally a non-production workspace) and export it.
export TEST_SPACE_ID="<UUID>"

# Echo (hosted, persistent, interactive reply mode).
ax gateway agents add validate_echo --template echo --space-id "$TEST_SPACE_ID" --start

# Hermes sentinel (requires --workdir).
mkdir -p /tmp/validate-hermes
ax gateway agents add validate_hermes \
    --template hermes \
    --workdir /tmp/validate-hermes \
    --space-id "$TEST_SPACE_ID" \
    --start

# Claude Code Channel (attached live listener — separate setup path).
mkdir -p /tmp/validate-cc-channel
cd /tmp/validate-cc-channel
ax channel setup     # follow approval flow at http://127.0.0.1:8765 if prompted
cd -
```

**Pass criteria:** all three agents reach `LIVE` / `IDLE` (or `working`) within
60s of `agents add`. Run:

```bash
ax gateway agents list --json | python3 -c '
import json, sys
data = json.load(sys.stdin)
agents = data.get("agents") or data
for a in agents:
    if a.get("name", "").startswith("validate_"):
        print(a["name"], a.get("mode"), a.get("presence"), a.get("desired_state"))
'
```

All three lines should show `LIVE IDLE running` (or `working`/`IDLE`).

**Capture:** screenshot of the dashboard showing all three agents online +
JSON dump of `agents list`.

## Message Exchange Matrix

Validate that messages flow in all relevant directions. Use the channel reply
tool (or `ax send`) from the operator's session; use `ax gateway local send`
or backend API calls from the agent direction. Each row must succeed and the
target inbox must receive within 30s.

| From | To | Path under test |
|---|---|---|
| operator (channel) | validate_echo | inbound mention → echo reply |
| operator (channel) | validate_hermes | inbound mention → hermes sentinel response |
| operator (channel) | validate_cc_channel | inbound mention → live attached listener |
| validate_echo | operator | agent-initiated reply path |
| validate_hermes | validate_echo | agent-to-agent across runtimes |
| validate_cc_channel | validate_hermes | live listener → sentinel runtime |

For each row:

```bash
# Operator → agent (example)
ax send "@validate_echo ping from runbook" --space-id "$TEST_SPACE_ID"

# Agent inbox check
ax gateway local inbox --workdir <agent's workdir>
```

**Pass criteria:** every row delivers within 30s, no `processing-status post
failed` errors in `~/.ax/gateway/gateway.log` during the exchange, and the
dashboard `LAST ACTIVITY` column updates for the receiving agent.

**Capture:** the gateway log slice covering the test window
(`tail -200 ~/.ax/gateway/gateway.log` after the matrix completes), plus the
inbox listings.

**Known gap:** `processing-status post failed [Errno 2] No such file or
directory: '/Users/jacob/claude_home/ax-cli/.ax/cache/tokens.json'` for
`codex_supervisor` is a pre-existing stale-path bug from the `ax-cli` →
`ax-gateway` rename. It does not gate this runbook but should be filed as a
follow-up.

## Dashboard / CLI / API Consistency

For each agent and for the workspace as a whole, the three observation
surfaces must agree:

```bash
# CLI view
ax gateway agents list --json > /tmp/cli-agents.json

# HTTP API view
curl -s http://127.0.0.1:8765/api/status > /tmp/api-status.json
curl -s http://127.0.0.1:8765/api/spaces > /tmp/api-spaces.json

# Dashboard view — eyeballed, capture screenshot
open http://127.0.0.1:8765/
```

Diff the agent set, names, modes, presence, space_ids, and counts between
CLI and API. The dashboard should match.

**Pass criteria:**

- Same set of agent names appears in CLI list and API status.
- For every agent, `space_id` is a UUID (not a name). Run:
  ```bash
  python3 -c '
  import json, re, sys
  data = json.load(open("/tmp/cli-agents.json"))
  agents = data.get("agents") or data
  uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
  bad = [a for a in agents if isinstance(a.get("space_id"), str) and a["space_id"] and not uuid_re.match(a["space_id"])]
  print(f"non-UUID space_ids: {len(bad)}")
  for a in bad: print(" ", a["name"], repr(a["space_id"]))
  '
  ```
  **Must print `non-UUID space_ids: 0`** (proves the registry is healthy and
  the space_id writer in #148 is not regressing).
- `/api/spaces` returns `active_space_id` populated even if the upstream call
  errors. If you can artificially trigger an upstream 429 (e.g. via a sequence
  of rapid calls), confirm the response still includes the active space and
  marks `cached: true` when serving from cache.

**Capture:** the three JSON dumps + dashboard screenshot.

## Archive / Restore Flow

Requires PR #147 merged into the test branch. Skip this section when
validating other PRs in isolation; it is the focal scenario for #147.

```bash
# Single archive
ax gateway agents archive validate_echo --reason "runbook test"
ax gateway agents list                             # validate_echo absent from default surface
ax gateway agents list --archived                  # validate_echo present, marked archived
ax gateway agents list --all                       # validate_echo visible alongside others

# Daemon reconcile race check — wait at least 30s past the next reconcile tick
sleep 35
ax gateway agents list --archived | grep validate_echo  # still archived (not auto-restored)

# Restore round-trip
ax gateway agents restore validate_echo
ax gateway agents list | grep validate_echo        # back in default surface
ax gateway agents show validate_echo --json        # desired_state restored to running

# Bulk archive
ax gateway agents archive validate_hermes validate_cc_channel --reason "runbook bulk"
ax gateway agents list --archived | wc -l          # 2+ archived rows
ax gateway agents restore validate_hermes validate_cc_channel
```

**Pass criteria:**

- Archive removes the agent from the default `agents list` view.
- Archive does **not** remove the agent from the upstream backend (verify by
  inspecting the agent record on `paxai.app` — it should still exist).
- Archived agents survive at least one daemon reconcile tick without being
  auto-restored or auto-hidden.
- Restore returns the agent to default visibility with `desired_state` honoring
  the captured `desired_state_before_archive`.
- Bulk operations succeed atomically — partial failure should not leave a
  mixed state.

**Dashboard affordances (known gap):** archive / restore are CLI-only as of
PR #147. The dashboard does not expose an Inactive section, edit-mode, or
multi-select. Operator UX completeness for archive blocks #147 merge until
those affordances ship.

**Capture:** the dashboard before/after each step + JSON output of the agent
record after each transition.

## Create-Agent 429 Handling

Reproduces the operator-visible failure mode supervisor hit during the smoke
test on 2026-05-06. Validates that the UI degrades gracefully rather than
leaving the operator wondering whether the agent was created.

```bash
# Trigger the create flow through the dashboard.
# 1. Open http://127.0.0.1:8765/
# 2. Pick Runtime=Echo, name="validate_429_check_<rand>"
# 3. Confirm the Space picker shows the actual space name (not "Current space")
#    — this validates PR #148's UI label fix.
# 4. Submit.
```

**Pass criteria:**

- Space picker shows the resolved space name (not the literal string
  `"Current space"`).
- On success: agent appears in the dashboard within 5s; CLI `agents list`
  shows the new row; HTTP `/api/status` includes it.
- On 429 from upstream: the UI must show an actionable message (rate limited,
  retry suggested) and must not double-create on retry. Inspect
  `/api/status` after a 429 to confirm no half-created row was persisted.

**Known gap:** the UI's 429 handling is not yet graceful — supervisor's smoke
saw a raw 429 from `paxai.app/api/v1/agents` with no operator guidance. This
is a follow-up after the runbook lands; it is recorded here so we don't lose
it.

**Capture:** screenshot of the picker showing the resolved space name +
network tab capture of the 429 (when reproducible).

## Pass / Fail Criteria Summary

The runbook **passes** for a given PR when every section above shows expected
behavior on the branch under test, all evidence captures are recorded, and no
unexplained errors appear in `~/.ax/gateway/gateway.log` during the test
window.

The runbook **fails** when any of the following occur:

- Any agent fails to reach LIVE within 60s of creation.
- Any message in the exchange matrix fails to deliver within 30s.
- Any agent row has a non-UUID `space_id` after the test sequence.
- `/api/spaces` returns `active_space_id: null` while the bootstrap session
  has a known current space.
- Archive does not survive a daemon reconcile tick (auto-restored or
  auto-hidden).
- Restore fails to honor the captured `desired_state_before_archive`.
- The daemon log contains errors not explained by Known Gaps below.

A failure on any single PR's focal scenario blocks that PR's merge until
fixed. A failure on a section unrelated to a PR is logged as a follow-up but
does not block that PR.

## Evidence To Capture

Per validation run, store under
`~/ax-gateway-validation/<date>-<branch>-<commit>/`:

- `branch.txt` — output of the prerequisite block (branch, commit, dirty,
  pip location, daemon start time).
- `agents-before.json`, `agents-after.json` — `ax gateway agents list --json`
  before and after the run.
- `api-status.json`, `api-spaces.json` — captured at the consistency-check
  step.
- `gateway.log.window` — the slice of `~/.ax/gateway/gateway.log` covering
  the test window.
- `dashboard-*.png` — screenshots at each capture point named in this doc.
- `notes.md` — operator's running notes, including any operator-visible
  surprises that did not block but warrant a follow-up.

These get attached to the PR review as the merge gate.

## Known Gaps (Not Blocking This Runbook)

- **`codex_supervisor` stale-path token cache**: `processing-status post
  failed [Errno 2] No such file or directory:
  '/Users/jacob/claude_home/ax-cli/.ax/cache/tokens.json'`. Pre-existing,
  unrelated to any current PR. Follow-up: rebind the cache path to the
  renamed repo location.
- **No in-UI version indicator**: branch + commit + dirty marker should
  appear in the dashboard header. Tracked separately. Until shipped, the
  manual `git rev-parse` step in Prerequisites is the only proof.
- **Archive / restore UI affordances missing**: PR #147 ships CLI + data
  layer only. Dashboard Inactive section, edit-mode, and multi-select are
  follow-up work that gates #147's final merge.
- **Create-agent 429 UX is raw**: upstream rate limiting surfaces as a
  generic 429 with no retry guidance. Follow-up after this runbook lands.
- **Spaces cache is best-effort**: `gateway_dir()/spaces.cache.json` writes
  swallow OSError. If the cache file goes missing, a transient upstream 429
  will fall back to session-only data. Acceptable for v1.

## Which PRs This Runbook Gates

| PR | Branch | Focal sections |
|---|---|---|
| **#148** | `fix/gateway-spaces-hygiene` | Prerequisites, Consistency (UUID check + `/api/spaces` fallback), Create-Agent (UI label) |
| **#147** | `feat/gateway-archive-restore` | Prerequisites, Archive / Restore Flow, Daemon reconcile race check |
| **#145** | `feat/gateway-strands` (outside contributor) | Setup (Strands template instantiation), Message Exchange (Strands runtime) |
| **#129** | `fix/hermes-sentinel-verbose` (outside contributor) | Setup (Hermes verbose flag), Message Exchange (Hermes runtime) |

PRs **must** be driven through their focal sections before merge. Sections
not focal to a given PR are run for regression coverage but are not the merge
gate for that PR specifically.

## Out Of Scope

- Gateway MCP spec or stdio transport validation. That work is gated behind
  this runbook landing successfully on `main`.
- Performance or load testing. This runbook validates correctness, not
  capacity.
- Cross-environment (dev / staging / prod) drift checks. See
  `operator-qa-runbook.md` for that path.
