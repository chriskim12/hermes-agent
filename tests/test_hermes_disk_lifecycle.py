from __future__ import annotations

from datetime import datetime, timezone

from hermes_disk_lifecycle import (
    BlockerCode,
    Decision,
    EnforcementMode,
    HostMode,
    LifecycleContext,
    MountRole,
    PathClass,
    RolloutFlags,
    TruthSurface,
    classify_path,
    evaluate_path_request,
    evaluate_root_delta,
    lifecycle_report,
    mount_identity_for,
    parse_mountinfo,
    resolver_contract,
    rollout_flags_from_env,
    validate_manifest,
)


def _context(**overrides):
    base = {
        "active_profile": "coder",
        "hermes_home": "/home/me/.hermes/profiles/coder",
        "data_root": "/mnt/hermes-data",
        "extra_root": "/mnt/hermes-extra",
        "workspace_root": "/mnt/hermes-extra/workspaces/coder",
        "sandbox_root": "/mnt/hermes-extra/sandboxes/coder",
        "cache_root": "/mnt/hermes-extra/caches/coder",
    }
    base.update(overrides)
    return LifecycleContext(**base)


def test_classifies_data_extra_root_and_profile_paths():
    ctx = _context()
    assert classify_path("/mnt/hermes-data/profiles/coder/logs/agent.log", ctx).truth_surface == TruthSurface.LOGS
    extra = classify_path("/mnt/hermes-extra/workspaces/coder/repo", ctx)
    assert extra.mount_role == MountRole.EXTRA
    assert extra.path_class == PathClass.HEAVY_WORKBENCH
    root_cache = classify_path("/home/me/.hermes/profiles/coder/sandboxes/build", ctx)
    assert root_cache.mount_role == MountRole.ROOT
    assert root_cache.path_class == PathClass.UNKNOWN_ROOT_MASS


def test_fake_mount_directory_is_not_valid_mount_identity():
    mounts = (
        {"mount_point": "/", "device_id": "8:1", "fs_type": "ext4", "source": "/dev/root"},
        {"mount_point": "/mnt/hermes-data", "device_id": "8:1", "fs_type": "ext4", "source": "/dev/root"},
        {"mount_point": "/mnt/hermes-extra", "device_id": "8:2", "fs_type": "xfs", "source": "/dev/disk/by-id/extra"},
    )
    data = mount_identity_for("/mnt/hermes-data", MountRole.DATA, mounts)
    extra = mount_identity_for("/mnt/hermes-extra", MountRole.EXTRA, mounts)
    assert data.real_mount is False
    assert BlockerCode.MOUNT_IDENTITY_INVALID.value in data.blockers
    assert extra.real_mount is True


def test_required_host_blocks_root_heavy_work_and_warn_mode_downgrades_to_warning():
    ctx = _context(
        rollout=RolloutFlags(mode=EnforcementMode.BLOCK, host_mode=HostMode.REQUIRED_HERMES_HOST, block_new_root_heavy=True),
        root_used_percent=82.0,
    )
    decision = evaluate_path_request("/home/me/.hermes/profiles/coder/sandboxes/build", ctx)
    assert decision.decision == Decision.BLOCK
    assert BlockerCode.ROOT_HEAVY_WORK.value in decision.blockers
    assert BlockerCode.ROOT_ABOVE_BLOCK.value in decision.blockers

    warn_ctx = _context(
        rollout=RolloutFlags(mode=EnforcementMode.WARN, host_mode=HostMode.REQUIRED_HERMES_HOST, block_new_root_heavy=True),
        root_used_percent=82.0,
    )
    assert evaluate_path_request("/home/me/.hermes/profiles/coder/sandboxes/build", warn_ctx).decision == Decision.WARN


def test_rollout_flags_resolve_defaults_and_overrides():
    flags = rollout_flags_from_env({
        "HERMES_DISK_LIFECYCLE_MODE": "block",
        "HERMES_DISK_HOST_MODE": "required_hermes_host",
        "HERMES_DISK_REQUIRE_MANIFEST_ON_CLOSEOUT": "yes",
    })
    assert flags.mode == EnforcementMode.BLOCK
    assert flags.host_mode == HostMode.REQUIRED_HERMES_HOST
    assert flags.block_new_root_heavy is True
    assert flags.require_manifest_on_closeout is True
    assert flags.mount_identity_required is True


def test_required_host_blocks_root_resident_durable_truth():
    ctx = _context(
        rollout=RolloutFlags(mode=EnforcementMode.BLOCK, host_mode=HostMode.REQUIRED_HERMES_HOST),
    )
    decision = evaluate_path_request("/home/me/.hermes/profiles/coder/cron/output", ctx)
    assert decision.decision == Decision.BLOCK
    assert BlockerCode.DATA_MOUNT_MISSING.value in decision.blockers


def test_manifest_validator_accepts_valid_extra_manifest_and_rejects_truth_conflicts():
    ctx = _context()
    manifest = {
        "schema": "hermes_artifact_manifest.v1",
        "owner": "kanban-worker",
        "run_id": "run-1",
        "card_id": "CARD-1",
        "created_at": "2026-06-24T10:00:00Z",
        "purpose": "build cache retained for repro",
        "truth_surface": "rebuildable",
        "cleanup_policy": "remove_by",
        "remove_by": "2026-07-01T10:00:00Z",
        "path": "/mnt/hermes-extra/workspaces/coder/repo/.cache",
        "mount_role": "extra",
        "adapter": "kanban_closeout",
        "disposition": "retained",
        "source_surface": "kanban_review_ready",
        "profile": "coder",
        "hermes_home": "/home/me/.hermes/profiles/coder",
        "mount_device_id": "8:2",
    }
    valid = validate_manifest(manifest, ctx, now=datetime(2026, 6, 24, tzinfo=timezone.utc))
    assert valid.valid is True

    conflicted = dict(manifest, truth_surface="runtime_state", path="/mnt/hermes-extra/workspaces/coder/state.db")
    invalid = validate_manifest(conflicted, ctx, now=datetime(2026, 6, 24, tzinfo=timezone.utc))
    assert invalid.valid is False
    assert BlockerCode.MANIFEST_TRUTH_SURFACE_CONFLICT.value in invalid.blockers


def test_manifest_validator_rejects_invalid_enums_and_mount_device_mismatch():
    ctx = _context(mount_table=(
        {"mount_point": "/", "device_id": "8:1", "fs_type": "ext4", "source": "/dev/root"},
        {"mount_point": "/mnt/hermes-extra", "device_id": "8:2", "fs_type": "xfs", "source": "/dev/extra"},
    ))
    manifest = {
        "schema": "wrong",
        "owner": "kanban-worker",
        "run_id": "run-1",
        "card_id": "CARD-1",
        "created_at": "2026-06-24T10:00:00Z",
        "purpose": "build cache retained for repro",
        "truth_surface": "not-a-surface",
        "cleanup_policy": "remove_by",
        "remove_by": "2026-07-01T10:00:00Z",
        "path": "/mnt/hermes-extra/workspaces/coder/repo/.cache",
        "mount_role": "extra",
        "adapter": "kanban_closeout",
        "disposition": "retained",
        "source_surface": "kanban_review_ready",
        "profile": "coder",
        "hermes_home": "/home/me/.hermes/profiles/coder",
        "mount_device_id": "WRONG",
    }
    invalid = validate_manifest(manifest, ctx, now=datetime(2026, 6, 24, tzinfo=timezone.utc))
    assert invalid.valid is False
    assert "manifest_invalid_field:schema" in invalid.blockers
    assert "manifest_invalid_field:truth_surface" in invalid.blockers
    assert BlockerCode.MANIFEST_PATH_MOUNT_CONFLICT.value in invalid.blockers


def test_root_delta_and_dry_run_report_are_structured():
    delta = evaluate_root_delta(100, 200, expected_bytes=10, mode=EnforcementMode.WARN)
    assert delta.decision == Decision.WARN
    assert BlockerCode.UNACCOUNTED_ROOT_DELTA.value in delta.warnings

    report = lifecycle_report(context=_context(), paths=["/mnt/hermes-extra/workspaces/coder/repo"], manifests=[])
    assert report["schema"] == "hermes_disk_lifecycle_report.v1"
    assert report["manifest_report"]["count"] == 0


def test_resolver_contract_exposes_data_and_extra_destinations():
    contract = resolver_contract(_context())
    assert contract["state_db_path"].endswith("state.db")
    assert contract["data_evidence_root"] == "/mnt/hermes-data/profiles/coder/evidence"
    assert contract["extra_workspace_root"] == "/mnt/hermes-extra/workspaces/coder"


def test_mountinfo_parser_extracts_device_and_mount_point():
    parsed = parse_mountinfo("26 22 8:1 / / rw,relatime - ext4 /dev/root rw\n27 22 8:2 / /mnt/hermes-extra rw - xfs /dev/extra rw")
    assert parsed[0]["mount_point"] == "/"
    assert parsed[1]["device_id"] == "8:2"
