import importlib.util
import json
import sqlite3
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


def create_kanban_db(path, rows, *, include_runs=True):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            public_id TEXT,
            status TEXT NOT NULL,
            completed_at INTEGER,
            workspace_path TEXT,
            claim_lock TEXT,
            worker_pid INTEGER,
            current_run_id INTEGER,
            closeout_evidence TEXT,
            result TEXT
        )
        """
    )
    if include_runs:
        conn.execute(
            """
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                worker_pid INTEGER,
                claim_lock TEXT,
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                summary TEXT,
                metadata TEXT
            )
            """
        )
    for row in rows:
        conn.execute(
            """
            INSERT INTO tasks (
                id, public_id, status, completed_at, workspace_path,
                claim_lock, worker_pid, current_run_id, closeout_evidence, result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row.get("public_id"),
                row.get("status", "done"),
                row.get("completed_at"),
                row.get("workspace_path"),
                row.get("claim_lock"),
                row.get("worker_pid"),
                row.get("current_run_id"),
                json.dumps(row["closeout_evidence"]) if "closeout_evidence" in row else row.get("closeout_evidence"),
                row.get("result"),
            ),
        )
        if include_runs:
            for run in row.get("runs", []):
                conn.execute(
                    """
                    INSERT INTO task_runs (
                        task_id, status, worker_pid, claim_lock, started_at,
                        ended_at, summary, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        run.get("status", "done"),
                        run.get("worker_pid"),
                        run.get("claim_lock"),
                        run.get("started_at", row.get("completed_at") or 0),
                        run.get("ended_at", row.get("completed_at")),
                        run.get("summary"),
                        json.dumps(run["metadata"]) if "metadata" in run else run.get("metadata"),
                    ),
                )
    conn.commit()
    conn.close()


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


def test_kanban_db_done_workspace_with_evidence_can_make_allowlisted_artifact_safe(tmp_path, monkeypatch):
    mod = load_janitor()
    hermes_home = tmp_path / ".hermes"
    workspaces = hermes_home / "kanban" / "workspaces"
    workspace = workspaces / "BO-075"
    (workspace / ".git").mkdir(parents=True)
    artifact = workspace / "node_modules"
    artifact.mkdir()
    (artifact / "pkg.js").write_text("x")
    db_path = tmp_path / "kanban.db"
    create_kanban_db(
        db_path,
        [{
            "id": "task-1",
            "public_id": "BO-075",
            "status": "done",
            "completed_at": 1_778_342_400,
            "workspace_path": str(workspace),
            "closeout_evidence": {"final_summary": "done", "tests": ["unit"]},
        }],
    )
    monkeypatch.setattr(mod, "git_state", lambda path: (False, False))

    args = mod.build_parser().parse_args([
        "--format", "json",
        "--hermes-home", str(hermes_home),
        "--workspaces-root", str(workspaces),
        "--worktrees-root", str(tmp_path / "worktrees"),
        "--sessions-root", str(tmp_path / "sessions"),
        "--tmp-root", str(tmp_path / "tmp"),
        "--docker-root", str(tmp_path / "docker"),
        "--containerd-root", str(tmp_path / "containerd"),
        "--kanban-db", str(db_path),
        "--now", "2026-05-20T00:00:00Z",
        "--no-live-checks",
    ])

    report = mod.build_report(args)

    assert report["kanban_metadata"]["loaded"] is True
    assert report["kanban_metadata"]["mapped_workspaces"] == 1
    workspace_report = report["kanban_workspaces"][0]
    assert workspace_report["metadata"]["task_id"] == "task-1"
    assert workspace_report["metadata"]["public_id"] == "BO-075"
    assert workspace_report["metadata_evidence"]["mapping_confidence"] == "exact"
    assert workspace_report["artifact_candidates"][0]["state"] == "safe-artifact-candidate"


def test_kanban_db_running_task_blocks_active_workspace(tmp_path, monkeypatch):
    mod = load_janitor()
    workspaces = tmp_path / "workspaces"
    workspace = workspaces / "BO-076"
    workspace.mkdir(parents=True)
    db_path = tmp_path / "kanban.db"
    create_kanban_db(
        db_path,
        [{
            "id": "task-2",
            "public_id": "BO-076",
            "status": "running",
            "workspace_path": str(workspace),
            "worker_pid": 123,
            "current_run_id": 1,
            "runs": [{"status": "running", "worker_pid": 123, "summary": ""}],
        }],
    )
    monkeypatch.setattr(mod, "git_state", lambda path: (False, False))

    db_metadata, summary = mod.load_kanban_db_metadata(db_path, [workspace])
    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=mod.enrich_metadata(workspace, db_metadata[str(workspace)], live_checks=False),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert summary["mapped_workspaces"] == 1
    assert result.state == "blocked-active"
    assert result.gates["active_worker_or_run"] is True
    assert result.gates["active_run_status"] == "running"


def test_kanban_db_ambiguous_duplicate_mapping_stays_approval_required(tmp_path, monkeypatch):
    mod = load_janitor()
    workspaces = tmp_path / "workspaces"
    workspace = workspaces / "BO-077"
    workspace.mkdir(parents=True)
    db_path = tmp_path / "kanban.db"
    create_kanban_db(
        db_path,
        [
            {"id": "task-a", "status": "done", "completed_at": 1_778_342_400, "workspace_path": str(workspace), "closeout_evidence": {"summary": "a"}},
            {"id": "task-b", "status": "done", "completed_at": 1_778_342_400, "workspace_path": str(workspace), "closeout_evidence": {"summary": "b"}},
        ],
    )
    monkeypatch.setattr(mod, "git_state", lambda path: (False, False))

    db_metadata, summary = mod.load_kanban_db_metadata(db_path, [workspace])
    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=mod.enrich_metadata(workspace, db_metadata[str(workspace)], live_checks=False),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert summary["ambiguous_workspaces"] == 1
    assert result.state == "approval-required"
    assert result.gates["mapping_confidence"] == "ambiguous"
    assert "ambiguous Kanban task mapping" in "; ".join(result.reasons)


def test_kanban_db_missing_evidence_remains_approval_required_for_workspace_cleanup(tmp_path, monkeypatch):
    mod = load_janitor()
    workspaces = tmp_path / "workspaces"
    workspace = workspaces / "BO-078"
    workspace.mkdir(parents=True)
    db_path = tmp_path / "kanban.db"
    create_kanban_db(
        db_path,
        [{"id": "task-4", "status": "done", "completed_at": 1_778_342_400, "workspace_path": str(workspace)}],
    )
    monkeypatch.setattr(mod, "git_state", lambda path: (False, False))

    db_metadata, _summary = mod.load_kanban_db_metadata(db_path, [workspace])
    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=mod.enrich_metadata(workspace, db_metadata[str(workspace)], live_checks=False),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert result.state == "approval-required"
    assert "evidence/summary preservation" in "; ".join(result.reasons)


def test_kanban_db_schema_variation_missing_optional_fields_fails_closed(tmp_path, monkeypatch):
    mod = load_janitor()
    workspaces = tmp_path / "workspaces"
    workspace = workspaces / "BO-079"
    workspace.mkdir(parents=True)
    db_path = tmp_path / "minimal.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", ("BO-079", "done"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(mod, "git_state", lambda path: (False, False))

    db_metadata, summary = mod.load_kanban_db_metadata(db_path, [workspace])
    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=mod.enrich_metadata(workspace, db_metadata[str(workspace)], live_checks=False),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert summary["loaded"] is True
    assert summary["mapped_workspaces"] == 1
    assert result.state == "approval-required"
    reasons = "; ".join(result.reasons)
    assert "unknown terminal-state age" in reasons
    assert "evidence/summary preservation" in reasons


def test_kanban_db_fallback_name_mapping_is_explanatory_not_auto_cleanable(tmp_path, monkeypatch):
    mod = load_janitor()
    workspaces = tmp_path / "workspaces"
    workspace = workspaces / "BO-080"
    (workspace / ".git").mkdir(parents=True)
    artifact = workspace / "node_modules"
    artifact.mkdir()
    db_path = tmp_path / "kanban.db"
    create_kanban_db(
        db_path,
        [{
            "id": "task-5",
            "public_id": "BO-080",
            "status": "done",
            "completed_at": 1_778_342_400,
            "closeout_evidence": {"summary": "done"},
        }],
    )
    monkeypatch.setattr(mod, "git_state", lambda path: (False, False))

    db_metadata, summary = mod.load_kanban_db_metadata(db_path, [workspace])
    result = mod.classify_candidate(
        artifact,
        kind="artifact",
        metadata=mod.enrich_metadata(workspace, db_metadata[str(workspace)], live_checks=False),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
        estimated_size_bytes=1,
    )

    assert summary["mapped_workspaces"] == 1
    assert result.state == "approval-required"
    assert result.gates["mapping_confidence"] == "fallback-name"
    assert "unknown task owner or task id" in "; ".join(result.reasons)


def test_archived_task_state_does_not_satisfy_terminal_cleanup_gate(tmp_path, monkeypatch):
    mod = load_janitor()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(mod, "git_state", lambda path: (False, False))

    result = mod.classify_candidate(
        workspace,
        kind="workspace",
        metadata=metadata(task_state="archived"),
        now=datetime(2026, 5, 20, tzinfo=timezone.utc),
    )

    assert result.state == "blocked-active"
    assert result.gates["task_terminal_state_done_cancelled_superseded"] is False


def test_nested_repo_node_modules_can_be_safe_artifact_candidate(tmp_path, monkeypatch):
    mod = load_janitor()
    hermes_home = tmp_path / ".hermes"
    workspaces = hermes_home / "kanban" / "workspaces"
    workspace = workspaces / "BO-081"
    repo = workspace / "dailychingu"
    (repo / ".git").mkdir(parents=True)
    artifact = repo / "node_modules"
    artifact.mkdir()
    (artifact / "pkg.js").write_text("x")
    db_path = tmp_path / "kanban.db"
    create_kanban_db(
        db_path,
        [{
            "id": "task-81",
            "public_id": "BO-081",
            "status": "done",
            "completed_at": 1_778_342_400,
            "workspace_path": str(workspace),
            "closeout_evidence": {"final_summary": "done", "tests": ["unit"]},
        }],
    )

    git_paths = []

    def fake_git_state(path):
        git_paths.append(path)
        return False, False

    monkeypatch.setattr(mod, "git_state", fake_git_state)

    args = mod.build_parser().parse_args([
        "--format", "json",
        "--hermes-home", str(hermes_home),
        "--workspaces-root", str(workspaces),
        "--worktrees-root", str(tmp_path / "worktrees"),
        "--sessions-root", str(tmp_path / "sessions"),
        "--tmp-root", str(tmp_path / "tmp"),
        "--docker-root", str(tmp_path / "docker"),
        "--containerd-root", str(tmp_path / "containerd"),
        "--kanban-db", str(db_path),
        "--now", "2026-05-20T00:00:00Z",
        "--no-live-checks",
    ])

    report = mod.build_report(args)

    workspace_report = report["kanban_workspaces"][0]
    assert git_paths == [repo]
    assert workspace_report["repo_discovery"]["selected_repo_root"] == str(repo)
    assert workspace_report["metadata_evidence"]["git_state_source"] == str(repo)
    assert workspace_report["artifact_discovery"]["artifact_paths"] == [str(artifact)]
    assert workspace_report["artifact_candidates"][0]["path"] == str(artifact)
    assert workspace_report["artifact_candidates"][0]["state"] == "safe-artifact-candidate"
    assert report["candidate_counts"]["safe-artifact-candidate"] == 1


def test_multiple_nested_repos_make_git_state_unknown_and_approval_required(tmp_path, monkeypatch):
    mod = load_janitor()
    hermes_home = tmp_path / ".hermes"
    workspaces = hermes_home / "kanban" / "workspaces"
    workspace = workspaces / "BO-082"
    (workspace / "repo-a" / ".git").mkdir(parents=True)
    (workspace / "repo-b" / ".git").mkdir(parents=True)
    artifact = workspace / "node_modules"
    artifact.mkdir()
    db_path = tmp_path / "kanban.db"
    create_kanban_db(
        db_path,
        [{
            "id": "task-82",
            "public_id": "BO-082",
            "status": "done",
            "completed_at": 1_778_342_400,
            "workspace_path": str(workspace),
            "closeout_evidence": {"final_summary": "done"},
        }],
    )

    def fail_if_called(path):  # pragma: no cover - asserted by no call
        raise AssertionError(f"git_state should not be called for ambiguous repos: {path}")

    monkeypatch.setattr(mod, "git_state", fail_if_called)

    args = mod.build_parser().parse_args([
        "--format", "json",
        "--hermes-home", str(hermes_home),
        "--workspaces-root", str(workspaces),
        "--worktrees-root", str(tmp_path / "worktrees"),
        "--sessions-root", str(tmp_path / "sessions"),
        "--tmp-root", str(tmp_path / "tmp"),
        "--docker-root", str(tmp_path / "docker"),
        "--containerd-root", str(tmp_path / "containerd"),
        "--kanban-db", str(db_path),
        "--now", "2026-05-20T00:00:00Z",
        "--no-live-checks",
    ])

    report = mod.build_report(args)

    workspace_report = report["kanban_workspaces"][0]
    assert workspace_report["repo_discovery"]["status"] == "multiple"
    assert workspace_report["metadata"]["git_dirty"] is None
    assert workspace_report["metadata"]["important_untracked"] is None
    assert (
        workspace_report["metadata_evidence"]["git_state_reason"]
        == "multiple repository roots found; git state is ambiguous"
    )
    artifact_candidate = workspace_report["artifact_candidates"][0]
    assert artifact_candidate["path"] == str(artifact)
    assert artifact_candidate["state"] == "approval-required"
    assert "git dirty state is dirty or unknown" in "; ".join(artifact_candidate["reasons"])


def test_missing_repo_root_keeps_git_state_unknown_and_approval_required(tmp_path, monkeypatch):
    mod = load_janitor()
    hermes_home = tmp_path / ".hermes"
    workspaces = hermes_home / "kanban" / "workspaces"
    workspace = workspaces / "BO-084"
    artifact = workspace / "node_modules"
    artifact.mkdir(parents=True)
    db_path = tmp_path / "kanban.db"
    create_kanban_db(
        db_path,
        [{
            "id": "task-84",
            "public_id": "BO-084",
            "status": "done",
            "completed_at": 1_778_342_400,
            "workspace_path": str(workspace),
            "closeout_evidence": {"final_summary": "done"},
        }],
    )

    def fail_if_called(path):  # pragma: no cover - asserted by no call
        raise AssertionError(f"git_state should not be called without a repo root: {path}")

    monkeypatch.setattr(mod, "git_state", fail_if_called)

    args = mod.build_parser().parse_args([
        "--format", "json",
        "--hermes-home", str(hermes_home),
        "--workspaces-root", str(workspaces),
        "--worktrees-root", str(tmp_path / "worktrees"),
        "--sessions-root", str(tmp_path / "sessions"),
        "--tmp-root", str(tmp_path / "tmp"),
        "--docker-root", str(tmp_path / "docker"),
        "--containerd-root", str(tmp_path / "containerd"),
        "--kanban-db", str(db_path),
        "--now", "2026-05-20T00:00:00Z",
        "--no-live-checks",
    ])

    report = mod.build_report(args)

    workspace_report = report["kanban_workspaces"][0]
    assert workspace_report["repo_discovery"]["status"] == "none"
    assert workspace_report["metadata"]["git_dirty"] is None
    assert workspace_report["metadata"]["important_untracked"] is None
    assert (
        workspace_report["metadata_evidence"]["git_state_reason"]
        == "no repository root found at workspace root or direct children"
    )
    artifact_candidate = workspace_report["artifact_candidates"][0]
    assert artifact_candidate["state"] == "approval-required"
    assert "git dirty state is dirty or unknown" in "; ".join(artifact_candidate["reasons"])


def test_large_nested_non_allowlisted_dir_is_not_artifact_candidate(tmp_path, monkeypatch):
    mod = load_janitor()
    hermes_home = tmp_path / ".hermes"
    workspaces = hermes_home / "kanban" / "workspaces"
    workspace = workspaces / "BO-083"
    repo = workspace / "dailychingu"
    (repo / ".git").mkdir(parents=True)
    non_allowlisted = repo / "uploaded-assets"
    non_allowlisted.mkdir()
    (non_allowlisted / "blob.bin").write_text("x" * 1024)
    db_path = tmp_path / "kanban.db"
    create_kanban_db(
        db_path,
        [{
            "id": "task-83",
            "public_id": "BO-083",
            "status": "done",
            "completed_at": 1_778_342_400,
            "workspace_path": str(workspace),
            "closeout_evidence": {"final_summary": "done"},
        }],
    )
    monkeypatch.setattr(mod, "git_state", lambda path: (False, False))

    args = mod.build_parser().parse_args([
        "--format", "json",
        "--hermes-home", str(hermes_home),
        "--workspaces-root", str(workspaces),
        "--worktrees-root", str(tmp_path / "worktrees"),
        "--sessions-root", str(tmp_path / "sessions"),
        "--tmp-root", str(tmp_path / "tmp"),
        "--docker-root", str(tmp_path / "docker"),
        "--containerd-root", str(tmp_path / "containerd"),
        "--kanban-db", str(db_path),
        "--large-threshold-bytes", "1",
        "--now", "2026-05-20T00:00:00Z",
        "--no-live-checks",
    ])

    report = mod.build_report(args)

    workspace_report = report["kanban_workspaces"][0]
    assert workspace_report["artifact_discovery"]["artifact_paths"] == []
    assert workspace_report["artifact_candidates"] == []
    assert non_allowlisted.name not in {
        Path(path).name for path in workspace_report["artifact_discovery"]["artifact_paths"]
    }
