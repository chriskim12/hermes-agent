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
    evaluate_command_request,
    evaluate_root_delta,
    lifecycle_report,
    mount_identity_for,
    parse_mountinfo,
    resolver_contract,
    rollout_flags_from_env,
    infer_command_output_paths,
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

def test_classifies_historical_root_build_outputs_as_rebuildable_mass():
    ctx = _context(hermes_home="/home/ubuntu/.hermes")
    target = classify_path("/home/ubuntu/apps/gajae-code/target", ctx)
    assert target.mount_role == MountRole.ROOT
    assert target.path_class == PathClass.UNKNOWN_ROOT_MASS
    assert target.truth_surface == TruthSurface.REBUILDABLE
    assert BlockerCode.ROOT_HEAVY_WORK.value in target.blockers

    next_dir = classify_path("/home/ubuntu/repos/hermes/web/.next", ctx)
    assert next_dir.path_class == PathClass.UNKNOWN_ROOT_MASS
    assert BlockerCode.ROOT_HEAVY_WORK.value in next_dir.blockers

    node_modules = classify_path("/home/ubuntu/repos/hermes/node_modules", ctx)
    assert node_modules.path_class == PathClass.UNKNOWN_ROOT_MASS
    assert BlockerCode.ROOT_HEAVY_WORK.value in node_modules.blockers


def test_command_preflight_infers_root_heavy_outputs_from_cwd_and_command():
    ctx = _context(
        rollout=RolloutFlags(mode=EnforcementMode.BLOCK, host_mode=HostMode.REQUIRED_HERMES_HOST, block_new_root_heavy=True),
        root_used_percent=82.0,
    )

    assert infer_command_output_paths("cargo build", "/home/ubuntu/apps/gajae-code") == (
        "/home/ubuntu/apps/gajae-code/target",
    )

    cargo = evaluate_command_request("cargo build", "/home/ubuntu/apps/gajae-code", ctx)
    assert cargo.decision == Decision.BLOCK
    assert cargo.classification is not None
    assert cargo.classification.path == "/home/ubuntu/apps/gajae-code/target"
    assert BlockerCode.ROOT_HEAVY_WORK.value in cargo.blockers

    npm = evaluate_command_request("npm run build", "/home/ubuntu/repos/hermes/web", ctx)
    assert npm.decision == Decision.BLOCK
    assert BlockerCode.ROOT_HEAVY_WORK.value in npm.blockers
    assert ".next" in npm.message
    assert "node_modules" in npm.message


def test_root_override_metadata_controls_root_heavy_blocking():
    incomplete = _context(
        rollout=RolloutFlags(
            mode=EnforcementMode.BLOCK,
            host_mode=HostMode.REQUIRED_HERMES_HOST,
            block_new_root_heavy=True,
            allow_root_override=True,
            approval_id="APPROVED-1",
            owner="ops",
            reason="   ",
            remove_by="2026-07-01T00:00:00Z",
        )
    )
    blocked = evaluate_path_request("/home/ubuntu/apps/gajae-code/target", incomplete)
    assert blocked.decision == Decision.BLOCK
    assert BlockerCode.ROOT_HEAVY_WORK.value in blocked.blockers
    assert BlockerCode.UNAPPROVED_ROOT_OVERRIDE.value in blocked.blockers

    complete = _context(
        rollout=RolloutFlags(
            mode=EnforcementMode.BLOCK,
            host_mode=HostMode.REQUIRED_HERMES_HOST,
            block_new_root_heavy=True,
            allow_root_override=True,
            approval_id="APPROVED-1",
            owner="ops",
            reason="bounded rebuild smoke",
            remove_by="2026-07-01T00:00:00Z",
        )
    )
    overridden = evaluate_path_request("/home/ubuntu/apps/gajae-code/target", complete)
    assert overridden.decision == Decision.WARN
    assert BlockerCode.ROOT_HEAVY_WORK.value in overridden.warnings
    assert BlockerCode.UNAPPROVED_ROOT_OVERRIDE.value not in overridden.blockers


def test_command_preflight_preserves_observe_warn_block_modes():
    root_cwd = "/home/ubuntu/apps/gajae-code"

    observe = evaluate_command_request("cargo build", root_cwd, _context())
    assert observe.decision == Decision.OBSERVE
    assert BlockerCode.ROOT_HEAVY_WORK.value in observe.warnings

    warn = evaluate_command_request(
        "cargo build",
        root_cwd,
        _context(rollout=RolloutFlags(mode=EnforcementMode.WARN, block_new_root_heavy=True)),
    )
    assert warn.decision == Decision.WARN
    assert BlockerCode.ROOT_HEAVY_WORK.value in warn.blockers

    block = evaluate_command_request(
        "cargo build",
        root_cwd,
        _context(rollout=RolloutFlags(mode=EnforcementMode.BLOCK, block_new_root_heavy=True)),
    )
    assert block.decision == Decision.BLOCK
    assert BlockerCode.ROOT_HEAVY_WORK.value in block.blockers

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


def test_containerd_root_is_root_heavy_mass_not_control_plane():
    # B/C classifier fix #2: /var/lib/containerd was misclassified CONTROL_PLANE,
    # letting the largest runtime data-root growth escape enforcement.
    ctx = _context(hermes_home="/home/ubuntu/.hermes")
    for path in ("/var/lib/containerd", "/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs"):
        c = classify_path(path, ctx)
        assert c.path_class == PathClass.UNKNOWN_ROOT_MASS, path
        assert c.mount_role == MountRole.ROOT, path
        assert BlockerCode.ROOT_HEAVY_WORK.value in c.blockers, path
        assert c.path_class != PathClass.CONTROL_PLANE, path


def test_docker_root_still_root_heavy_mass_regression():
    # docker was already heavy; adding containerd must not regress it.
    ctx = _context(hermes_home="/home/ubuntu/.hermes")
    c = classify_path("/var/lib/docker/overlay2", ctx)
    assert c.path_class == PathClass.UNKNOWN_ROOT_MASS
    assert BlockerCode.ROOT_HEAVY_WORK.value in c.blockers


def test_root_worktrees_are_root_heavy_mass_not_control_plane():
    # B/C classifier fix #1: root-resident worktree trees were misrouted to
    # CONTROL_PLANE, escaping workbench/enforcement classification.
    ctx = _context(hermes_home="/home/ubuntu/.hermes")
    for path in (
        "/home/ubuntu/worktrees/dailychingu-dc087",
        "/home/ubuntu/repos/.worktrees/oh-my-codex/task-owned-worktree-guard",
    ):
        c = classify_path(path, ctx)
        assert c.path_class == PathClass.UNKNOWN_ROOT_MASS, path
        assert BlockerCode.ROOT_HEAVY_WORK.value in c.blockers, path
        assert c.path_class != PathClass.CONTROL_PLANE, path


def test_hermes_home_worktrees_route_to_heavy_mass():
    # A worktrees subtree under ~/.hermes should now be heavy mass, not control plane.
    ctx = _context(hermes_home="/home/ubuntu/.hermes")
    c = classify_path("/home/ubuntu/.hermes/worktrees/task-abc", ctx)
    assert c.path_class == PathClass.UNKNOWN_ROOT_MASS
    assert c.mount_role == MountRole.ROOT


def test_heavy_name_additions_do_not_overmatch_control_paths():
    # Over-match guard: containerd config and Git's linked-worktree metadata are
    # control-plane paths, and filename substrings are not heavy components.
    ctx = _context(hermes_home="/home/ubuntu/.hermes")
    for path in (
        "/etc/systemd/journald.conf.d",
        "/etc/containerd/config.toml",
        "/var/lib/postgresql",
        "/home/ubuntu/repos/project/.git/worktrees/feature",
        "/home/ubuntu/config/worktrees.yaml",
        "/home/ubuntu/repos/containerd-notes.md",
    ):
        c = classify_path(path, ctx)
        assert c.path_class == PathClass.CONTROL_PLANE, path
        assert BlockerCode.ROOT_HEAVY_WORK.value not in c.blockers, path
