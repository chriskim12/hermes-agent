---
status: pending approval
target_repo: /home/ubuntu/.hermes/hermes-agent
plan_worktree: /home/ubuntu/.hermes/hermes-agent/.worktrees/plan-20260531-hermes-upstream-carry-reconciliation
canonical_path: .omh/plans/ralplan-hermes-upstream-carry-reconciliation.md
authority: planning artifact only; Kanban is execution SSOT after approval
fallback: false
created_at_utc: 2026-05-31T17:11:16Z
owner: Hayase Yuuka
execution_authority: not_admitted
admission_state: pending_approval
execution_approved: false
---

# RALPLAN — Hermes upstream update carry reconciliation

## 0. One-line objective

**Update Hermes from upstream without damaging Chrisland's execution contract: SSOT, local carry meaning, verification, approval boundaries, and final completion definition must remain intact.**

Shorter operational form:

> Accept upstream as the base, but never lose required Chrisland carry, never resurrect stale local architecture, and never collapse PR/merge/materialization/restart/live-apply into one vague “done”.

## 1. Context package

### Source request

Chris approved the following upstream-update principle and asked for this RALPLAN:

> 최신 upstream을 받아오되, Chrisland 운영 계약의 SSOT·검증·승인 경계를 깨지 않는다.

### Current baseline facts

- Target repo: `/home/ubuntu/.hermes/hermes-agent`
- Current canonical local `main` vs `upstream/main`: **upstream-only 458 / local-only 245**.
- Current upstream head used for proof: `61268ff7a feat(cli): add hermes prompt-size diagnostic (#35276)`.
- Current local main head before proof: `5ec6840db fix(kanban): distinguish reviewer remediation from duplicate PR guard (#79)`.
- Integration proof worktree: `/home/ubuntu/.hermes/hermes-agent/.worktrees/hermes-upstream-20260531-proof`
- Proof branch status: `yuuka/hermes-upstream-20260531-proof...upstream/main [ahead 4]`.
- Proof branch local commits:
  1. `e0a74358f` — test isolation: clear `HERMES_CRON_SESSION` in hermetic tests.
  2. `9750830df` — Kanban strict-ready carry adapted from `012ebdf121`: list is read-only; raw ready dispatch is gateable.
  3. `086aee68d` — BWS SSOT carry adapted from `858869d34`: Bitwarden source commands are nested under `hermes secrets source/provider ...`; deleted `hermes_cli/secrets.py` was **not** resurrected.
  4. `88fba43e9` — scoped gateway intake state carry from `3219fa2cf`.
- Proof verification already run: **15 files / 527 tests passed / 0 failed**, plus py_compile, tool schema smoke, gateway config smoke, and `git diff --check`.
- Audit artifacts:
  - `/tmp/hermes-upstream-audit-20260531-proof/summary.md`
  - `/tmp/hermes-upstream-audit-20260531-proof/summary.json`

### Code anchors already observed

These anchors matter because the RALPLAN must bind to actual code surfaces, not vague intent.

- Kanban list read-only guard: `hermes_cli/kanban.py:1399-1406` (`_cmd_list`, no `recompute_ready` mutation in list path).
- Kanban strict ready gate helpers: `hermes_cli/kanban_db.py:883-884`, `919-927`.
- Kanban recompute/dispatch ready gate usage: `hermes_cli/kanban_db.py:2692`, `2749-2753`, `5637`, `5779-5783`.
- Secrets source nesting: `hermes_cli/main.py:11575-11606` (`secrets source/provider bitwarden/bw`).
- Scoped intake state: `gateway/session.py:580`, `917`, `955`.
- Codex app-server is opt-in, not default: `hermes_cli/runtime_provider.py:263-286`, `409-413`.
- Codex MCP callback deliberately does **not** expose terminal/file mutation tools and keeps Hermes loop-only tools out: `agent/transports/hermes_tools_mcp_server.py:27-37`, `56-67`.

### Constraints / non-goals

- No upstream PR/comment/update/merge without explicit current-turn approval.
- No fork push or PR creation until Chris approves that side effect.
- No root/canonical checkout materialization until approved.
- No gateway restart/reload/live runtime apply until separately approved.
- No env/secret/config mutation as part of planning.
- No cron mutation as part of planning.
- RALPLAN is not execution authority. Kanban remains execution SSOT after approval.
- Do not reintroduce stale local surfaces merely because old commits used them.

## 2. Operating principles

1. **Upstream-first, semantics-preserving.** Use upstream/main as the substrate. Local carry is judged by behavior/authority, not by nostalgia or commit age.
2. **SSOT is a hard invariant.** Kanban lifecycle/accounting, repo policy, secret/env/config authority, gateway runtime state, and planning artifacts must not create duplicate truths.
3. **Status words must not drift.** Proof branch green, fork PR, merge, root materialization, gateway restart, and live verification are separate ledger states.
4. **Verification must fail closed.** A command that can silently no-op is not evidence. Every verification target must be explicit and observed.
5. **Policy beats similar names.** Upstream features only replace local carry when they prove the same operational meaning, not merely the same label.
6. **No stale substrate resurrection.** Preserve policy hooks and behavioral guarantees, not deleted/obsolete implementation surfaces.
7. **RALPLAN binds the full update objective.** The plan must prevent the “small PR done ⇒ whole update done” failure mode.

## 3. Decision drivers

1. **Loss prevention.** Local-only 245 commits contain real operational policy; blind upstream adoption can drop safety behavior.
2. **Architecture hygiene.** Reapplying old carry verbatim can revive stale surfaces and create duplicate lifecycle/accounting paths.
3. **Approval clarity.** Chrisland needs explicit gates for fork PR, root landing, and live runtime actions.

## 4. Viable options

### Option A — Make the current proof branch the final integration PR now

**Pros**
- Fastest visible output.
- Current proof branch is green on focused verification.

**Cons**
- Misrepresents the update as complete while most local carry remains unclassified.
- High risk of silent carry loss in Kanban/review/Codex/GWS/Hindsight/gateway areas.
- Encourages “PR exists ⇒ done” status drift.

**Verdict**: Reject as final integration. Accept only as a **partial proof PR** if clearly labeled.

### Option B — Use current proof branch as Slice 1, then continue carry reconciliation by risk-ranked slices

**Pros**
- Preserves the green proof work without overstating completion.
- Keeps high-risk carry in explicit ledger buckets.
- Allows narrow PRs with independent verification.
- Preserves approval boundaries.

**Cons**
- Slower than one broad PR.
- Requires discipline to keep the ledger current.

**Verdict**: **Chosen.** Best expected value and lowest SSOT drift risk.

### Option C — Continue adding many carry commits before any PR

**Pros**
- Might reduce PR count.
- Feels like one “complete” integration.

**Cons**
- Conflict surface expands quickly.
- Review and rollback become harder.
- A single failed area can block all already-green work.
- Encourages local architecture resurrection.

**Verdict**: Reject. Too much risk concentration.

### Option D — Drop most local carry and trust upstream

**Pros**
- Simplest codebase.
- Fewer conflicts.

**Cons**
- Violates Chrisland operating contract unless equivalence is proven.
- Silent loss of policy/runtime safeguards is likely.

**Verdict**: Reject. Upstream-first does not mean local-policy-blind.

## 5. Proposed decision / ADR

### ADR: Upstream-based cumulative carry reconciliation with explicit side-effect gates

**Decision**

Use `upstream/main` as the base and continue a cumulative proof chain, but treat the current branch as **Slice 1 only**. Do not call the upstream update complete until the local carry ledger is classified and either applied, adapted, redesigned, intentionally dropped, or explicitly deferred with a recorded owner/gate.

**Chosen option**: Option B.

**Why chosen**

- It keeps upstream architecture as the default substrate.
- It prevents local carry loss by requiring semantic classification.
- It prevents stale surface resurrection by forcing adapter/redesign decisions.
- It keeps PR/merge/materialization/restart/live-apply boundaries explicit.

**Consequences**

- Current proof branch may become a partial fork PR only after approval.
- A follow-up ledger must remain authoritative for the unresolved local carry.
- Later PRs should be risk-sliced, not bulk cherry-picks.
- Gateway restart/live apply remain out of scope unless explicitly approved later.

## 6. Conflict / semantic-drift manifest

Every later slice must explicitly cover these surfaces before claiming completion:

| Surface | Risk | Current stance |
|---|---|---|
| Repo policy / mutation guards | Agents may mutate wrong checkout or bypass authority | Preserve until upstream equivalent proven |
| Kanban execution/accounting | Duplicate lifecycle, false ready, stale reviewer handoff | Preserve policy, redesign stale autopilot surfaces |
| Gateway/session/Discord | Runtime lifeline; restart/session loss risk | Adopt upstream mechanics, keep no-restart/handoff-first policy |
| Env/secret/config SSOT | Wrong authority or raw secret drift | Preserve BWS/manifest authority; adapters only |
| Codex/OMX/runtime | OMX may duplicate upstream Codex runtime; evidence semantics may be lost | Codex app-server is target candidate; OMX freeze/legacy pending proof |
| Hindsight/memory/curator | Autonomous mutation and default behavior drift | Defer broad behavior changes until gated |
| Release/publish/tag scripts | Side-effect paths can mutate external surfaces | Inspect explicitly before final integration PR |
| Cron/scripts/channel bindings | Runtime imports may reference dropped modules | Verify imports against target root before materialization |

## 7. Local carry ledger buckets

### Already applied in Slice 1

- `e0a74358f`: test isolation for `HERMES_CRON_SESSION`.
- `9750830df` from/adapted `012ebdf121`: Kanban strict-ready/list read-only.
- `086aee68d` from/adapted `858869d34`: secrets source nesting under BWS SSOT without resurrecting deleted parser file.
- `88fba43e9` from `3219fa2cf`: gateway scoped intake state.

### Priority Slice 2 — Secrets/GWS/BWS carry

Candidate commits/features:

- `641123f730 feat(gws): resolve readonly credentials through BWS`
- `45aeb1ed9a feat(secrets): BO-170 Hermes runtime secret SSOT governance CLI (#71)`
- `2aa8384262 Clarify secrets source refresh for agents`
- duplicate/cross-project BWS smoke false-positive fixes if still applicable

Classification target:

- B / adapter if upstream has native Bitwarden mechanisms but not Chrisland GWS credential authority.
- E / preserve if upstream lacks readonly GWS-through-BWS behavior.
- F / drop only if upstream behavior proves equivalent through tests.

### Priority Slice 3 — Kanban relation/review/autopilot redesign

Candidate commits/features:

- `dcf60a7702 feat(kanban): expose relation types in tool and dashboard APIs`
- `20f2d69f75 feat(kanban): route autopilot worker candidates through review`
- `aa7baf8549 fix(kanban): simplify review package gating`
- `7be39c7df2 fix(kanban): adjudicate reviewer-loop block claims (#78)`
- `a537ed5070 fix(kanban): persist human review handoff context`
- `5ec6840db fix(kanban): distinguish reviewer remediation from duplicate PR guard (#79)`

Classification target:

- Preserve the **policy/behavior** where still required.
- Do **not** resurrect absent/stale `gateway/kanban_autopilot.py` as a substrate.
- Redesign against current upstream Kanban/session/gateway primitives.

### Priority Slice 4 — Codex runtime / OMX / evidence semantics

Candidate commits/features:

- old `codex_session` executor/evidence commits.
- bounded Codex write profile commits.
- Codex token/credential-pool resolution commits.
- OMX execution-lane guardrail commits.

Classification target:

- C / shrink or D / design choice.
- Upstream `codex_app_server` is an opt-in runtime candidate, not proof that local evidence semantics are covered.
- OMX remains frozen/legacy/special unless a separate proof shows net value over upstream Codex runtime.

### Priority Slice 5 — Hindsight / Codex OAuth bridge

Candidate commits/features:

- `972bf4db43 fix(hindsight): expose reflect evidence metadata`
- `dedca0e9d1 feat(hindsight): add Codex OAuth bridge`

Classification target:

- Defer until config/default behavior impact is isolated.
- Must not broaden autonomous memory/curator/self-improvement behavior by accident.

## 8. Implementation slices and gates

### Slice 1 — Current proof PR candidate

**Scope**

- Existing 4 proof commits only.

**Allowed after Chris approval**

- Fork branch push.
- Draft PR creation/update against Chris's fork target.

**Not allowed**

- upstream PR.
- merge.
- root/canonical materialization.
- gateway restart/reload.
- live runtime apply.
- env/secret/cron changes.

**Done when**

- PR title/body explicitly says this is partial Slice 1, not full carry reconciliation.
- PR body links this RALPLAN and `/tmp/hermes-upstream-audit-20260531-proof/summary.md`.
- PR body includes non-actions and deferred carry ledger.
- Branch SHA equals PR head SHA.

### Slice 2 — Secrets/GWS/BWS

**Scope**

- Resolve GWS readonly credentials through BWS if still needed.
- Preserve BWS manifest/config authority.
- Verify no raw secret values enter code/tests/docs.

**Verification**

- Existing Bitwarden tests plus new GWS credential-resolution tests.
- Static diff check for secret-ish values in changed files.
- CLI smoke for `hermes secrets source bitwarden status`.

### Slice 3 — Kanban relation/review/autopilot redesign

**Scope**

- Preserve relation/dashboard API if still needed.
- Preserve reviewer remediation/duplicate PR/handoff semantics if still needed.
- Use current upstream session/Kanban primitives, not stale `gateway/kanban_autopilot.py` resurrection.

**Verification**

- Kanban DB/CLI tests.
- Gateway/session tests if session state is touched.
- A reviewer-loop scenario test that proves blocked/review/remediation states do not collapse.

### Slice 4 — Codex runtime / OMX decision

**Scope**

- Compare upstream `codex_app_server` runtime against old local `codex_session` value.
- Preserve evidence semantics only if not upstream-native.
- OMX remains freeze/legacy/special unless proof shows otherwise.

**Verification**

- Codex app-server runtime/session tests.
- Evidence schema tests.
- No OMX path becomes default execution lane without explicit approval.

### Slice 5 — Hindsight/OAuth bridge

**Scope**

- Reflect evidence metadata and Codex OAuth bridge.
- Isolate config/default behavior changes.

**Verification**

- Hindsight provider tests.
- Config migration/precedence tests.
- Explicit no-autonomous-mutation gate.

### Slice 6 — Final materialization plan

**Scope**

- Only after all required carry is classified.
- Fork/main and canonical root landing relation must be explicit.

**Separate approvals required**

- fork push/PR.
- fork merge.
- root canonical checkout materialization.
- gateway restart/reload.
- live runtime verification.

## 9. Verification plan

Minimum verification before each PR/update:

```bash
git status --short --branch
git diff --check
/home/ubuntu/.hermes/hermes-agent/venv/bin/python -m py_compile <explicit changed runtime files>
./scripts/run_tests.sh <explicit focused test files>
```

Integration smoke for any cumulative proof branch:

```bash
/home/ubuntu/.hermes/hermes-agent/venv/bin/python - <<'PY'
from model_tools import get_tool_definitions
schemas = get_tool_definitions(quiet_mode=True)
names = [s['function']['name'] for s in schemas]
print('tool_schema_count', len(names))
print('has_terminal', 'terminal' in names)
print('has_tts', 'text_to_speech' in names)
print('duplicates', sorted({n for n in names if names.count(n) > 1}))
from gateway.config import load_gateway_config
cfg = load_gateway_config()
print('gateway_config_loaded', type(cfg).__name__, bool(getattr(cfg, 'platforms', None)))
PY
```

Runtime binding integrity before root/canonical materialization:

```bash
python - <<'PY'
# Fail closed: explicit roots only; no shell globs that can silently no-op.
from pathlib import Path
roots = [Path.home()/'.hermes/scripts']
missing = []
for root in roots:
    if not root.exists():
        continue
    for path in root.glob('*.py'):
        text = path.read_text(encoding='utf-8', errors='ignore')
        for token in ('tools.', 'agent.', 'gateway.', 'plugins.', 'hermes_cli.'):
            if token in text:
                print('runtime_import_candidate', path)
print('runtime_import_scan_complete')
PY
```

Important: scans must print explicit targets or explicit “not present”/“complete” lines. A blank scan is not acceptable proof.

## 10. Pre-mortem

1. **False completion**
   - Scenario: Slice 1 PR is created and treated as the full upstream update.
   - Mitigation: PR/body/RALPLAN must state partial scope and unresolved carry ledger.

2. **Silent local carry loss**
   - Scenario: Required GWS/BWS or Kanban reviewer semantics are dropped because upstream has similarly named functionality.
   - Mitigation: semantic carry ledger and per-slice classification gates.

3. **Stale local substrate resurrection**
   - Scenario: old `gateway/kanban_autopilot.py` or codex_session mechanics are revived to make cherry-picks easy.
   - Mitigation: adapter/redesign requirement; stale-surface resurrection is a plan violation.

4. **Live/runtime overreach**
   - Scenario: fork PR/merge gets conflated with gateway restart/live apply.
   - Mitigation: separate approval boundaries recorded in every slice.

5. **Verification no-op**
   - Scenario: test/scan commands pass without touching intended files.
   - Mitigation: explicit path lists and fail-closed smoke checks.

## 11. Rollback / cleanup plan

- If a slice fails verification, do not widen the slice. Revert or amend the slice branch before moving on.
- If a cherry-pick pulls stale surfaces, abort or manually re-implement only the policy adapter.
- If Slice 1 PR is created, it must remain draft/partial until Chris approves next gate.
- Plan worktree cleanup is deferred until the plan is preserved/promoted or superseded. Before final program closeout, either commit/link this plan as canonical evidence or remove the registered plan worktree with `git worktree remove` + `git worktree prune` after preservation.

## 12. Architect review

### Architectural soundness

#### ✅ What fits well

- The plan uses upstream/main as the substrate rather than merging upstream into local history. This fits the existing proof branch shape and avoids preserving local workarounds by inertia.
- Slice 1 already demonstrates the adapter pattern: secrets carry was adapted into current `hermes_cli/main.py:11575-11606` instead of resurrecting deleted `hermes_cli/secrets.py`.
- Kanban policy preservation is anchored to current functions: list read-only behavior at `hermes_cli/kanban.py:1399-1406`, strict-ready controls in `hermes_cli/kanban_db.py:883-884`, `919-927`, and dispatch/recompute paths at `2749-2753`, `5779-5783`.
- Codex/OMX stance is compatible with upstream code: `codex_app_server` is opt-in in `hermes_cli/runtime_provider.py:263-286`, not a default migration, and the MCP bridge intentionally excludes terminal/file mutation tools in `agent/transports/hermes_tools_mcp_server.py:27-37`, `56-67`.
- The plan preserves approval boundaries rather than letting PR creation imply root materialization or gateway restart.

#### ⚠️ Concerns

- The remaining Kanban reviewer/autopilot area is broad. If Slice 3 is not split further after read-only inspection, it can become a second mega-merge.
- The runtime import scan in the verification plan is intentionally a skeleton; before materialization it must be expanded into an actual import-resolution checker, not just a candidate printer.
- The current plan uses `/tmp` audit artifacts as references; before final closeout, durable evidence should be linked in Kanban or committed/PR-linked so `/tmp` volatility does not become a stale pointer.

### Steelman antithesis

> “This plan still risks becoming process theater. It acknowledges 245 local commits but only applies four; unless the unresolved ledger becomes a machine-checkable gate, a future agent can still create a PR, say ‘deferred’, and leave the important Chrisland behavior behind. The strongest technical concern is that the plan’s big buckets—especially Kanban/autopilot and Codex/OMX—are still too wide to enforce without additional per-surface manifests.”

Assessment: valid concern. The plan addresses it by making Slice 1 explicitly partial and requiring later classification, but execution must preserve that discipline. The follow-up carry ledger should be materialized as a checklist in PR/Kanban, not only prose.

### Tradeoff tensions

#### T1: Early partial PR vs wait for full reconciliation

- **Gain**: Early PR preserves green proof and reduces review size.
- **Loss**: Higher risk readers misread it as the full update.
- **Assessment**: Worth it only if PR title/body and Kanban state explicitly say partial Slice 1.

#### T2: Upstream-first vs local policy preservation

- **Gain**: Cleaner architecture and less stale code.
- **Loss**: More work to prove local policy equivalence.
- **Assessment**: Worth it. Local policy cannot be dropped by similar naming.

#### T3: Adapter/redesign vs cherry-pick speed

- **Gain**: Avoids stale substrate resurrection.
- **Loss**: More manual analysis per carry.
- **Assessment**: Required for Kanban/autopilot and Codex areas.

### Risk matrix

| Risk | Likelihood | Impact | Mitigation |
|---|---:|---:|---|
| Slice 1 treated as full update | Medium | High | Partial PR wording, RALPLAN link, deferred ledger |
| Required carry silently dropped | Medium | High | Per-slice semantic classification and verification |
| Stale local architecture resurrected | Medium | High | Adapter/redesign rule; no deleted surface revival |
| Runtime import/config break after root materialization | Medium | High | Runtime binding integrity check before materialization |
| Plan artifact becomes stale SSOT | Low | Medium | Kanban remains execution SSOT; plan cleanup/preservation required |

### Architect verdict

**APPROVE with provisos.**

Provisos to fold into execution:

- A1: Slice 1 PR must be explicitly partial.
- A2: Slice 3 must be broken down after read-only inspection; do not make it one broad autopilot PR.
- A3: Before root materialization, replace candidate-only runtime import scan with import-resolution proof.

These provisos are included in the final plan above.

## 13. Critic review

### Principle-option consistency

The chosen option is consistent with the principles. Option B is the only option that both honors upstream-first architecture and preserves local policy meaning.

### Fair alternatives

The rejected options are fairly represented:

- Option A is valid as a partial PR but invalid as final integration.
- Option C is tempting but concentrates risk.
- Option D is clean but violates loss-prevention.

### Risk mitigation quality

The plan handles the largest known risks: false completion, stale surface resurrection, approval boundary collapse, and verification no-op. The main remaining weakness is that follow-up ledger enforcement is still procedural until implemented in Kanban/PR checklists.

### Testable acceptance criteria

Acceptance criteria are testable for Slice 1. Later slices need their own concrete test lists after read-only context passes.

### Simplicity / deletion opportunities

The plan correctly prefers deletion/drop where upstream proves equivalence, and rejects preserving old implementation surfaces. It should continue to look for obsolete local carry to drop rather than trying to honor every local commit.

### Critic verdict

**APPROVE.**

Required caveat: consensus completion does **not** approve execution, fork push, PR creation, merge, materialization, restart, or live apply.

## 14. Consensus handoff record

```yaml
planning_artifact: /home/ubuntu/.hermes/hermes-agent/.worktrees/plan-20260531-hermes-upstream-carry-reconciliation/.omh/plans/ralplan-hermes-upstream-carry-reconciliation.md
final_planner_plan: this document
ralplan_architect_review:
  reviewer: Hayase Yuuka / architect pass
  verdict: APPROVE_WITH_PROVISOS
  required_revisions_folded: true
ralplan_critic_review:
  reviewer: Hayase Yuuka / critic pass
  verdict: APPROVE
ralplan_consensus_gate:
  complete: true
  sequence: planner -> architect -> revised/folded -> critic
consensus_complete: true
admission_ready: true
admission_state: pending_approval
execution_authority: not_admitted
execution_approved: false
approval_boundary:
  currently_approved: planning artifact only
  excluded_without_separate_approval:
    - fork push
    - PR creation/update
    - upstream PR/comment/update
    - merge
    - root/canonical materialization
    - gateway restart/reload
    - live runtime apply
    - env/secret/config mutation
    - cron mutation
```

## 15. Chris decision request

Recommended approval wording if Chris accepts this plan:

> “Approve this RALPLAN for admission. Proceed with Slice 1 fork push + draft PR only, with no merge, no root materialization, no gateway restart/reload, no live apply, and no env/secret/cron changes.”

If Chris wants to continue without PR first, alternate approval wording:

> “Approve this RALPLAN. Continue Slice 2 carry reconciliation locally only; no push/PR/merge/materialization/restart/live apply.”


## 16. Execution progress record — 2026-06-01 local-only Slice 2 advance

Recorded at: `2026-05-31T17:42:49+00:00`

### Scope actually advanced

Chris approved continuing as far as possible while preserving the RALPLAN safety boundary. I treated this as approval for **local-only execution and verification**, not as merge/live/restart/env/cron authority.

New local-only cumulative proof worktree:

- Worktree: `/home/ubuntu/.hermes/hermes-agent/.worktrees/hermes-upstream-20260601-slice2-local`
- Branch: `yuuka/hermes-upstream-20260601-slice2-local`
- Base: current `upstream/main` `1044d9f25 fix(gateway): /stop can interrupt a sibling participant's run in a per-user thread (#35959)`
- Status: clean; `ahead 7` of upstream/main
- Side effects: no push, no PR update, no merge, no root materialization, no gateway restart/reload, no live runtime apply, no env/secret/config mutation, no cron mutation.

### Commits in local Slice 2 branch

1. `f26961b25` — test isolation for `HERMES_CRON_SESSION`.
2. `c5ee7d1f4` — Kanban strict-ready/list-read-only carry, rebased onto latest upstream.
3. `242967846` — Bitwarden source/provider nesting carry.
4. `ba666179a` — gateway scoped intake state carry.
5. `bf058f931` — GWS read-only credential resolution through BWS, adapted by restoring the Google Workspace read-only toolset and explicit `google_workspace` toolset wiring on latest upstream.
6. `26b6e9f63` — BWS runtime secret SSOT governance CLI, adapted into the existing `hermes secrets source/provider bitwarden` parser instead of creating a duplicate top-level `secrets` parser.
7. `3b32da02c` — secrets source refresh wording/alias carry; `refresh` is canonical and `sync` remains an upstream-compatible alias for source refresh.

### Slice 2 classification outcome

| Carry | Verdict | Decision |
|---|---|---|
| `641123f730` GWS read-only credentials through BWS | B / adapter | Preserved as local read-only GWS toolset because upstream lacks equivalent `tools/google_workspace_tool.py`; no raw credentials in tests. |
| `45aeb1ed9a` runtime secret SSOT governance CLI | B / adapter | Preserved as governance commands under the existing `hermes secrets` tree; avoided duplicate parser and kept BWS as SSOT. |
| `2aa8384262` source refresh clarification | B / adapter | Preserved with `hermes secrets source bitwarden refresh` canonical and `sync` as compatibility alias. |

### Slice 3 classification outcome

- Relation metadata: B / adapter — preserve API/tool/dashboard relation metadata, not legacy controller state.
- Worker-through-review / review-package gating / reviewer-loop block claims / handoff context / remediation-vs-duplicate guard: B / adapter — preserve policy against current Kanban DB/session primitives.
- `gateway/kanban_autopilot.py` and old continuous controller substrate: F / do not resurrect.
- Next Slice 3 must split relation metadata, reviewer remediation, human handoff context, and legacy autopilot cleanup; do not make it one mega-merge.

### Slice 4/5 classification outcome

- Upstream `codex_app_server` runtime/session is native and should be adopted as substrate.
- Old local `codex_session` value is not fully upstream-native: preserve only as a narrow evidence-only shim or migration bridge, not default runtime.
- OMX stays frozen/legacy/special; do not promote as default execution lane.
- Hindsight reflect metadata and Codex OAuth bridge are local-only: preserve only as opt-in, loopback/secret-gated, non-default behavior until separate proof.

### Verification run on local Slice 2 branch

Commands/results:

```bash
python -m py_compile hermes_cli/main.py hermes_cli/secrets.py hermes_cli/secrets_cli.py toolsets.py tools/google_workspace_tool.py gateway/session.py hermes_cli/kanban.py hermes_cli/kanban_db.py
./scripts/run_tests.sh tests/hermes_cli/test_secrets_source_wiring.py tests/hermes_cli/test_secrets_cli.py tests/tools/test_google_workspace_tool.py
# 3 files / 44 tests passed / 0 failed

git diff --check
./scripts/run_tests.sh tests/tools/test_terminal_task_cwd.py tests/tools/test_code_execution_modes.py tests/tools/test_terminal_tool.py tests/agent/test_skill_commands.py tests/hermes_cli/test_config_env_refs.py tests/gateway/test_config_env_bridge_authority.py tests/hermes_cli/test_secrets_source_wiring.py tests/hermes_cli/test_secrets_cli.py tests/tools/test_google_workspace_tool.py tests/gateway/test_intake_state.py tests/hermes_cli/test_kanban_db.py tests/hermes_cli/test_kanban_cli.py tests/gateway/test_session_work_state.py tests/gateway/test_session_handoff.py tests/kanban/test_ready_gate.py
# 12 discovered files / 410 tests passed / 0 failed

python - <<'PY'
from model_tools import get_tool_definitions
schemas = get_tool_definitions(quiet_mode=True)
names = [s['function']['name'] for s in schemas]
print('tool_schema_count', len(names))
print('has_terminal', 'terminal' in names)
print('has_tts', 'text_to_speech' in names)
print('has_google_workspace_profiles', 'google_workspace_profiles' in names)
print('duplicates', sorted({x for x in names if names.count(x) > 1}))
from gateway.config import load_gateway_config
cfg = load_gateway_config()
print('gateway_config_loaded', type(cfg).__name__, bool(getattr(cfg, 'platforms', None)))
PY
# tool_schema_count 35; terminal=True; tts=True; google_workspace_profiles=True; duplicates=[]; GatewayConfig True
```

### Current completion state

- Slice 1 draft PR remains a previously verified partial proof anchor; it was **not** updated in this pass because fork PR update is a separate side effect.
- Local Slice 2 branch is verified on latest upstream and ready to become the next draft/update candidate if Chris approves fork push/PR update.
- RALPLAN still does **not** authorize merge, root materialization, gateway restart/reload, live apply, env/secret/config mutation, or cron mutation.
