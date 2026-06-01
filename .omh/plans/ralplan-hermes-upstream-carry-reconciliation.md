---
status: pending approval
admission_ready: true
review_ready: true
target_repo: /home/ubuntu/.hermes/hermes-agent
plan_worktree: /home/ubuntu/.hermes/hermes-agent/.worktrees/plan-20260531-hermes-upstream-carry-reconciliation
canonical_path: .omh/plans/ralplan-hermes-upstream-carry-reconciliation.md
authority: planning artifact only; Kanban is execution SSOT after approval
fallback: false
created_at_utc: 2026-05-31T17:11:16Z
owner: Hayase Yuuka
execution_authority: not_admitted
admission_state: admission_ready_pending_approval
execution_approved: false
latest_proof_pr: https://github.com/chriskim12/hermes-agent/pull/81
latest_proof_head: 6bb84afa58808ed66ea0f325fbca1579c4691a36
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

Classification outcome:

- **Drop both local carries by Chris decision.**
- Keep upstream/Hermes-native Hindsight provider and Codex OAuth/provider flows as the authority.
- Do not port local reflect evidence metadata or local Hindsight Codex OAuth bridge into the upstream integration proof branch.
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

### Slice 5 — Hindsight/OAuth local carry drop

**Scope**

- No integration work for local `972bf4db43` reflect evidence metadata.
- No integration work for local `dedca0e9d1` Hindsight Codex OAuth bridge.
- Keep upstream/Hermes-native Hindsight provider and Codex OAuth/provider flows as the authority.

**Verification**

- Verify the cumulative proof branch does not include `hermes_cli/hindsight_codex_bridge.py` or local Hindsight reflect evidence metadata changes.
- Verify no config/default behavior is changed for Hindsight, OAuth, memory, curator, or self-improvement flows.

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

- Codex runtime / evidence semantics: **B / adapter** — adopt upstream-native `codex_app_server` / Codex app-server session substrate and preserve local evidence semantics only through the current upstream-compatible projector/session test surface. Do not resurrect old `codex_session` as default runtime.
- Old local `codex_session` executor/tooling commits: **drop as implementation substrate; preserve only semantic coverage through current Codex app-server tests**.
- OMX/Ralph lane carries: **defer / legacy-special** — keep OMX frozen as a special lane; do not promote OMX as default execution route in this upstream reconciliation proof.
- Hindsight reflect metadata and Hindsight Codex OAuth bridge: **drop by Chris decision** — keep Hermes-native Hindsight provider and Codex OAuth/provider flows as authority; do not port `972bf4db43` or `dedca0e9d1`.
- Upstream advanced caveat: newer upstream runtime capability does not imply live/default migration. Runtime default changes, root materialization, and gateway/runtime apply remain separate approval gates.

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

## 15. Slice 3 local proof — Kanban relation metadata (2026-06-01)

Status: **local proof complete; not pushed; not merged; no live apply**.

### Scope fixed

Slice 3 was narrowed to the low-risk Kanban relation metadata carry before touching review/autopilot/handoff behavior:

- `a8dd263639 fix: distinguish kanban hierarchy links from dependencies`
- `dcf60a7702 feat(kanban): expose relation types in tool and dashboard APIs`

### Classification

- **Preserve / adapter**: relation metadata is still required because parent-child links have two distinct meanings:
  - `dependency` gates child readiness and dispatch.
  - `hierarchy` records umbrella/epic structure and must not block executable child work.
- This is not stale UI decoration. It protects Kanban SSOT from status-word drift where “parent” silently means either blocker or grouping depending on context.
- No legacy `gateway/kanban_autopilot.py` substrate was resurrected. Review/autopilot/handoff commits remain separate later slices.

### Implementation result

Applied on local Slice 2 worktree branch `yuuka/hermes-upstream-20260601-slice2-local` after conflict resolution:

- `49450e453 fix: distinguish kanban hierarchy links from dependencies`
- `368ae98f1 feat(kanban): expose relation types in tool and dashboard APIs`

The Slice 3 local branch now has **9 commits ahead of latest `upstream/main` base `1044d9f25`**.

### Verification

- `python -m py_compile hermes_cli/kanban.py hermes_cli/kanban_db.py plugins/kanban/dashboard/plugin_api.py tools/kanban_tools.py` — passed.
- `git diff --check` — passed.
- Focused relation/API bundle:
  - `./scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/plugins/test_kanban_dashboard_plugin.py tests/tools/test_kanban_tools.py`
  - **3 files / 389 tests passed / 0 failed**.
- Broader integration bundle:
  - `./scripts/run_tests.sh tests/tools/test_terminal_task_cwd.py tests/tools/test_code_execution_modes.py tests/tools/test_terminal_tool.py tests/agent/test_skill_commands.py tests/hermes_cli/test_config_env_refs.py tests/gateway/test_config_env_bridge_authority.py tests/hermes_cli/test_kanban_db.py tests/plugins/test_kanban_dashboard_plugin.py tests/tools/test_kanban_tools.py`
  - **9 files / 494 tests passed / 0 failed**.

### Non-actions

- No push.
- No PR update/new PR.
- No merge to root `main`.
- No root checkout materialization.
- No gateway restart/reload.
- No live runtime apply.
- No env/secret/config mutation.
- No cron mutation.

### Remaining Slice 3 work

Still unresolved and should not be implied by this proof:

- review package simplification / reviewer remediation semantics.
- human review handoff context persistence.
- autopilot worker-candidate review routing.
- duplicate PR guard vs reviewer remediation distinction.

Those are higher-risk lifecycle/accounting behaviors and should be handled as separate sub-slices rather than bundled into the relation metadata proof.

## 16. Slice 3b local proof — reviewer remediation vs duplicate PR guard (2026-06-01)

Status: **local proof complete; not pushed; not merged; no live apply**.

### Scope fixed

Chris approved the recommendation to split the next work at `reviewer remediation vs duplicate PR guard`. During implementation, the old local carry was found to depend on broader review-loop/autopilot substrate (`gateway/kanban_autopilot.py`, full reviewer-loop transitions) that should not be resurrected wholesale on the upstream-based branch.

This sub-slice therefore preserved only the narrow kernel invariant:

- A normal ready task with a recent PR comment remains `active_pr` guarded to prevent duplicate PR creation.
- A task explicitly marked as reviewer-FAIL remediation may respawn even with an active PR comment, because the worker must update the existing PR rather than create a duplicate.
- The worker context must clearly say remediation mode and list existing PR target(s).

### Classification

- **Preserve / adapter** for the kernel guard and worker-context warning.
- **Do not resurrect** the old `gateway/kanban_autopilot.py` substrate in this slice.
- Full review-loop/autopilot routing remains a later design slice.

### Implementation result

Applied on local Slice 3 branch `yuuka/hermes-upstream-20260601-slice2-local`:

- `00841587b fix(kanban): allow reviewer remediation with active PR`

The cumulative local proof branch now has **10 commits ahead of latest `upstream/main` base `1044d9f25`**.

### TDD / verification

RED was observed first:

- `python -m pytest tests/hermes_cli/test_kanban_db.py::test_dispatch_allows_reviewer_fail_remediation_with_existing_pr -q` initially failed because the upstream-based branch did not yet have `closeout_evidence` task state / remediation handling.

GREEN / focused verification:

- `python -m py_compile hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_db.py` — passed.
- Specific regression: `python -m pytest tests/hermes_cli/test_kanban_db.py::test_dispatch_allows_reviewer_fail_remediation_with_existing_pr -q` — **1 passed**.
- DB focused file: `./scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py` — **213 tests passed / 0 failed**.
- Broader focused bundle:
  - `./scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/plugins/test_kanban_dashboard_plugin.py tests/tools/test_kanban_tools.py tests/tools/test_terminal_task_cwd.py tests/tools/test_code_execution_modes.py tests/tools/test_terminal_tool.py tests/agent/test_skill_commands.py tests/hermes_cli/test_config_env_refs.py tests/gateway/test_config_env_bridge_authority.py`
  - **9 files / 495 tests passed / 0 failed**.
- `git diff --check` — passed before commit.

### Non-actions

- No push.
- No PR update/new PR.
- No merge to root `main`.
- No root checkout materialization.
- No gateway restart/reload.
- No live runtime apply.
- No env/secret/config mutation.
- No cron mutation.

### Remaining Slice 3 work

Still unresolved and must not be implied by this proof:

- review package simplification / closeout gating.
- human review handoff context persistence.
- autopilot worker-candidate review routing.
- full reviewer-loop lifecycle/adjudication.

Recommended next sub-slice: review package simplification / closeout gating, because it is the next smallest kernel-side behavior before touching gateway/autopilot routing.

## 17. Slice 3c local proof — review package simplification / closeout gating (2026-06-01)

Status: **local proof complete; not pushed; not merged; no live apply**.

### Scope fixed

This sub-slice preserved the closeout/review package kernel required before any broader Autopilot routing can be trusted:

- `worker_done` records executor completion but does **not** mean final Done.
- `review_ready` requires a fail-closed closeout verifier result with worker evidence, verifier PASS, boundaries confirmation, PR/check evidence or a strict no-PR exception, residue accounting, and cleanup proof.
- Raw `complete_task` cannot bypass an already-governed review task into board `done`.
- `kanban_complete` can submit a structured `closeout_evidence` package and the kernel attempts `worker_done -> review_ready` only through the verifier.
- CLI `/kanban closeout ... --check-only --json` can inspect the gate without mutating the task.

### Classification

- **Preserve / adapter** for closeout package gating and worker_done/review_ready/closed phase separation.
- **Do not resurrect** old continuous `gateway/kanban_autopilot.py` substrate.
- This is the output-side verifier kernel needed before later `autopilot worker-candidate review routing` can safely run.

### Implementation result

Applied on local cumulative branch `yuuka/hermes-upstream-20260601-slice2-local`:

- `b94fdb4d0 fix(kanban): gate review package closeout`

The cumulative local proof branch is now **ahead 11** of latest `upstream/main` base `1044d9f25`.

### TDD / verification

RED was observed first:

- A new closeout verifier regression initially failed because the upstream-based branch lacked the local `hermes_cli/kanban_closeout.py` closeout gate module and then lacked the DB authority helpers expected by the review package tests.

GREEN / focused verification:

- `python -m py_compile hermes_cli/kanban.py hermes_cli/kanban_db.py hermes_cli/kanban_closeout.py tools/kanban_tools.py tests/hermes_cli/test_kanban_closeout.py tests/tools/test_kanban_tools.py` — passed.
- Closeout/tool focused bundle:
  - `./scripts/run_tests.sh tests/hermes_cli/test_kanban_closeout.py tests/tools/test_kanban_tools.py`
  - **2 files / 127 tests passed / 0 failed**.
- Broader Kanban/review integration bundle:
  - `git diff --check && ./scripts/run_tests.sh tests/hermes_cli/test_kanban_closeout.py tests/tools/test_kanban_tools.py tests/hermes_cli/test_kanban_db.py tests/plugins/test_kanban_dashboard_plugin.py tests/tools/test_kanban_tools.py tests/tools/test_terminal_task_cwd.py tests/tools/test_code_execution_modes.py tests/tools/test_terminal_tool.py tests/agent/test_skill_commands.py tests/hermes_cli/test_config_env_refs.py tests/gateway/test_config_env_bridge_authority.py`
  - **10 files / 540 tests passed / 0 failed**.

### Non-actions

- No push.
- No PR update/new PR.
- No merge to root `main`.
- No root checkout materialization.
- No gateway restart/reload.
- No live runtime apply.
- No env/secret/config mutation.
- No cron mutation.

### Remaining Slice 3 work

Still unresolved and must not be implied by this proof:

- human review handoff context persistence.
- autopilot worker-candidate review routing.
- full reviewer-loop lifecycle/adjudication.
- end-to-end bounded `/autopilot` smoke after the evaluator/routing substrate is materialized.

Recommended next sub-slice: **human review handoff context persistence**. The closeout gate now defines what counts as reviewable output; next the review/handoff context needs to persist enough detail for a reviewer or remediation worker to act without rebuilding state from scattered comments.
## 18. Draft PR anchor + packaging-smoke repair (2026-06-01)

Status: **draft PR anchor created; hosted CI green; not merged; no live apply**.

### Scope fixed

Chris approved leaving a PR anchor for the completed RALPLAN proof slices. The cumulative Slice 2/3/3b/3c branch was pushed to the fork and opened as a draft PR:

- PR: https://github.com/chriskim12/hermes-agent/pull/81
- Base: `yuuka/upstream-main-20260601-base`
- Base SHA: `1044d9f25d63b48c51fe40af0a4cfeea3b6de516`
- Head branch: `yuuka/hermes-upstream-20260601-slice2-local`
- Final head SHA: `6bb84afa58808ed66ea0f325fbca1579c4691a36`
- PR state: `OPEN`, `draft: true`, `mergeStateStatus: CLEAN`, `mergeable: MERGEABLE`

The first PR head `b94fdb4d09b7e7c0cb0b49a830d3918b28c7f2c0` exposed a hosted Nix packaging smoke failure rather than a local focused-test failure. The Nix `hermes-cli-commands` check failed with:

```text
FAIL: gateway subcommand missing
```

Root cause: `hermes_cli/kanban_closeout.py` imported `hermes_cli.kanban_drift_audit`, but the source file was missing from the upstream-based proof branch. The local source checkout could hide this through stale `__pycache__`, while a clean wheel/Nix package could not.

### Implementation result

Applied on cumulative branch `yuuka/hermes-upstream-20260601-slice2-local`:

- `6bb84afa5 fix(kanban): include drift audit helper for closeout packaging`

This added:

- `hermes_cli/kanban_drift_audit.py`
- `tests/hermes_cli/test_kanban_closeout_packaging.py`

### Verification

Local repair verification:

- `git diff --check` — passed.
- `python -m py_compile hermes_cli/kanban_drift_audit.py hermes_cli/kanban_closeout.py hermes_cli/kanban.py hermes_cli/main.py` — passed.
- Clean-pycache import smoke using `PYTHONPYCACHEPREFIX=/tmp/hermes-pr81-clean-pycache` — passed.
- `python -m hermes_cli.main --help | grep -q gateway` — passed.
- Fresh pip package smoke in `/tmp/hermes-pr81-pip-smoke`: install `.[all]`, run packaged `hermes --help`, require `gateway` and `config` — passed.
- Focused closeout package bundle:
  - `./scripts/run_tests.sh tests/hermes_cli/test_kanban_closeout_packaging.py tests/hermes_cli/test_kanban_closeout.py tests/tools/test_kanban_tools.py`
  - **3 files / 128 tests passed / 0 failed**.
- Broader focused integration bundle:
  - `git diff --check && ./scripts/run_tests.sh tests/hermes_cli/test_kanban_closeout_packaging.py tests/hermes_cli/test_kanban_closeout.py tests/tools/test_kanban_tools.py tests/hermes_cli/test_kanban_db.py tests/plugins/test_kanban_dashboard_plugin.py tests/tools/test_terminal_task_cwd.py tests/tools/test_code_execution_modes.py tests/tools/test_terminal_tool.py tests/agent/test_skill_commands.py tests/hermes_cli/test_config_env_refs.py tests/gateway/test_config_env_bridge_authority.py`
  - **11 files / 541 tests passed / 0 failed**.

Hosted PR CI at final head `6bb84afa58808ed66ea0f325fbca1579c4691a36`:

- `nix (ubuntu-latest)` — success.
- `nix (macos-latest)` — success.
- `changes` — success.
- `Scan PR for critical supply chain risks` — success/skipped matrix entries.
- `Check PyPI dependency upper bounds` — success/skipped matrix entries.

### Non-actions

- No merge.
- No root/canonical checkout materialization.
- No gateway restart/reload.
- No live runtime apply.
- No env/secret/config mutation.
- No cron mutation.
- No upstream PR.

### Remaining work classification

The PR is now the reviewable/admission-ready anchor for the verified reconciliation slices, not root/live completion.

- Human review handoff context persistence: **preserve/adapt** through the current `worker_done -> closeout verifier -> review_ready` package path; no separate `gateway/kanban_autopilot.py` resurrection.
- Autopilot worker-candidate review routing: **defer** as a separate lifecycle feature; current closeout correctness is covered by the fail-closed closeout verifier.
- Full reviewer-loop lifecycle/adjudication: **defer**; too broad for this proof and risks duplicating the current closeout gate.
- Codex runtime/evidence: **adapted** to upstream Codex app-server/session/projector substrate; old local `codex_session` implementation is not admitted as default runtime.
- OMX/Ralph: **defer / legacy-special**; not a default lane.
- Hindsight/OAuth bridge carries: **drop**.
- Final root materialization / runtime apply / gateway restart gates remain separate approvals.

## 19. Final review-ready / admission-ready closeout (2026-06-01)

### 결론

This RALPLAN is **review-ready / admission-ready pending approval** for the current fork-local proof anchor. It is **not** merge approval, root materialization approval, gateway restart approval, or live runtime approval.

### 실제 반영

- Latest proof PR: https://github.com/chriskim12/hermes-agent/pull/81
- Proof branch: `yuuka/hermes-upstream-20260601-slice2-local`
- Proof head: `6bb84afa58808ed66ea0f325fbca1579c4691a36`
- Base anchor: `yuuka/upstream-main-20260601-base` at `1044d9f25d63b48c51fe40af0a4cfeea3b6de516`
- Verified preserved/adapted carries:
  - test isolation for `HERMES_CRON_SESSION`
  - Kanban strict-ready/list-read-only semantics
  - Bitwarden/GWS/BWS read-only and runtime secret-governance adapters
  - gateway scoped intake state
  - Kanban hierarchy-vs-dependency relation metadata
  - reviewer remediation vs duplicate PR guard
  - fail-closed review package closeout gating
  - closeout packaging helper required by clean package/Nix smoke
- Classified remaining carries:
  - human review handoff context: **preserve/adapt** through current closeout evidence path
  - autopilot worker-candidate review routing: **defer**
  - full reviewer-loop lifecycle/adjudication: **defer**
  - Codex runtime/evidence: **adapt** to upstream Codex app-server/session/projector substrate
  - old local `codex_session` implementation substrate: **drop as default/runtime substrate**
  - OMX/Ralph lanes: **defer / legacy-special**, not default execution route
  - Hindsight reflect metadata and Hindsight Codex OAuth bridge: **drop** by Chris decision

### 아직 안 한 것

- No upstream/NouResearch PR, comment, push, or update.
- No merge into fork/main or canonical `main`.
- No root/canonical checkout materialization.
- No gateway restart/reload.
- No live runtime apply.
- No env/secret/provider/config mutation.
- No cron mutation.
- No customer-facing/external send.
- No cost-bearing action.

### 다음 판단

Chris can review the proof PR and this RALPLAN as an admission-ready package. The next decisions remain separate:

1. whether to merge the fork-local proof PR;
2. whether/when to materialize the canonical root checkout;
3. whether/when to restart/reload the gateway;
4. whether/when to run live/runtime verification;
5. whether deferred Autopilot reviewer-loop / OMX lanes should become separate future work.

### Policy check

- RALPLAN remains a planning/admission artifact.
- Kanban remains execution SSOT after approval.
- PR existence is evidence, not merge/root/live authority.
- `worker_done`, `review_ready`, `closed`, PR merged, root materialized, and runtime applied remain distinct ledger states.
- Hindsight/memory is not promoted above repo/Kanban/PR truth.

### Green 완료

- PR #81 exists as a draft/open review anchor.
- PR #81 hosted checks are green at head `6bb84afa58808ed66ea0f325fbca1579c4691a36`.
- All remaining carry buckets have an explicit preserve/adapt/drop/defer classification.
- Hindsight/OAuth local carries are explicitly dropped.
- OMX is explicitly not promoted to default.
- Closeout verifier path is tested and packaged.

### Yellow 대기

- `upstream/main` has advanced after the PR #81 base anchor. This does not invalidate the current proof package, but a future merge/materialization decision should either retain the anchored base intentionally or refresh/rebase in a new proof pass.
- Deferred Autopilot reviewer-loop routing and OMX retirement/migration remain future separately-scoped work, not blockers for this admission-ready closeout.
- Runtime import/config binding verification before root materialization remains required if Chris later approves root landing.

### Red 필요

None inside the approved review-ready/admission-ready scope. Red gates only appear if someone tries to proceed to upstream mutation, merge, root materialization, gateway restart, live apply, env/secret mutation, cron mutation, customer-facing send, or cost-bearing work without separate approval.

### 검증

Latest closeout verification on proof branch:

```bash
python -m py_compile hermes_cli/kanban.py hermes_cli/kanban_db.py hermes_cli/kanban_closeout.py hermes_cli/kanban_drift_audit.py tools/kanban_tools.py agent/transports/codex_app_server_session.py agent/transports/codex_event_projector.py hermes_cli/runtime_provider.py
./scripts/run_tests.sh tests/hermes_cli/test_kanban_closeout_packaging.py tests/hermes_cli/test_kanban_closeout.py tests/tools/test_kanban_tools.py tests/hermes_cli/test_kanban_db.py tests/agent/transports/test_codex_app_server_session.py tests/agent/transports/test_codex_event_projector.py tests/run_agent/test_codex_app_server_integration.py
# 7 files / 436 tests passed / 0 failed

git diff --check

python - <<'PY'
from model_tools import get_tool_definitions
schemas = get_tool_definitions(quiet_mode=True)
names = [s['function']['name'] for s in schemas]
print('tool_schema_count', len(names))
print('has_terminal', 'terminal' in names)
print('has_tts', 'text_to_speech' in names)
print('has_google_workspace_profiles', 'google_workspace_profiles' in names)
print('duplicates', sorted({x for x in names if names.count(x)>1}))
from gateway.config import load_gateway_config
cfg = load_gateway_config()
print('gateway_config_loaded', type(cfg).__name__, bool(getattr(cfg,'platforms',None)))
PY
# tool_schema_count=45; has_terminal=True; has_tts=True; has_google_workspace_profiles=True; duplicates=[]; gateway_config_loaded=GatewayConfig True
```

Hindsight/OAuth drop check:

```bash
BASE=$(git merge-base HEAD upstream/main)
git diff --name-only "$BASE...HEAD" | grep -Ei 'hindsight|oauth|codex.*bridge' || echo 'no Hindsight/OAuth bridge files in proof diff'
# no Hindsight/OAuth bridge files in proof diff
```

### Git 상태

- Root checkout: inspected read-only; not mutated.
- Proof worktree: clean at `6bb84afa58808ed66ea0f325fbca1579c4691a36`, tracking `fork/yuuka/hermes-upstream-20260601-slice2-local`.
- Plan worktree: this RALPLAN closeout section is the final planning artifact update and should be committed/pushed as plan evidence.
- PR #81: draft/open, base `yuuka/upstream-main-20260601-base`, head `yuuka/hermes-upstream-20260601-slice2-local`, hosted checks green at recorded head.

### Live 상태

- Gateway not restarted or reloaded.
- Live runtime not applied.
- Root/canonical checkout not materialized.
- Env/secret/provider/config not mutated.
- Cron not mutated.

### ralplan_consensus_gate

```yaml
ralplan_consensus_gate:
  complete: true
  final_planner_plan: .omh/plans/ralplan-hermes-upstream-carry-reconciliation.md
  architect_review: present
  critic_review: present
  revisions_folded: true
hermes_overlay:
  admission_ready: true
  review_ready: true
  admission_state: admission_ready_pending_approval
  execution_authority: not_admitted
  execution_approved: false
approval_boundary:
  approved: local proof work, tests, commits, fork push/PR update, RALPLAN update/commit
  still_requires_separate_approval:
    - upstream PR/comment/push/update
    - merge
    - root/canonical checkout materialization
    - gateway restart/reload
    - live runtime apply
    - env/secret/provider/config mutation
    - cron mutation
    - customer-facing/external send
    - cost-bearing work
```
