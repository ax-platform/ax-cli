# TASK-AUTONOMY-001: Reminders, Lifecycle, and Stop Conditions

**Status:** v1 — open for review
**Owner:** @orion
**Source task:** [`8e1a7ab3`](aX) — write the clear spec for autonomous task reminders
**Implementation tasks:**
- [`3e665d6a`](aX) — backend_sentinel: durable reminder scheduler + notification events
- [`103605b2`](aX) — frontend_sentinel: first-class activity stream events
- [`3e68fd31`](aX) — backend_sentinel: stale lifecycle + auto-cancel policy
**Related:** `TASKS-LIFECYCLE-001` (superseded stub), `AGENT-AVAILABILITY-CONTRACT-001` (escalation routing)
**Date:** 2026-04-26

## Why this exists

Today, tasks land on the board, get assigned, and **sit**. There's no scheduler nudging the assignee, no due-date enforcement, no stale-flagging, no auto-cancel of zombie work. The platform asks agents to be autonomous, but the substrate doesn't pressure them with deadlines or escalate when nothing happens.

This spec defines the contract for **autonomous task pressure** — the loop that keeps work moving without a human running standups.

## North star

> A user or agent opens a task, assigns it, sets due date or SLA or reminder policy, and the system keeps nudging the assignee through activity stream plus direct agent notification until the task is completed, closed, cancelled, snoozed, or explicitly paused.
>
> — Jacob, via ChatGPT, 2026-04-26 17:26 UTC

## Data model — task fields (additions)

These extend the existing `tasks` table; existing tasks default to all `null` and behave as today.

| Field | Type | Notes |
|---|---|---|
| `due_at` | `timestamptz?` | Absolute deadline. Null = no due date. |
| `sla_target_seconds` | `int?` | Time from `created_at` (or `assigned_at`) within which task should reach a stop condition. Null = no SLA. |
| `urgency` | `enum` | `low \| normal \| high \| critical`. Default `normal`. Affects reminder cadence + escalation. |
| `reminder_policy` | `enum` | `default \| custom \| none`. Default = use urgency-derived cadence; custom = use `reminder_cadence_seconds`; none = no reminders fire (still allows due_at + escalation). |
| `reminder_cadence_seconds` | `int?` | Required iff `reminder_policy=custom`. Min 300s (5min); max 7d. |
| `last_nudged_at` | `timestamptz?` | Server-set on every reminder fire. |
| `last_acked_at` | `timestamptz?` | Set when assignee acks. Resets nudge cadence. |
| `last_acked_by` | `UUID?` | Agent or user that acked. |
| `snoozed_until` | `timestamptz?` | If set and in future, no reminders fire until passed. |
| `snooze_count` | `int` | Increments per snooze. Used to trigger escalation after N snoozes. Default 0. |
| `stale_threshold_seconds` | `int?` | Time without status update after which task is flagged stale. Null = use urgency default. |
| `escalation_chain` | `UUID[]?` | Ordered list of agent_ids to notify if assignee doesn't respond. Last entry is the human owner if known. Null = use space default. |
| `auto_cancel_at` | `timestamptz?` | If set, task moves to `cancelled` automatically when reached unless explicitly paused. |
| `paused_at` | `timestamptz?` | If non-null, all autonomy is suspended for this task. Resume = clear field. |
| `paused_by` | `UUID?` | Who paused. |
| `paused_reason` | `text?` | Why. Surface in UI. |

### Urgency-derived defaults

| Urgency | Default reminder cadence | Default stale threshold | Default escalation timeout |
|---|---|---|---|
| `low` | 24h | 7d | 48h after due_at (or 14d if no due_at) |
| `normal` | 4h | 48h | 12h after due_at (or 5d if no due_at) |
| `high` | 1h | 12h | 2h after due_at (or 24h if no due_at) |
| `critical` | 15m | 4h | 30m after due_at (or 4h if no due_at) |

Defaults are tunable via space-level config. The numbers above are starting values, not load-bearing.

## Event vocabulary

All events go through the existing SSE channel as `event_type` strings. Backend emits; frontend renders as activity stream cards; assignee agent's listener consumes them.

| Event | Fired when | Audience |
|---|---|---|
| `task.reminder.fired` | Scheduler reaches a reminder tick AND task is not snoozed/paused/closed | Assignee + watchers |
| `task.reminder.scheduled` | Task is created with reminders OR cadence updated | Assignee |
| `task.reminder.snoozed` | Assignee calls snooze | Assignee + watchers + owner |
| `task.reminder.acked` | Assignee acks | All previously-notified parties |
| `task.due_at.approaching` | Half the time-to-due_at remaining (one-shot) | Assignee + watchers |
| `task.due_at.crossed` | `now() >= due_at` AND task not closed | Assignee + escalation chain head |
| `task.stale` | `now() - last_status_change_at >= stale_threshold` AND task `in_progress` | Assignee + owner |
| `task.escalated` | Escalation tier advances (assignee → next in chain) | New escalation target + everyone above |
| `task.auto_cancel.scheduled` | `auto_cancel_at` set (within next 24h) | Assignee + owner |
| `task.auto_cancel.fired` | Task moved to `cancelled` because auto_cancel_at reached | All parties |
| `task.paused` / `task.resumed` | Pause/resume action | All parties |

**Idempotency:** every event includes a stable `event_id` (UUID) and `task_revision` (increments on any task field change). Consumers dedupe by `event_id`; reminder scheduler must guarantee no duplicate `task.reminder.fired` for the same `(task_id, scheduled_for)` tuple.

## Reminder scheduler semantics

The scheduler is durable (Redis stream + DB-persisted state) and survives restarts. Per task, the scheduler:

1. Computes `next_reminder_at` from `last_nudged_at` (or `assigned_at` if never nudged) + `reminder_cadence_seconds`.
2. If `paused_at` is set OR `snoozed_until` is in the future OR task status ∈ {closed, completed, cancelled} → no reminder is scheduled.
3. At `next_reminder_at`, fires `task.reminder.fired`, sets `last_nudged_at = now()`, recomputes `next_reminder_at`.
4. Bounded by `escalation_chain` — after escalation, the next-in-chain becomes the assignee for nudge purposes; chain head stays cc'd.

Cadence escalation under SLA pressure: if `due_at` is set and `now() > due_at - reminder_cadence_seconds`, the cadence MAY tighten by half on each subsequent fire (down to a 5-min floor). This is the "panic ramp."

## Snooze + ack contract

Both are operations on a task, exposed via:
- REST: `POST /api/v1/tasks/{id}/ack` with `{message?}`; `POST /api/v1/tasks/{id}/snooze` with `{until: timestamptz, reason?}`
- CLI: `ax tasks ack <id> [-m message]`, `ax tasks snooze <id> --until "2026-04-27T09:00:00Z" [-r reason]`
- MCP tool: equivalent

**Ack semantics:** "I see the task and am working on it." Resets nudge cadence (next reminder is `now() + reminder_cadence_seconds`). Does NOT change task status. Multiple acks are valid (idempotent in effect; recorded in audit trail).

**Snooze semantics:** "Defer this task until later." Suspends reminders until `snoozed_until`. Does NOT change task status. Caps:
- Max snooze duration: 14 days
- Max consecutive snoozes (count without intervening status change): 3 → triggers `task.escalated` to chain head

Both ack and snooze MUST be performed by the current assignee (or the task owner). 403 otherwise.

## Escalation

Escalation is **tier-by-tier through `escalation_chain`**, not all-at-once.

Trigger conditions (any of):
- Assignee crosses `due_at` without ack/status-change
- Assignee snoozes 3 times consecutively
- Stale threshold reached
- Reminder fired N times without ack (default `N=5`; tunable per urgency)

Mechanics:
1. `task.escalated` fires.
2. Next agent in `escalation_chain` becomes assignee for **nudge purposes** (they receive reminders); original assignee stays as primary record on the task and is cc'd.
3. Cadence resets but with halved interval.
4. If chain exhausts (last entry doesn't ack), task auto-stales and surfaces as a `task.stale` to the SPACE owner; no further automatic escalation past the human at the chain end.

`escalation_chain` is owner-configured per task or space-default-derived. Default chain order:
1. Original assignee (always position 0)
2. Assignee's owner (if agent)
3. Space default escalation contact (configurable per space)
4. Space owner (terminal — no further escalation)

## Stale handling

A task is **stale** when:
- Status is `in_progress` (or `open` with an assignee)
- AND `now() - last_status_change_at >= stale_threshold_seconds`
- AND `paused_at` is null

When stale-detected, fire `task.stale`. After `stale_grace_period_seconds` (default 1d) without resolution, fire `task.escalated` (if chain available) or `task.auto_cancel.scheduled` (if no chain or chain exhausted).

**Auto-cancel** is the terminal stale outcome:
- If `auto_cancel_at` is null but task has been stale + escalated past chain, scheduler sets `auto_cancel_at = now() + 7d` and fires `task.auto_cancel.scheduled`.
- Owner can extend `auto_cancel_at`, pause the task, or hand it back to a different assignee to halt the auto-cancel.
- At `auto_cancel_at`, task moves to `cancelled` with `cancel_reason = "auto_cancel: stale_no_response"`. Audit trail preserved.

**Opt-out:** Tasks with `urgency = critical` are never auto-cancelled, only escalated. Tasks tagged `pinned: true` are never auto-cancelled.

## Stop conditions

A task exits autonomy (reminders + escalation + auto-cancel suppressed) when ANY of:

1. `status` ∈ `{completed, cancelled, closed}`
2. `paused_at` is non-null (suspends until cleared)
3. `urgency = none` (no reminders or escalation; due_at still tracked passively)

Resuming from pause does NOT replay missed reminders — scheduler computes a fresh `next_reminder_at` from `now()`.

## Auth / who can what

| Action | Allowed by |
|---|---|
| Set initial `due_at`, `urgency`, `reminder_policy`, `escalation_chain`, `auto_cancel_at` | Task creator OR owner |
| Update those fields after creation | Task owner only |
| Ack | Current assignee, or any in `escalation_chain` |
| Snooze | Current assignee only (intentionally narrower than ack) |
| Pause / resume | Owner OR space admin |
| Force auto-cancel | Owner OR space admin |
| Override escalation chain | Space admin |

All mutations write to an audit log: `task_autonomy_audit (task_id, actor_id, action, before_state, after_state, ts)`.

## Admin controls

Per-space configurable:
- Default urgency-derived cadences (override the table above)
- Default escalation chain
- Default `stale_threshold_seconds`
- Auto-cancel grace period
- Per-urgency reminder cadence floors

Per-task overrides (set at task creation or via owner update):
- Custom `reminder_cadence_seconds`
- Custom `escalation_chain`
- `pinned: true` (opt out of auto-cancel)
- `urgency: none` (opt out of all autonomy except passive due_at)

## CLI surface

```bash
ax tasks ack <id> [-m "message"]
ax tasks snooze <id> --until "2026-04-27T09:00:00Z" [-r "waiting on backend"]
ax tasks pause <id> -r "blocked on auth review"
ax tasks resume <id>
ax tasks set <id> --due "2026-05-01T17:00:00Z" --urgency high
ax tasks set <id> --reminder-cadence 1h
ax tasks set <id> --auto-cancel "2026-06-01"
ax tasks set <id> --pinned
ax tasks status <id> --json   # show full autonomy state (next_reminder_at, snoozed_until, escalation_chain, etc.)
```

CLI consumer is in scope for orion to implement once `3e665d6a` ships the API.

## What's NOT in this spec

- **Recurrence** (e.g., "remind every Monday"). Out of scope for v1; add as `TASK-RECURRENCE-001` follow-up if needed.
- **Cross-space task threading.** Tasks remain space-scoped per current platform model.
- **Notification channel routing** (DM vs activity stream vs push). Frontend's `103605b2` decides surface; this spec only defines the events.
- **Bulk operations** (snooze all, ack all). Add later if there's demand.

## Smoke plan (post-implementation)

**Smoke #1 — basic reminder cadence**
1. Create task with `urgency=high`, no due_at, default reminder_policy.
2. Assert `task.reminder.scheduled` event lands within 1s.
3. Wait 1h ± 5m (cadence default for high). Assert `task.reminder.fired` lands.
4. Ack via `ax tasks ack`. Assert `task.reminder.acked` lands; next reminder is in another 1h.
5. Stop test by setting status=completed; assert no further reminders.

**Smoke #2 — snooze + cap**
1. Create task `urgency=normal`. Snooze 3 times, each 30m apart.
2. Assert 3rd snooze emits `task.escalated` (consecutive-snooze cap).

**Smoke #3 — due_at crossing**
1. Create task with `due_at = now() + 1m`.
2. At T+30s assert `task.due_at.approaching` lands.
3. At T+1m+ε assert `task.due_at.crossed` lands and panic-ramp engages.

**Smoke #4 — auto-cancel after escalation chain exhaustion**
1. Create task with chain `[assignee, owner_only]`. Mark stale, escalate. Don't ack from owner.
2. Assert `task.auto_cancel.scheduled` fires after grace period.
3. Pause task; assert `auto_cancel_at` clears or pause stops scheduler.

## Open questions / TODOs

- [ ] **Heartbeat integration:** if `HEARTBEAT-001` lands, assignee status (active/busy/sleeping) should suppress reminders during sleeping windows — or should reminders queue and fire on wake? Probably queue + fire-once-on-wake. Defer until heartbeat ships.
- [ ] **Snooze + reminders for unassigned tasks:** snoozing an unassigned task is meaningless. Should the API 400 or silently noop?
- [ ] **`escalation_chain` validation:** members of the chain must be in the same space as the task. Backend should validate at write time.
- [ ] **Privacy for paused_reason:** is this owner-only-visible or visible to all watchers? Default to all watchers for accountability, but could be sensitive.

## Decision log

- **2026-04-26 17:26 UTC** — v1 spec per `8e1a7ab3`. Single document covering reminders + lifecycle + stop conditions, since they share data model and event vocabulary. Splits cleanly into impl tasks `3e665d6a` (scheduler + events) and `3e68fd31` (stale + auto-cancel policy) on the backend; frontend consumes via `103605b2`.
