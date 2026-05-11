# BO-006 patched skill excerpts

These excerpts were copied from live Hermes skills after the approved BO-006 patches. They provide concrete skill-file evidence for the repo PR ledger.


## `omx-card-execution-routing`


### Excerpt around `version:`

```markdown

```

### Patched section

```markdown
## Authority boundary

Kanban is the default task authority for new work. Linear/`CH-*` references are legacy compatibility only unless the live task is explicitly declared as a Linear legacy-authority card.

Before broad reads, worktree creation, mutation, or OMX handoff:
1. read the live Kanban item when one exists;
2. record the routing verdict on the Kanban item, not only in chat or Linear;
3. treat GitHub PRs, OMX session state, Discord/wiki/dashboard summaries, and Linear comments as evidence/projection unless one of them is explicitly the authority surface for that legacy lane;
4. if Kanban authority and a projection disagree, fail closed and reconcile Kanban first.
```


## `github-pr-workflow`


### Excerpt around `version:`

```markdown

```

### Patched section

```markdown
Operational rule:
- for Kanban-authority work, treat GitHub PRs/checks as evidence and review surface, not task authority; persist PR URL, head SHA, CI rollup, mergeability, and cleanup proof back to the Kanban item/closeout evidence
- use the branch's configured remote when present
- if the repo has `fork`/`upstream`, usually **push to `fork`** and treat `upstream` as read-only unless the user explicitly wants otherwise
- **do not open or even suggest an upstream PR by default after a fork push**; after publish, stop and wait unless Chris explicitly instructs an upstream PR in that same turn
- if the branch was created from `fork/main` and the operating contract says fork/local truth is the integration surface, create the PR against the fork repo explicitly (`gh pr create --repo <fork-owner>/<repo> --base main ...`). `gh repo view` may default to the upstream repo even inside a fork-based worktree, and an accidental upstream PR can become `DIRTY` from unrelated upstream divergence. If that happens, close it with a correction comment and replace it with the fork-targeted PR; do not try to solve broad upstream conflicts as part of the bounded task slice.
- before any fetch/push/merge examples copied from this skill, normalize the remote names for the live repo instead of blindly using `origin`
- when using `gh` in a fork/upstream repo, prefer explicit targeting like `--repo <owner>/<repo>` for `pr view`, `pr checks`, `run view`, and related commands; PR numbers can exist in both repos, and an unscoped `gh pr view 5` can resolve to the wrong upstream PR with the same number
```


## `autopilot-pr-closeout-protocol`


### Excerpt around `version:`

```markdown

```

### Patched section

```markdown
## Contract

For AUTOPILOT implementation closeout, a local verified commit alone is not enough. The default closeout for Kanban-authority work is:

```text
live Kanban re-query
→ routing verdict persisted on the Kanban item
→ create/use a dedicated task worktree + branch
→ repo/worktree/ref verification
→ implementation verification
→ local verified commit
→ push feature branch
→ create PR to the repo integration branch
→ verify PR URL/base/head
→ cleanup task-owned runtime residue/processes
→ persist Kanban evidence
→ move Kanban closeout through worker_done -> review_ready
→ only close after review/merge/cleanup authority is satisfied
```

For existing Linear legacy-authority work, keep the older Linear evidence/Done sequence as compatibility behavior only.

The PR is the review/accounting unit for Chris, but it is evidence rather than task authority when the work item is Kanban-native. In an AUTOPILOT queue with cards A, B, C, the controller must not silently finish A with only local/direct repo state and then continue to B/C. Before advancing away from an implementation/code card, it should leave a reviewable PR artifact for that card, with Kanban evidence tying card ↔ branch ↔ commit ↔ PR ↔ verification ↔ cleanup. If PR creation is impossible or intentionally bypassed, that is a blocker or an explicitly recorded exception, not normal Done.

Do not merge the PR. Merge/release/prod remains Chris-controlled unless explicitly requested in the same turn.
```
