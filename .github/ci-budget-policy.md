# CI budget and full-sweep policy

This repo uses a tiered CI contract so PRs stay reviewable without deleting safety coverage.

## Tiers

### Tier 0 — always-on PR safety

Cheap/high-signal checks should stay on ordinary PRs:

- history/common-ancestor checks
- ruff/ty diff checks
- Windows-footgun checks
- sharded unit tests plus the aggregate `test result`
- e2e smoke tests
- narrow supply-chain/lockfile checks when their watched files change

### Tier 1 — path-relevant PR heavy checks

Heavy checks should run on PRs only when the changed paths can affect them:

- Docker PR smoke: container/package/runtime-entrypoint surfaces
- Nix: flake, nix, package/dependency, and Nix-consumed frontend surfaces
- docs site checks: website/docs site surfaces
- lock/security scans: lock/dependency surfaces

Path-filtered checks must not be the only required branch-protection context unless skipped workflows are known not to deadlock the merge gate. Prefer a stable aggregate check when a required context is needed.

### Tier 2 — full-sweep backstop

Broad confidence is preserved outside the fast PR lane:

- `push` to `main` runs the broad post-merge gates configured in each workflow.
- Weekly scheduled runs cover Tests, Nix, Docker smoke, and OSV lockfile scanning.
- `workflow_dispatch` exists for manual full-sweep reruns when a PR, release, or Autopilot lane needs fresh broad evidence.
- Docker publish/tag movement remains restricted to `push` on `main` and `release`; scheduled/manual Docker runs are smoke-only because publish steps are guarded by event checks.

### Tier 3 — Autopilot / operator CI budget rules

Autopilot and human operators should classify CI state explicitly instead of blindly rerunning expensive jobs:

- `green`: all relevant checks reached success or expected skip.
- `waiting`: checks are still queued/in-progress and within their normal budget.
- `ci-budget-blocked`: a check exceeded or approached runtime budget without clear task-local failure evidence.
- `runtime-promotion-blocked`: code/PR is review-ready, but live apply, gateway restart, release, merge, or branch-protection changes still require separate authority.

Recommended operating limits:

- Keep at most one full-sweep/stacked CI lane active per parent program unless Chris explicitly asks for parallel CI burn.
- Do not rerun the same expensive failed or timed-out check more than once without reading logs and classifying the failure.
- Do not fix slowness by only increasing timeout; add diagnostics or split/shard/path-filter the workload.
- Do not treat fork Docker skips as upstream Docker smoke proof. They only prove the fork/no-secret boundary.
- Record PR URL, head SHA, check rollup, and explicit non-actions in Kanban closeout evidence.

## Current BO-107 contract

BO-107 reduced PR CI cost by:

1. enabling uv cache and pytest diagnostics;
2. splitting unit pytest into 4 shards with `test result` aggregate;
3. narrowing Docker PR triggers while preserving `push`/release publish coverage;
4. narrowing Nix PR triggers while preserving `push`/manual/scheduled backstop coverage;
5. documenting this budget policy so future agents do not re-expand heavy PR gates accidentally.
