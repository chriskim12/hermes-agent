# Closed-loop Kanban Autopilot ADR and operating contract

## ADR: bounded controller, not executor

Autopilot is a controller/policy/evidence layer over the existing Kanban dispatcher. It may read live Kanban, evaluate scope and policy, select a candidate, prepare handoff-shaped evidence, and later invoke approved dispatcher handoff paths. It must not become a second dispatcher or a local completion ledger.

The existing Kanban dispatcher remains the owner of worker lifecycle accounting: claim, spawn, retry, timeout, crash recovery, run records, and worker completion. Handoff success is not worker completion. `worker_done` truth must come from Kanban/dispatcher/worker evidence, not from local controller optimism.

## Authority ceiling

Default ceiling: review-ready PR package or no-code review package.

Forbidden without separate current-turn approval:

- gateway restart or reload;
- config, env, secret, provider, billing, or pricing mutation;
- live worker dispatch/claim/spawn before the relevant activation slice explicitly opens that test boundary;
- fork push or PR before BO-091 through BO-098 are all local `worker_done`;
- upstream PR creation or mutation;
- merge, release, deploy, production mutation, or customer-visible action;
- canonical main sync/materialization.

## State machine

Allowed states:

1. `disabled` — no new Autopilot work.
2. `dry_run` — read and simulate decisions only.
3. `single_flight` — at most one approved dispatcher handoff.
4. `bounded_multi_tick` — repeated ticks inside strict caps.
5. `parent_scoped` — bounded autonomy under one approved parent.
6. `lane_scoped` — bounded autonomy under approved lane/repo selectors.
7. `paused` — operator or policy pause; no new handoff.
8. `hard_stopped` — safety stop; recovery evidence required.
9. `needs_human` — ambiguity or approval gap requires Chris.

Promotion ladder:

- `dry_run` -> `single_flight`
- `single_flight` -> `bounded_multi_tick`
- `bounded_multi_tick` -> `parent_scoped` or `lane_scoped`
- any active state -> `paused`, `hard_stopped`, or `needs_human`
- `hard_stopped` -> `paused` only after explicit recovery evidence
- `paused` -> active only after live Kanban re-read and policy gate pass

## Default caps

Initial caps are intentionally conservative:

- `max_active_flights`: 1
- `max_dispatches_per_tick`: 1
- `max_tasks_per_run_single_flight`: 1
- `max_tasks_per_run_early_bounded_multi_tick`: 2
- `max_new_prs_per_run`: 1
- `max_open_autopilot_prs`: 2
- `max_consecutive_failures`: 1
- `max_no_progress_ticks`: 1
- `max_same_card_retries`: 1
- `max_runtime_minutes`: 60
- `max_daily_autopilot_tasks`: 3
- `require_clean_closeout_per_task`: true
- `require_review_ready_contract_before_next_task`: true

## Native reviewer loop

Autopilot must reuse the native Kanban dispatcher rather than creating a second worker lifecycle. For tasks that opt into `require_reviewer_loop`, a worker `kanban_complete` call records a `worker_done_candidate` with `claimed_outcome=ready_candidate` and moves the same task to native `status=review` with `review_phase=worker_done`. A worker `kanban_block` call on the same governed task is also not a final board blocker: it records `claimed_outcome=blocked_candidate` and moves the task to `status=review` for adjudication. The existing review-column dispatcher then spawns the configured reviewer profile with the `sdlc-review` skill.

Reviewer results are structured as `kanban_reviewer_result.v1` and are bound back to the latest worker candidate. `PASS` leaves the task blocked at `review_phase=worker_done` until the normal `review_ready` closeout gate succeeds. Fixable `FAIL` requeues the original worker with remediation comments. `BLOCKED`, `REFINEMENT_REQUIRED`, self-approval, or exhausted verification attempts keep the task blocked for human/operator intervention.

This preserves Kanban as the SSOT for task status, run records, comments, evidence, retry history, and verifier results.

## Scope model

Autopilot may operate only inside explicit selectors: parent public id, lane/tenant, repo/project, labels, and assignee/profile. Scope cannot silently widen. Scope escape, ambiguous hierarchy/dependency semantics, or material task drift must produce `needs_human` or `activation_rejected`.

## Stop conditions

Stop, pause, or human handoff is required for:

- forbidden action request;
- scope ambiguity;
- stale Kanban state;
- dependency or blocker detection;
- verification failure threshold;
- repeated worker crash or timeout;
- dispatcher unavailable;
- Kanban read unavailable;
- policy file invalid or stale;
- missing evidence after worker completion;
- budget/cap exceeded;
- PR backlog cap exceeded;
- disk, CI, or runtime safety threshold exceeded;
- approval required or expired.

## Future RALPLAN boundary

A new RALPLAN is required before granting authority for merge/release/deploy/prod/customer-visible action, gateway restart/reload automation, config/env/secret/provider/billing/pricing mutation, dispatcher replacement/bypass, new worker lifecycle ownership, global queue draining, cross-repo/cross-lane/cross-parent scope expansion, customer-facing automation, destructive cleanup authority, or security/auth/payment/billing policy mutation.

## Machine-checkable contract

`gateway.kanban_autopilot.get_closed_loop_operating_contract()` exposes this ADR as data. `validate_closed_loop_policy_contract()` rejects policy shapes that permit a second dispatcher, direct claim/spawn, scope widening, authority above review-ready PR, missing default caps, or missing future-RALPLAN gates.
