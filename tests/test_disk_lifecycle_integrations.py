from __future__ import annotations

import pytest


def test_terminal_lifecycle_preflight_blocks_root_heavy_when_enabled(monkeypatch):
    import tools.terminal_tool as terminal_tool

    monkeypatch.setenv("HERMES_DISK_LIFECYCLE_MODE", "block")
    monkeypatch.setenv("HERMES_DISK_HOST_MODE", "required_hermes_host")
    monkeypatch.setenv("HERMES_DISK_BLOCK_NEW_ROOT_HEAVY", "true")
    note = terminal_tool._disk_lifecycle_preflight_path(
        "/home/test/.hermes/profiles/default/sandboxes/build",
        background=True,
        env_type="local",
    )
    assert note is not None
    assert note["status"] == "blocked"
    assert "root_heavy_work" in note["disk_lifecycle"]["blockers"]

def test_terminal_lifecycle_preflight_blocks_root_build_command_from_source_cwd(monkeypatch):
    import tools.terminal_tool as terminal_tool

    monkeypatch.setenv("HERMES_DISK_LIFECYCLE_MODE", "block")
    monkeypatch.setenv("HERMES_DISK_HOST_MODE", "required_hermes_host")
    monkeypatch.setenv("HERMES_DISK_BLOCK_NEW_ROOT_HEAVY", "true")
    monkeypatch.setattr(terminal_tool, "_disk_lifecycle_root_used_percent", lambda: 82.0)
    note = terminal_tool._disk_lifecycle_preflight_path(
        "/home/ubuntu/apps/gajae-code",
        background=False,
        env_type="local",
        command="cargo build",
    )
    assert note is not None
    assert note["status"] == "blocked"
    assert note["disk_lifecycle"]["classification"]["path"] == "/home/ubuntu/apps/gajae-code/target"
    assert "root_heavy_work" in note["disk_lifecycle"]["blockers"]

def test_terminal_command_preflight_uses_cached_live_cwd(monkeypatch):
    import json

    import tools.terminal_tool as terminal_tool

    class DummyEnv:
        cwd = "/home/ubuntu/apps/gajae-code"

    monkeypatch.setenv("HERMES_DISK_LIFECYCLE_MODE", "block")
    monkeypatch.setenv("HERMES_DISK_HOST_MODE", "required_hermes_host")
    monkeypatch.setenv("HERMES_DISK_BLOCK_NEW_ROOT_HEAVY", "true")
    monkeypatch.setattr(terminal_tool, "_disk_lifecycle_root_used_percent", lambda: 82.0)
    monkeypatch.setitem(terminal_tool._active_environments, "default", DummyEnv())

    result = json.loads(terminal_tool.terminal_tool("cargo build", task_id=None))

    assert result["status"] == "blocked"
    assert result["disk_lifecycle"]["classification"]["path"] == "/home/ubuntu/apps/gajae-code/target"
    assert "root_heavy_work" in result["disk_lifecycle"]["blockers"]


def test_terminal_command_preflight_preserves_warn_mode(monkeypatch):
    import tools.terminal_tool as terminal_tool

    monkeypatch.setenv("HERMES_DISK_LIFECYCLE_MODE", "warn")
    monkeypatch.setenv("HERMES_DISK_BLOCK_NEW_ROOT_HEAVY", "true")
    monkeypatch.setattr(terminal_tool, "_disk_lifecycle_root_used_percent", lambda: None)

    note = terminal_tool._disk_lifecycle_preflight_path(
        "/home/ubuntu/apps/gajae-code",
        background=True,
        env_type="local",
        command="cargo build",
    )

    assert note is not None
    assert note.get("status") != "blocked"
    assert note["disk_lifecycle"]["decision"] == "warn"
    assert "root_heavy_work" in note["disk_lifecycle"]["blockers"]


def test_sandbox_preflight_logs_warn_mode_blocker_diagnostics(monkeypatch, caplog):
    from pathlib import Path

    import tools.environments.base as env_base

    monkeypatch.setenv("HERMES_DISK_LIFECYCLE_MODE", "warn")
    monkeypatch.setenv("HERMES_DISK_BLOCK_NEW_ROOT_HEAVY", "true")
    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", "/home/ubuntu/apps/gajae-code/target")
    monkeypatch.setattr(env_base, "_disk_lifecycle_mount_table", lambda: ())
    monkeypatch.setattr(Path, "mkdir", lambda self, *args, **kwargs: None)

    with caplog.at_level("WARNING"):
        sandbox = env_base.get_sandbox_dir()

    assert str(sandbox) == "/home/ubuntu/apps/gajae-code/target"
    assert "root_heavy_work" in caplog.text


def test_sandbox_preflight_normalizes_custom_root(monkeypatch):
    from pathlib import Path

    import tools.environments.base as env_base
    import tools.terminal_tool as terminal_tool

    monkeypatch.setenv("TERMINAL_SANDBOX_DIR", "~/sandboxes")
    monkeypatch.setattr(Path, "mkdir", lambda self, *args, **kwargs: None)

    sandbox = env_base.get_sandbox_dir()
    ctx = terminal_tool._disk_lifecycle_context(
        surface=terminal_tool.DiskLifecycleSurface.SANDBOX,
        work_kind=terminal_tool.DiskLifecycleWorkKind.HEAVY_WORK,
        path=str(sandbox),
    )

    assert "~" not in str(sandbox)
    assert ctx.sandbox_root == str(sandbox)


def test_cron_preflight_logs_warn_mode_blocker_diagnostics(monkeypatch, tmp_path, caplog):
    from pathlib import Path
    import cron.jobs as jobs

    monkeypatch.setenv("HERMES_DISK_LIFECYCLE_MODE", "warn")
    monkeypatch.setenv("HERMES_DISK_BLOCK_NEW_ROOT_HEAVY", "true")
    monkeypatch.setattr(jobs, "HERMES_DIR", tmp_path / ".hermes")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / ".hermes" / "cron" / "output")

    with caplog.at_level("WARNING"):
        note = jobs._enforce_cron_disk_lifecycle(Path("/home/ubuntu/apps/gajae-code/target"))

    assert note["decision"] == "warn"
    assert "root_heavy_work" in note["blockers"]
    assert "root_heavy_work" in caplog.text


def test_dry_run_report_uses_config_projection_and_candidate_classes(monkeypatch):
    import hermes_cli.disk_lifecycle as disk_lifecycle

    monkeypatch.setattr(disk_lifecycle, "_read_mount_table", lambda: ())
    monkeypatch.setattr(disk_lifecycle, "_root_used_percent", lambda path="/": 91.0)
    config = {
        "disk_lifecycle": {
            "mode": "warn",
            "host_mode": "compatibility_host",
            "block_new_root_heavy": True,
            "post_run_root_delta": "warn",
            "data_root": "/mnt/hermes-data",
            "extra_root": "/mnt/hermes-extra",
            "sandbox_root": "/mnt/hermes-extra/sandboxes/default",
        }
    }

    report = disk_lifecycle.dry_run_report(
        paths=[
            "/home/ubuntu/apps/gajae-code/target",
            "/mnt/hermes-extra/workspaces/repo/node_modules",
        ],
        env={},
        config=config,
    )

    assert report["rollout"]["mode"] == "warn"
    assert report["runtime_env_projection"]["HERMES_DISK_LIFECYCLE_MODE"] == "warn"
    assert report["live_env_status"]["missing_from_live"]
    assert report["candidate_summary"]["root_reclaiming_candidates"] == ["/home/ubuntu/apps/gajae-code/target"]
    assert report["candidate_summary"]["data_disk_cache_candidates"] == ["/mnt/hermes-extra/workspaces/repo/node_modules"]



def test_dry_run_report_coerces_string_boolean_settings():
    import hermes_cli.disk_lifecycle as disk_lifecycle

    projection = disk_lifecycle.runtime_env_projection(
        {
            "mode": "warn",
            "host_mode": "compatibility_host",
            "block_new_root_heavy": False,
            "allow_root_override": False,
            "mount_identity_required": False,
        }
    )
    assert projection["HERMES_DISK_BLOCK_NEW_ROOT_HEAVY"] == "false"
    assert projection["HERMES_DISK_ALLOW_ROOT_OVERRIDE"] == "false"
    assert projection["HERMES_DISK_MOUNT_IDENTITY_REQUIRED"] == "false"

    report = disk_lifecycle.dry_run_report(
        paths=[],
        env={},
        config={
            "disk_lifecycle": {
                "block_new_root_heavy": "false",
                "allow_root_override": "false",
                "mount_identity_required": "false",
            }
        },
    )
    assert report["runtime_env_projection"]["HERMES_DISK_BLOCK_NEW_ROOT_HEAVY"] == "false"
    assert report["runtime_env_projection"]["HERMES_DISK_ALLOW_ROOT_OVERRIDE"] == "false"
    assert report["runtime_env_projection"]["HERMES_DISK_MOUNT_IDENTITY_REQUIRED"] == "false"


def test_dry_run_report_loads_config_and_uses_resolver_defaults(monkeypatch):
    import hermes_cli.config as config_mod
    import hermes_cli.disk_lifecycle as disk_lifecycle

    monkeypatch.setattr(disk_lifecycle, "_read_mount_table", lambda: ())
    monkeypatch.setattr(disk_lifecycle, "_root_used_percent", lambda path="/": 50.0)
    monkeypatch.setattr(
        config_mod,
        "load_config_readonly",
        lambda: {
            "disk_lifecycle": {
                "mode": "block",
                "host_mode": "required_hermes_host",
                "block_new_root_heavy": True,
                "data_root": "/mnt/hermes-data",
                "extra_root": "/mnt/hermes-extra",
                "sandbox_root": "",
                "workspace_root": "",
                "cache_root": "",
            }
        },
    )

    report = disk_lifecycle.dry_run_report(env={})

    assert report["rollout"]["mode"] == "block"
    assert report["runtime_env_projection"]["HERMES_DISK_LIFECYCLE_MODE"] == "block"
    assert report["resolver_contract"]["sandbox_root"] == "/mnt/hermes-extra/sandboxes/default"
    assert report["resolver_contract"]["extra_workspace_root"] == "/mnt/hermes-extra/workspaces/default"

def test_default_config_exposes_disk_lifecycle_settings():
    from hermes_cli.config import DEFAULT_CONFIG

    settings = DEFAULT_CONFIG["disk_lifecycle"]
    assert settings["mode"] == "observe"
    assert settings["data_root"] == "/mnt/hermes-data"
    assert settings["extra_root"] == "/mnt/hermes-extra"


def test_terminal_lifecycle_preflight_uses_config_when_env_projection_missing(monkeypatch):
    import hermes_cli.config as config_mod
    import tools.terminal_tool as terminal_tool

    for key in [
        "HERMES_DISK_LIFECYCLE_MODE",
        "HERMES_DISK_HOST_MODE",
        "HERMES_DISK_BLOCK_NEW_ROOT_HEAVY",
        "HERMES_DISK_POST_RUN_ROOT_DELTA",
        "HERMES_DISK_ALLOW_ROOT_OVERRIDE",
        "HERMES_DISK_MOUNT_IDENTITY_REQUIRED",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(terminal_tool, "_disk_lifecycle_root_used_percent", lambda: 82.0)
    monkeypatch.setattr(
        config_mod,
        "load_config_readonly",
        lambda: {
            "disk_lifecycle": {
                "mode": "block",
                "host_mode": "compatibility_host",
                "block_new_root_heavy": True,
                "post_run_root_delta": "warn",
                "data_root": "/mnt/hermes-data",
                "extra_root": "/mnt/hermes-extra",
                "workspace_root": "/mnt/hermes-extra/workspaces/default",
            }
        },
    )

    note = terminal_tool._disk_lifecycle_preflight_path(
        "/home/ubuntu/repos/dailychingu",
        background=False,
        env_type="local",
        command="npm install",
    )

    assert note is not None
    assert note["status"] == "blocked"
    assert note["disk_lifecycle"]["enforcement_mode"] == "block"
    assert note["disk_lifecycle"]["classification"]["path"] == "/home/ubuntu/repos/dailychingu/node_modules"
    assert "root_heavy_work" in note["disk_lifecycle"]["blockers"]


def test_root_emergency_does_not_block_extra_workspace_reroute():
    from hermes_disk_lifecycle import (
        EnforcementMode,
        LifecycleContext,
        RolloutFlags,
        Surface,
        evaluate_command_request,
        parse_mountinfo,
    )

    mount_table = parse_mountinfo(
        "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "2 0 8:32 / /mnt/hermes-extra rw - ext4 /dev/sdc rw\n"
        "3 0 8:16 / /mnt/hermes-data rw - ext4 /dev/sdb rw\n"
    )
    ctx = LifecycleContext(
        data_root="/mnt/hermes-data",
        extra_root="/mnt/hermes-extra",
        workspace_root="/mnt/hermes-extra/workspaces/default",
        surface=Surface.TERMINAL,
        rollout=RolloutFlags(mode=EnforcementMode.BLOCK, block_new_root_heavy=True),
        mount_table=mount_table,
        root_used_percent=98.0,
    )

    decision = evaluate_command_request("npm install", "/mnt/hermes-extra/workspaces/default/demo", ctx)

    assert decision.allowed is True
    assert decision.decision.value == "warn"
    assert "root_above_emergency" in decision.warnings
    assert "root_above_emergency" not in decision.blockers


def test_cron_lifecycle_blocks_output_path_in_block_mode(monkeypatch, tmp_path):
    import cron.jobs as jobs

    monkeypatch.setenv("HERMES_DISK_LIFECYCLE_MODE", "block")
    monkeypatch.setenv("HERMES_DISK_HOST_MODE", "required_hermes_host")
    monkeypatch.setenv("HERMES_DISK_BLOCK_NEW_ROOT_HEAVY", "true")
    monkeypatch.setattr(jobs, "HERMES_DIR", tmp_path / ".hermes")
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / ".hermes" / "cron")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / ".hermes" / "cron" / "output")

    with pytest.raises(RuntimeError, match="Disk lifecycle policy blocked cron output path"):
        jobs.ensure_dirs()


def test_cron_lifecycle_classifies_against_active_profile_store(monkeypatch, tmp_path):
    """A profile-scoped cron store (via use_cron_store) must classify its own
    output path as durable cron-output-under-hermes-home, not against the
    frozen default-profile HERMES_DIR/OUTPUT_DIR module globals (#profile
    cron lifecycle carry bug: dashboard's _call_cron_for_profile scopes
    storage to a non-active profile's home via use_cron_store, but disk
    lifecycle classification kept anchoring on the import-time default)."""
    import cron.jobs as jobs

    # Leave HERMES_DIR/OUTPUT_DIR pointed at an unrelated default-profile home
    # so a bug that reads those globals instead of the active store would
    # misclassify the profile's cron output as outside any known hermes_home
    # (falling through to /tmp-temporary or root-heavy/unknown-mass) instead
    # of recognizing it as durable truth under its own profile home.
    monkeypatch.setattr(jobs, "HERMES_DIR", tmp_path / "default-home")
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "default-home" / "cron")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "default-home" / "cron" / "output")

    profile_home = tmp_path / "profiles" / "coder"
    with jobs.use_cron_store(profile_home):
        decision = jobs._cron_disk_lifecycle_decision(profile_home / "cron" / "output")

    assert decision.classification is not None
    assert decision.classification.path_class.value == "durable_truth"
    assert decision.classification.mount_role.value == "root"
    assert "root_heavy_work" not in decision.blockers


def test_cron_store_hermes_home_matches_output_dir(tmp_path):
    """use_cron_store must scope hermes_home alongside cron_dir/output_dir so
    downstream disk-lifecycle classification stays consistent with the active
    profile, not just the storage paths."""
    import cron.jobs as jobs

    profile_home = tmp_path / "profiles" / "writer"
    with jobs.use_cron_store(profile_home):
        store = jobs._current_cron_store()
        assert store.hermes_home == profile_home.resolve()
        assert store.output_dir == profile_home.resolve() / "cron" / "output"

    # Outside the context manager, the store reverts to the process default.
    default_store = jobs._current_cron_store()
    assert default_store.hermes_home == jobs.HERMES_DIR

# Kanban closeout integration from the original local carry is intentionally
# omitted in this port. Chris explicitly chose not to revive Kanban governance
# core extensions; disk lifecycle stays on terminal/cron/read-only CLI surfaces.
