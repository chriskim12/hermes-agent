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


def test_terminal_lifecycle_preflight_observes_by_default(monkeypatch):
    import tools.terminal_tool as terminal_tool

    monkeypatch.delenv("HERMES_DISK_LIFECYCLE_MODE", raising=False)
    note = terminal_tool._disk_lifecycle_preflight_path(
        "/home/test/.hermes/profiles/default/sandboxes/build",
        background=True,
        env_type="local",
    )
    assert note is not None
    assert note["disk_lifecycle"]["decision"] == "observe"


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

# Kanban closeout integration from the original local carry is intentionally
# omitted in this port. Chris explicitly chose not to revive Kanban governance
# core extensions; disk lifecycle stays on terminal/cron/read-only CLI surfaces.
