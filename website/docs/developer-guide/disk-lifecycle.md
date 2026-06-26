# Disk lifecycle operating model

Hermes treats root (`/`) as control-plane storage only. Durable runtime truth belongs on `/mnt/hermes-data`; rebuildable heavy work belongs on `/mnt/hermes-extra`. The lifecycle policy starts in observe compatibility mode and must be promoted deliberately through warn and block after host mounts, manifests, and rollback runbooks are verified.

## Policy core

`hermes_disk_lifecycle.py` is dependency-neutral and has no filesystem-write side effects. It defines:

- host modes: `compatibility_host`, `required_hermes_host`, `test_dev_host`;
- rollout flags: `HERMES_DISK_LIFECYCLE_MODE`, `HERMES_DISK_HOST_MODE`, `HERMES_DISK_BLOCK_NEW_ROOT_HEAVY`, `HERMES_DISK_REQUIRE_MANIFEST_ON_CLOSEOUT`, `HERMES_DISK_POST_RUN_ROOT_DELTA`, `HERMES_DISK_ALLOW_ROOT_OVERRIDE`, `HERMES_DISK_MOUNT_IDENTITY_REQUIRED`;
- path classes for control-plane, durable truth/evidence, heavy workbench, rebuildable cache, temporary, unknown root mass, and invalid mount paths;
- mount identity checks that reject prefix-only fake mount directories;
- `hermes_artifact_manifest.v1` validation for retained artifacts and residue.

## Adapter behavior

Current adapters are compatibility-safe:

- terminal foreground/background preflight records lifecycle decisions and blocks only when rollout mode is `block`;
- terminal foreground post-run root delta emits lifecycle evidence according to `HERMES_DISK_POST_RUN_ROOT_DELTA`;
- sandbox root creation checks lifecycle policy before creating child directories;
- cron output directory creation checks lifecycle policy before creating output directories;
- Kanban closeout residue validation accepts supplied manifests in all modes and requires them only when `HERMES_DISK_REQUIRE_MANIFEST_ON_CLOSEOUT=true`.

No adapter migrates state, sessions, logs, Docker data-root, toolchains, caches, worktrees, apps, or unknown residue. No adapter restarts Hermes or changes global host toolchain configuration.

## Manifest contract

Every retained artifact manifest uses schema `hermes_artifact_manifest.v1` and includes:

```json
{
  "schema": "hermes_artifact_manifest.v1",
  "owner": "worker-or-operator",
  "run_id": "run-id",
  "card_id": "card-id",
  "created_at": "2026-06-24T10:00:00Z",
  "purpose": "why this artifact is retained",
  "truth_surface": "rebuildable",
  "cleanup_policy": "remove_by",
  "remove_by": "2026-07-01T10:00:00Z",
  "path": "/mnt/hermes-extra/workspaces/profile/repo/.cache",
  "mount_role": "extra",
  "adapter": "kanban_closeout",
  "disposition": "retained",
  "source_surface": "kanban_review_ready",
  "profile": "profile",
  "hermes_home": "/home/user/.hermes/profiles/profile",
  "mount_device_id": "8:2"
}
```

Durable truth/evidence (`runtime_state`, `audit`, `logs`, `cron_output`, `final_evidence`) must not be placed on `extra`. Rebuildable work must not be placed on `data` without a higher-level archival decision and separate approval evidence. Root-resident non-control artifacts fail manifest validation.

## Dry-run reporting

Use `hermes_cli.disk_lifecycle.dry_run_report()` from diagnostics, tests, or future CLI/API surfaces to produce a non-mutating report with rollout mode, mount identity, root utilization, path decisions, manifest counts, unowned/invalid residue blockers, and warnings. The helper reads local filesystem facts but never creates mount roots, deletes data, or moves files.

## Future approval-gated migrations

Each live migration remains separately approval-gated and must include quiesce, backup, verification, rollback, owner, approval id, and post-migration root delta evidence:

1. `state.db` and durable runtime truth to a profile-scoped data path.
2. Logs, sessions, cron output, audit, and final evidence to data.
3. Worktrees, repos, apps, toolchains, caches, build outputs, and Docker proof lanes to extra.
4. Docker data-root migration with daemon downtime, inventory, smoke test, and rollback.
5. Unknown root residue classification before retention, move, or deletion.

The current implementation provides gates and evidence surfaces only; it does not perform those migrations.
