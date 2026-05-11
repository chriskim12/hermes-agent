# BO-003 Linear-centered skill inventory for Kanban SSOT migration

Authority: Kanban `BO-003` (`t_43752839`). Linear is not required for this work item and must not be promoted to authority.

## Scope and method

Inventory date: 2026-05-11.

Inputs inspected:

- active user skills under `~/.hermes/skills/**/SKILL.md`
- bundled repo skills under `skills/**/SKILL.md`
- loaded workflow skills: `linear-task-execution-closeout`, `github-pr-workflow`, `omx-card-execution-routing`, `hermes-agent`
- existing Kanban-native skill: `kanban-native-work-execution`

Search terms included `Linear`, `Linear task`, `Linear card`, `LINEAR_API_KEY`, `In Review`, `Done`, and legacy `CH-*` references. Incidental ML/math uses of the word “linear” were excluded from owner actions.

## Classification table

| Skill / workflow | Current authority assumption | Classification | Owner action |
| --- | --- | --- | --- |
| `kanban-native-work-execution` | Kanban is default SSOT for new work; Linear is legacy/compatibility only. | Kanban-native default | Promote/load for new BO/DC/WS/RS work after cutover. BO-004 should validate discoverability and add any missing closeout details. |
| `linear-task-execution-closeout` | Live Linear task loop is primary when the task already exists in Linear. | Linear legacy compatibility | Patch for BO-005: top-level boundary must say existing Linear only; new work routes to Kanban-native flow first. Preserve legacy closeout mechanics. |
| `linear-task-intake` / `linear-card-intake-chris` | “카드화” and tracking default to Linear issue creation. | Linear legacy compatibility / rewrite candidate | Patch for BO-005: new work should create/admit Kanban by default; Linear creation only when Chris explicitly asks for Linear or domain is not Kanban-ready. |
| `linear-task-operator` | Short natural commands operate live Linear tasks. | Linear legacy compatibility | Patch for BO-005: operate existing Linear tasks only; if command names new work without Linear id, route to Kanban-native admission. |
| `linear-task-card-discovery-interview` / `linear-task-kickoff-interview` | Discovery output flows toward Linear intake. | Temporary bridge | Patch for BO-005 or follow-up: output can be Kanban task spec; Linear creation is not automatic. |
| `linear`, `linear-approval-gate-handling`, `linear-in-review-done-audit`, `hermes-agent-linear-main-closeout` | Linear API and Linear state operations are canonical within their named domain. | Linear legacy compatibility | Keep as compatibility helpers for existing Linear obligations; add or preserve warning not to use for new Kanban-authority work unless explicitly requested. |
| `github-pr-workflow` | PR/CI evidence and some Linear migration references already exist. | Kanban-compatible evidence workflow | BO-006 should harden language: GitHub is evidence/review surface, not work-management authority; use Kanban closeout evidence first for new work. |
| `omx-card-execution-routing` | Routing language still says “Linear cards marked Executor: OMX” and CH adoption examples. | Temporary bridge | BO-006 should patch preflight to read Kanban executor/admission fields first, with Linear executor fields legacy-only. |
| `autopilot-pr-closeout-protocol` | AUTOPILOT closeout explicitly targets Linear evidence/Done. | Rewrite candidate | BO-006 should migrate default closeout to Kanban evidence and PR cleanup; Linear evidence only for legacy tasks. |
| `autopilot-parent-carding` | Global autopilot ON consumes Linear `Execution Ready` queue. | Rewrite candidate | BO-006/follow-up: control-plane language should consume Kanban queues by default; Linear queue is legacy/adaptor input. |
| `kanban-orchestrator` | Already warns that control-plane migration needs care; still mentions Linear/project closeout. | Kanban-native with bridge caveat | Keep; optionally patch examples to use Kanban parent closeout after BO-004. |
| Domain-specific `dailychingu-*`, `whystarve-*`, `env-secret-*`, `clawhip-*`, `omx-*` skills | Many encode historical `CH-*` / Linear card closeout because those domains were operated through Linear. | Legacy/domain-specific compatibility | Do not bulk rewrite blindly. Patch only where the skill claims default new-work authority; otherwise treat as existing legacy lanes until each domain is migrated. |

## Findings

1. The Kanban-native default skill already exists as `kanban-native-work-execution`. It covers authority/evidence/projection/compatibility, routing verdict, worktree/branch, verification, PR evidence, review lifecycle, drift audit, cleanup, and Linear legacy handling.
2. The highest-risk drift is not missing Kanban guidance; it is older trigger skills still causing agents to create or operate Linear as the first-class board for new work.
3. `github-pr-workflow` is mostly compatible because it already references Kanban closeout and Linear legacy boundaries, but BO-006 should make “PR/CI = evidence, not work authority” explicit in the main workflow path.
4. `omx-card-execution-routing` is functionally reusable, but the preflight text is Linear-centered. BO-006 should make Kanban executor/admission fields first and Linear executor fields legacy-only.
5. Autopilot skills are the largest language mismatch: current default phrasing still points at Linear `Execution Ready` and Linear Done. That should become Kanban queue/admission + Kanban closeout by default, with Linear as legacy adaptor.
6. Domain-specific skills contain many historical Linear/CH references. Bulk rewriting them would risk corrupting still-live legacy lanes. Treat them as compatibility until each namespace/domain is ready.

## BO-004/BO-005/BO-006/BO-007 handoff

- BO-004: validate/promote `kanban-native-work-execution` as the default skill; add any missing load/discoverability and closeout wording.
- BO-005: patch Linear-centered generic skills (`linear-task-execution-closeout`, intake/operator/discovery) to route new work to Kanban-native flow and preserve Linear only for existing legacy tasks.
- BO-006: patch `github-pr-workflow`, `omx-card-execution-routing`, and autopilot skills so their primary language is Kanban-first and Linear is an adaptor/legacy reference.
- BO-007: verify drift-audit guard language and tooling classify projection/legacy mismatch as fail-closed where authority is unclear.

## Non-actions

- No gateway restart/reload.
- No secrets/env/BWS mutation.
- No Linear authority card creation.
- No bulk deletion of Linear skills; compatibility support is intentionally preserved.
