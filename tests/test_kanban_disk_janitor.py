import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "kanban_disk_janitor.py"


def load_janitor():
    spec = importlib.util.spec_from_file_location("kanban_disk_janitor_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def metadata(**overrides):
    mod = load_janitor()
    base = dict(
        task_id="BO-001",
        task_state="Done",
        terminal_since=datetime(2026, 5, 10, tzinfo=timezone.utc),
        active_worker=False,
        active_run=False,
        process_cwd_under_path=False,
        tmux_cwd_under_path=False,
        git_dirty=False,
        important_untracked=False,
        evidence_preserved=True,
        owner_known=True,
        non_allowlisted_large_files=False,
    )
    base.update(overrides)
    return mod.WorkspaceMetadata(**base)


def test_safe_artifact_candidate_requires_terminal_ttl_and_clean_gates(tmp_path):
    mod = load_janitor()
    artifact = tmp_path / "workspace" / "node_modules"
    artifact.mkdir(parents=True)

    result = mod.classify_candidate(
        artifact,
        kind="artifact",
        metadata=metadata(),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
        estimated_size_bytes=123,
    )

    assert result.state == "safe-artifact-candidate"
    assert result.auto_cleanable is True
    assert result.gates["artifact_ttl_48h"] is True
    assert result.gates["allowlisted_reproducible_artifact"] is True


def test_future_workspace_cleanup_candidate_requires_7d_and_preserved_evidence(tmp_path):
    mod = load_janitor()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=metadata(),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert result.state == "future-workspace-cleanup-candidate"
    assert result.auto_cleanable is True
    assert result.gates["workspace_ttl_7d"] is True


def test_approval_required_for_dirty_workspace_even_if_terminal(tmp_path):
    mod = load_janitor()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=metadata(git_dirty=True),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert result.state == "approval-required"
    assert result.auto_cleanable is False
    assert "git dirty" in "; ".join(result.reasons)


def test_blocked_active_for_active_worker(tmp_path):
    mod = load_janitor()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=metadata(active_worker=True),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert result.state == "blocked-active"
    assert result.auto_cleanable is False
    assert "active worker/run" in "; ".join(result.reasons)


def test_fail_closed_for_unknown_owner_and_missing_evidence(tmp_path):
    mod = load_janitor()
    artifact = tmp_path / "workspace" / "node_modules"
    artifact.mkdir(parents=True)

    result = mod.classify_candidate(
        artifact,
        kind="artifact",
        metadata=metadata(task_id=None, owner_known=False, evidence_preserved=None),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert result.state == "approval-required"
    assert result.auto_cleanable is False
    reasons = "; ".join(result.reasons)
    assert "unknown task owner" in reasons
    assert "evidence/summary preservation" in reasons


def test_unknown_task_state_is_approval_required_not_blocked_active(tmp_path):
    mod = load_janitor()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=metadata(task_state=None),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert result.state == "approval-required"
    assert result.auto_cleanable is False
    assert "unknown task state" in "; ".join(result.reasons)


def test_recent_terminal_workspace_is_approval_required_not_auto_cleanable(tmp_path):
    mod = load_janitor()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    now = datetime(2026, 5, 20, tzinfo=timezone.utc)

    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=metadata(terminal_since=now - timedelta(days=2)),
        now=now,
    )

    assert result.state == "approval-required"
    assert result.auto_cleanable is False
    assert "workspace TTL below 7d" in "; ".join(result.reasons)


def test_report_includes_surfaces_breakdown_and_no_deletion_safety(tmp_path):
    mod = load_janitor()
    hermes_home = tmp_path / ".hermes"
    workspaces = hermes_home / "kanban" / "workspaces"
    workspace = workspaces / "BO-001"
    (workspace / "node_modules").mkdir(parents=True)
    (workspace / "node_modules" / "pkg.js").write_text("x" * 10)
    (workspace / ".next").mkdir()
    (workspace / ".next" / "build").write_text("y" * 5)
    sessions = hermes_home / "sessions"
    sessions.mkdir(parents=True)
    worktrees = hermes_home / "hermes-agent" / ".worktrees"
    worktrees.mkdir(parents=True)
    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    docker = tmp_path / "docker"
    docker.mkdir()
    containerd = tmp_path / "containerd"
    containerd.mkdir()
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        """
        {"workspaces": [{
          "path": "%s",
          "task_id": "BO-001",
          "task_state": "Done",
          "terminal_since": "2026-05-10T00:00:00Z",
          "git_dirty": false,
          "important_untracked": false,
          "evidence_preserved": true,
          "owner_known": true
        }]}
        """ % workspace
    )

    args = mod.build_parser().parse_args([
        "--format", "json",
        "--metadata-json", str(metadata_path),
        "--hermes-home", str(hermes_home),
        "--workspaces-root", str(workspaces),
        "--worktrees-root", str(worktrees),
        "--sessions-root", str(sessions),
        "--tmp-root", str(tmp_root),
        "--docker-root", str(docker),
        "--containerd-root", str(containerd),
        "--now", "2026-05-20T00:00:00Z",
        "--no-live-checks",
    ])

    report = mod.build_report(args)

    assert report["mode"] == "report-first-read-only"
    assert report["root_disk_usage"]["path"] == "/"
    assert report["safety"]["deletes_files"] is False
    assert {surface["label"] for surface in report["surfaces"]} == {
        "kanban_workspaces", "agent_worktrees", "sessions", "tmp", "docker", "containerd"
    }
    workspace_report = report["kanban_workspaces"][0]
    assert workspace_report["size_breakdown"]["node_modules_bytes"] >= 10
    assert workspace_report["size_breakdown"]["next_bytes"] >= 5
    assert len(report["top_pressure_surfaces"]) == 6
    assert report["candidate_counts"]["safe-artifact-candidate"] >= 2
    assert "actual_reclaimed_size_bytes" in report["audit_manifest_fields_for_future_apply_mode"]
    definitions = report["audit_manifest_field_definitions_for_future_apply_mode"]
    assert set(definitions) == set(report["audit_manifest_fields_for_future_apply_mode"])
    assert "Measured bytes reclaimed" in definitions["actual_reclaimed_size_bytes"]
    assert "safety gate" in definitions["gates_evaluated"]
