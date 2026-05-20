#!/usr/bin/env python3
"""Report-first Kanban disk lifecycle janitor for BO-075.

This helper is intentionally read-only. It inventories the pressure surfaces
called out by BO-075 and classifies Kanban workspace/artifact candidates using
fail-closed lifecycle gates. It does not delete files, enable plugins, schedule
cron jobs, restart services, or prune Docker/containerd.

Future apply mode must use the audit manifest fields emitted by this report and
must keep dry-run/apply paths separate.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - script can still run outside package setup
    def get_hermes_home() -> Path:  # type: ignore[no-redef]
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


TERMINAL_STATES = {"Done", "Cancelled", "Superseded"}
TERMINAL_STATE_KEYS = {"done", "cancelled", "canceled", "superseded"}
ACTIVE_STATE_KEYS = {"running"}
ACTIVE_RUN_STATE_KEYS = {"running"}
ARTIFACT_TTL = timedelta(hours=48)
WORKSPACE_TTL = timedelta(days=7)
CANDIDATE_STATES = {
    "safe-artifact-candidate",
    "future-workspace-cleanup-candidate",
    "approval-required",
    "blocked-active",
}
AUDIT_MANIFEST_FIELD_DEFINITIONS = {
    "timestamp": "UTC ISO-8601 time when a future apply/report action is recorded.",
    "actor_or_job_id": "Human, agent, cron, or job identity responsible for the action.",
    "workspace_or_path": "Exact workspace or artifact path evaluated or acted on.",
    "kanban_task_or_card_id": "Kanban task/card id associated with the path, when known.",
    "candidate_state": "One of BO-075's candidate states at decision time.",
    "reason": "Human-readable reason for the classification or action.",
    "estimated_size_bytes": "Best-effort pre-action byte estimate for the candidate path.",
    "actual_reclaimed_size_bytes": "Measured bytes reclaimed by future apply mode; zero/null for reports.",
    "gates_evaluated": "Structured safety gate results used for the decision.",
    "action_taken": "Report-only, dry-run, artifact cleanup, workspace cleanup, or skipped action.",
    "approval_id": "Approval record id for approval-gated actions, when applicable.",
    "dry_run_or_report_id": "Identifier linking future apply actions back to the source report/dry-run.",
}
AUDIT_MANIFEST_FIELDS = list(AUDIT_MANIFEST_FIELD_DEFINITIONS)
ALLOWLISTED_ARTIFACT_NAMES = {
    "node_modules",
    ".next",
    "dist",
    "build",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "coverage",
    ".coverage",
    ".turbo",
    ".parcel-cache",
    ".vite",
}
BUILD_CACHE_NAMES = ALLOWLISTED_ARTIFACT_NAMES - {"node_modules", ".next"}


@dataclass(frozen=True)
class WorkspaceMetadata:
    """Lifecycle facts for a workspace.

    Unknown values intentionally fail closed during classification. Operators can
    pass these facts through ``--metadata-json``; absent metadata is reported as
    ``approval-required`` rather than auto-cleanable.
    """

    task_id: str | None = None
    public_id: str | None = None
    task_state: str | None = None
    terminal_since: datetime | None = None
    active_worker: bool = False
    active_run: bool = False
    active_run_status: str | None = None
    process_cwd_under_path: bool = False
    tmux_cwd_under_path: bool = False
    git_dirty: bool | None = None
    important_untracked: bool | None = None
    evidence_preserved: bool | None = None
    owner_known: bool = True
    metadata_source: str | None = None
    mapping_confidence: str = "manual"
    mapping_reasons: list[str] = field(default_factory=list)
    matched_task_count: int = 0
    non_allowlisted_large_files: bool = False


@dataclass(frozen=True)
class CandidateAssessment:
    path: str
    kind: str
    state: str
    reasons: list[str]
    gates: dict[str, Any]
    estimated_size_bytes: int = 0
    task_id: str | None = None

    @property
    def auto_cleanable(self) -> bool:
        return self.state in {
            "safe-artifact-candidate",
            "future-workspace-cleanup-candidate",
        }


@dataclass
class SizeBreakdown:
    path: str
    total_bytes: int = 0
    node_modules_bytes: int = 0
    next_bytes: int = 0
    build_test_cache_bytes: int = 0
    other_large_entries: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class SurfaceReport:
    path: str
    label: str
    exists: bool
    readable: bool
    total_bytes: int | None = None
    error: str | None = None


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _state_key(value: str | None) -> str | None:
    return value.strip().casefold() if value else None


def _is_terminal_task_state(value: str | None) -> bool:
    key = _state_key(value)
    return bool(key and key in TERMINAL_STATE_KEYS)


def _is_active_task_state(value: str | None) -> bool:
    key = _state_key(value)
    return bool(key and key in ACTIVE_STATE_KEYS)


def _datetime_from_epoch(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _json_dict(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _path_key(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def safe_tree_size(path: Path, *, max_errors: int = 8) -> tuple[int, list[str]]:
    """Return a best-effort tree size without following symlinked directories."""
    errors: list[str] = []
    if not path.exists():
        return 0, ["missing"]
    if path.is_file() or path.is_symlink():
        try:
            return path.lstat().st_size, []
        except OSError as exc:
            return 0, [str(exc)]

    total = 0
    for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        kept_dirs = []
        for dirname in dirs:
            child = root_path / dirname
            try:
                if not child.is_symlink():
                    kept_dirs.append(dirname)
                else:
                    total += child.lstat().st_size
            except OSError as exc:
                if len(errors) < max_errors:
                    errors.append(f"{child}: {exc}")
        dirs[:] = kept_dirs
        for filename in files:
            child = root_path / filename
            try:
                total += child.lstat().st_size
            except OSError as exc:
                if len(errors) < max_errors:
                    errors.append(f"{child}: {exc}")
    return total, errors


def direct_child_sizes(path: Path) -> list[tuple[Path, int]]:
    entries: list[tuple[Path, int]] = []
    try:
        children = list(path.iterdir())
    except OSError:
        return entries
    for child in children:
        size, _errors = safe_tree_size(child)
        entries.append((child, size))
    return sorted(entries, key=lambda item: item[1], reverse=True)


def workspace_size_breakdown(path: Path, *, large_threshold_bytes: int = 100 * 1024 * 1024) -> SizeBreakdown:
    total, errors = safe_tree_size(path)
    breakdown = SizeBreakdown(path=str(path), total_bytes=total, errors=errors)

    for child, size in direct_child_sizes(path):
        name = child.name
        if name == "node_modules":
            breakdown.node_modules_bytes += size
        elif name == ".next":
            breakdown.next_bytes += size
        elif name in BUILD_CACHE_NAMES:
            breakdown.build_test_cache_bytes += size
        elif size >= large_threshold_bytes:
            breakdown.other_large_entries.append({
                "path": str(child),
                "name": name,
                "bytes": size,
            })
    return breakdown


def load_metadata(path: Path | None) -> dict[str, WorkspaceMetadata]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw.get("workspaces", raw) if isinstance(raw, dict) else raw
    result: dict[str, WorkspaceMetadata] = {}
    for item in records:
        item = dict(item)
        workspace = str(Path(item.pop("path")).expanduser())
        item["terminal_since"] = parse_datetime(item.get("terminal_since"))
        result[workspace] = WorkspaceMetadata(**item)
    return result


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def _select_existing(conn: sqlite3.Connection, table: str, columns: list[str]) -> list[dict[str, Any]]:
    available = _table_columns(conn, table)
    selected = [column for column in columns if column in available]
    if not selected:
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT {', '.join(selected)} FROM {table}").fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def _extract_path_values(value: Any) -> list[str]:
    """Pull likely workspace paths from JSON-ish run metadata without trusting shape."""
    paths: list[str] = []

    def visit(obj: Any, key: str | None = None) -> None:
        if isinstance(obj, dict):
            for child_key, child in obj.items():
                visit(child, str(child_key))
            return
        if isinstance(obj, list):
            for child in obj:
                visit(child, key)
            return
        if not isinstance(obj, str):
            return
        lowered = (key or "").casefold()
        if lowered in {"workspace", "workspace_path", "workdir", "cwd", "path"} and "/" in obj:
            paths.append(obj)

    visit(value)
    return paths


def _evidence_preserved(task: dict[str, Any], runs: list[dict[str, Any]]) -> bool | None:
    evidence = _json_dict(task.get("closeout_evidence"))
    if evidence:
        return True
    result = task.get("result")
    if isinstance(result, str) and result.strip():
        return True
    for run in runs:
        summary = run.get("summary")
        if isinstance(summary, str) and summary.strip():
            return True
        metadata = _json_dict(run.get("metadata"))
        if metadata and any(key in metadata for key in ("evidence", "summary", "closeout_evidence", "final_summary")):
            return True
    return None


def _terminal_since(task: dict[str, Any], runs: list[dict[str, Any]]) -> datetime | None:
    for key in ("completed_at", "ended_at", "updated_at"):
        if key in task:
            parsed = _datetime_from_epoch(task.get(key))
            if parsed:
                return parsed
    ended_runs = [_datetime_from_epoch(run.get("ended_at")) for run in runs]
    ended_runs = [item for item in ended_runs if item is not None]
    return max(ended_runs) if ended_runs else None


def _task_runs_by_task(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    if not _table_exists(conn, "task_runs"):
        return {}
    rows = _select_existing(
        conn,
        "task_runs",
        [
            "id",
            "task_id",
            "status",
            "claim_lock",
            "claim_expires",
            "worker_pid",
            "last_heartbeat_at",
            "started_at",
            "ended_at",
            "outcome",
            "summary",
            "metadata",
            "error",
        ],
    )
    by_task: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        task_id = row.get("task_id")
        if task_id:
            by_task.setdefault(str(task_id), []).append(row)
    return by_task


def _metadata_from_task(
    task: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    db_path: Path,
    confidence: str,
    reasons: list[str],
    match_count: int = 1,
) -> WorkspaceMetadata:
    task_id = str(task.get("id")) if task.get("id") is not None else None
    status = str(task.get("status")) if task.get("status") is not None else None
    run_statuses = [str(run.get("status")) for run in runs if run.get("status") is not None]
    active_run_status = next((status for status in run_statuses if _state_key(status) in ACTIVE_RUN_STATE_KEYS), None)
    current_run_id = task.get("current_run_id")
    active_run = bool(active_run_status or (current_run_id and _state_key(status) == "running"))
    active_worker = bool(
        _is_active_task_state(status)
        or task.get("worker_pid")
        or task.get("claim_lock")
        or any(run.get("worker_pid") or run.get("claim_lock") for run in runs if _state_key(str(run.get("status"))) in ACTIVE_RUN_STATE_KEYS)
    )
    terminal_since = _terminal_since(task, runs) if _is_terminal_task_state(status) else None
    return WorkspaceMetadata(
        task_id=task_id,
        public_id=str(task.get("public_id")) if task.get("public_id") else None,
        task_state=status,
        terminal_since=terminal_since,
        active_worker=active_worker,
        active_run=active_run,
        active_run_status=active_run_status,
        evidence_preserved=_evidence_preserved(task, runs),
        owner_known=confidence == "exact",
        metadata_source=f"kanban-db:{db_path}",
        mapping_confidence=confidence,
        mapping_reasons=reasons,
        matched_task_count=match_count,
    )


def _ambiguous_metadata(path: Path, matches: list[tuple[dict[str, Any], list[dict[str, Any]], str]]) -> WorkspaceMetadata:
    task_ids = sorted(str(task.get("id")) for task, _runs, _reason in matches if task.get("id") is not None)
    return WorkspaceMetadata(
        owner_known=False,
        metadata_source="kanban-db",
        mapping_confidence="ambiguous",
        mapping_reasons=[
            f"workspace path matched multiple Kanban tasks: {', '.join(task_ids[:8])}",
        ],
        matched_task_count=len(matches),
    )


def _unique_matches(
    matches: list[tuple[dict[str, Any], list[dict[str, Any]], str]],
) -> list[tuple[dict[str, Any], list[dict[str, Any]], str]]:
    merged: dict[str, tuple[dict[str, Any], list[dict[str, Any]], set[str]]] = {}
    for task, runs, reason in matches:
        task_id = str(task.get("id")) if task.get("id") is not None else f"row:{id(task)}"
        if task_id not in merged:
            merged[task_id] = (task, runs, {reason})
        else:
            merged[task_id][2].add(reason)
    return [
        (task, runs, "; ".join(sorted(reasons)))
        for task, runs, reasons in merged.values()
    ]


def load_kanban_db_metadata(db_path: Path, workspaces: list[Path]) -> tuple[dict[str, WorkspaceMetadata], dict[str, Any]]:
    """Read Kanban metadata from SQLite without writes or schema migration.

    The adapter inspects table/column availability and returns partial metadata
    when possible. Missing tables/columns are reported and otherwise fail closed
    through unknown ``WorkspaceMetadata`` fields.
    """
    summary: dict[str, Any] = {
        "enabled": True,
        "source": str(db_path),
        "loaded": False,
        "tasks_seen": 0,
        "mapped_workspaces": 0,
        "ambiguous_workspaces": 0,
        "errors": [],
        "schema": {},
    }
    if not db_path.exists():
        summary["errors"].append("kanban db missing")
        return {}, summary

    try:
        uri = f"file:{db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        summary["errors"].append(f"open failed: {exc}")
        return {}, summary

    try:
        conn.row_factory = sqlite3.Row
        task_columns = _table_columns(conn, "tasks")
        summary["schema"]["tasks"] = sorted(task_columns)
        if not task_columns:
            summary["errors"].append("tasks table missing or unreadable")
            return {}, summary

        tasks = _select_existing(
            conn,
            "tasks",
            [
                "id",
                "public_id",
                "status",
                "completed_at",
                "started_at",
                "workspace_path",
                "claim_lock",
                "claim_expires",
                "worker_pid",
                "current_run_id",
                "closeout_evidence",
                "result",
            ],
        )
        runs_by_task = _task_runs_by_task(conn)
        summary["schema"]["task_runs"] = sorted(_table_columns(conn, "task_runs")) if _table_exists(conn, "task_runs") else []
    except sqlite3.Error as exc:
        summary["errors"].append(f"read failed: {exc}")
        return {}, summary
    finally:
        conn.close()

    summary["loaded"] = True
    summary["tasks_seen"] = len(tasks)

    explicit_by_path: dict[str, list[tuple[dict[str, Any], list[dict[str, Any]], str]]] = {}
    fallback_by_name: dict[str, list[tuple[dict[str, Any], list[dict[str, Any]], str]]] = {}
    for task in tasks:
        task_id = str(task.get("id")) if task.get("id") is not None else None
        runs = runs_by_task.get(task_id or "", [])
        workspace_path = task.get("workspace_path")
        if workspace_path:
            explicit_by_path.setdefault(_path_key(str(workspace_path)), []).append((task, runs, "tasks.workspace_path exact match"))
        for run in runs:
            for path_value in _extract_path_values(_json_dict(run.get("metadata"))):
                explicit_by_path.setdefault(_path_key(path_value), []).append((task, runs, "task_runs.metadata workspace path match"))
        for value_name in ("id", "public_id"):
            value = task.get(value_name)
            if value:
                fallback_by_name.setdefault(str(value), []).append((task, runs, f"workspace directory name matched task {value_name}"))

    metadata: dict[str, WorkspaceMetadata] = {}
    for workspace in workspaces:
        key = _path_key(workspace)
        matches = explicit_by_path.get(key, [])
        confidence = "exact"
        if not matches:
            matches = fallback_by_name.get(workspace.name, [])
            confidence = "fallback-name"
        matches = _unique_matches(matches)
        if not matches:
            continue
        if len(matches) > 1:
            metadata[str(workspace)] = _ambiguous_metadata(workspace, matches)
            summary["ambiguous_workspaces"] += 1
            continue
        task, runs, reason = matches[0]
        metadata[str(workspace)] = _metadata_from_task(
            task,
            runs,
            db_path=db_path,
            confidence=confidence,
            reasons=[reason],
        )
        summary["mapped_workspaces"] += 1
    return metadata, summary


def resolve_kanban_db_path(args: argparse.Namespace) -> Path:
    if args.kanban_db:
        return Path(args.kanban_db).expanduser()
    try:
        from hermes_cli.kanban_db import kanban_db_path

        return kanban_db_path(board=args.kanban_board)
    except Exception:
        return Path(args.hermes_home).expanduser() / "kanban.db"


def detect_process_cwd_under(path: Path) -> bool:
    proc = Path("/proc")
    if not proc.exists():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = (entry / "cwd").resolve(strict=True)
        except OSError:
            continue
        if _is_relative_to(cwd, path):
            return True
    return False


def detect_tmux_cwd_under(path: Path) -> bool:
    try:
        proc = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_current_path}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return any(_is_relative_to(Path(line.strip()), path) for line in proc.stdout.splitlines() if line.strip())


def git_state(path: Path) -> tuple[bool | None, bool | None]:
    if not (path / ".git").exists():
        return None, None
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain=v1"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if proc.returncode != 0:
        return None, None
    dirty = False
    important_untracked = False
    for line in proc.stdout.splitlines():
        if not line:
            continue
        dirty = True
        if line.startswith("?? "):
            untracked = line[3:]
            if not any(part in untracked.split("/") for part in ALLOWLISTED_ARTIFACT_NAMES):
                important_untracked = True
    return dirty, important_untracked


def classify_candidate(
    path: Path,
    *,
    kind: str,
    metadata: WorkspaceMetadata,
    now: datetime | None = None,
    estimated_size_bytes: int = 0,
) -> CandidateAssessment:
    """Classify a candidate using BO-075's exact candidate states.

    This function is intentionally conservative: unknown owner, unknown task
    state, dirty/untracked state, active references, or missing evidence all
    prevent auto-cleanable states.
    """
    if kind not in {"artifact", "workspace"}:
        raise ValueError("kind must be 'artifact' or 'workspace'")
    now = now or datetime.now(timezone.utc)
    path_name = path.name
    is_allowlisted_artifact = kind == "artifact" and path_name in ALLOWLISTED_ARTIFACT_NAMES
    terminal = _is_terminal_task_state(metadata.task_state)
    terminal_age = (now - metadata.terminal_since) if metadata.terminal_since else None

    gates: dict[str, Any] = {
        "task_terminal_state_done_cancelled_superseded": terminal,
        "terminal_since": metadata.terminal_since.isoformat() if metadata.terminal_since else None,
        "artifact_ttl_48h": bool(terminal_age is not None and terminal_age >= ARTIFACT_TTL),
        "workspace_ttl_7d": bool(terminal_age is not None and terminal_age >= WORKSPACE_TTL),
        "active_worker_or_run": bool(metadata.active_worker or metadata.active_run),
        "process_cwd_under_path": metadata.process_cwd_under_path,
        "tmux_pane_cwd_under_path": metadata.tmux_cwd_under_path,
        "git_dirty": metadata.git_dirty,
        "important_untracked_files": metadata.important_untracked,
        "evidence_summary_preserved": metadata.evidence_preserved,
        "owner_known": metadata.owner_known,
        "metadata_source": metadata.metadata_source,
        "mapping_confidence": metadata.mapping_confidence,
        "mapping_reasons": metadata.mapping_reasons,
        "matched_task_count": metadata.matched_task_count,
        "active_run_status": metadata.active_run_status,
        "allowlisted_reproducible_artifact": is_allowlisted_artifact,
        "non_allowlisted_large_files": metadata.non_allowlisted_large_files,
    }

    reasons: list[str] = []
    if metadata.active_worker or metadata.active_run:
        reasons.append("active worker/run references path")
    if metadata.process_cwd_under_path:
        reasons.append("process cwd under path")
    if metadata.tmux_cwd_under_path:
        reasons.append("tmux/pane cwd under path")
    if metadata.mapping_confidence == "ambiguous":
        approval_reason = "ambiguous Kanban task mapping"
    else:
        approval_reason = ""
    if metadata.task_state is not None and not terminal:
        reasons.append("task is not in terminal Done/Cancelled/Superseded state")
    if reasons:
        return CandidateAssessment(str(path), kind, "blocked-active", reasons, gates, estimated_size_bytes, metadata.task_id)

    approval_reasons: list[str] = []
    if approval_reason:
        approval_reasons.append(approval_reason)
    if not metadata.owner_known or not metadata.task_id:
        approval_reasons.append("unknown task owner or task id")
    if metadata.task_state is None:
        approval_reasons.append("unknown task state")
    if metadata.terminal_since is None:
        approval_reasons.append("unknown terminal-state age")
    if metadata.git_dirty is not False:
        approval_reasons.append("git dirty state is dirty or unknown")
    if metadata.important_untracked is not False:
        approval_reasons.append("important untracked files present or unknown")
    if metadata.evidence_preserved is not True:
        approval_reasons.append("evidence/summary preservation missing or unknown")
    if kind == "artifact" and not is_allowlisted_artifact:
        approval_reasons.append("artifact path is not allowlisted as reproducible")
    if kind == "artifact" and terminal_age is not None and terminal_age < ARTIFACT_TTL:
        approval_reasons.append("artifact TTL below 48h")
    if kind == "workspace" and terminal_age is not None and terminal_age < WORKSPACE_TTL:
        approval_reasons.append("workspace TTL below 7d")
    if metadata.non_allowlisted_large_files:
        approval_reasons.append("workspace contains non-allowlisted large files")
    if approval_reasons:
        return CandidateAssessment(str(path), kind, "approval-required", approval_reasons, gates, estimated_size_bytes, metadata.task_id)

    if kind == "artifact":
        return CandidateAssessment(
            str(path),
            kind,
            "safe-artifact-candidate",
            ["terminal workspace artifact satisfies 48h TTL and safety gates"],
            gates,
            estimated_size_bytes,
            metadata.task_id,
        )
    return CandidateAssessment(
        str(path),
        kind,
        "future-workspace-cleanup-candidate",
        ["terminal workspace satisfies 7d TTL, clean git, preserved evidence, and active-reference gates"],
        gates,
        estimated_size_bytes,
        metadata.task_id,
    )


def surface_report(label: str, path: Path) -> SurfaceReport:
    exists = path.exists()
    readable = os.access(path, os.R_OK) if exists else False
    if not exists:
        return SurfaceReport(str(path), label, False, False, error="missing")
    if not readable:
        return SurfaceReport(str(path), label, True, False, error="not readable")
    total, errors = safe_tree_size(path)
    return SurfaceReport(str(path), label, True, True, total_bytes=total, error="; ".join(errors) if errors else None)


def discover_workspaces(workspaces_root: Path) -> list[Path]:
    if not workspaces_root.exists() or not workspaces_root.is_dir():
        return []
    try:
        return sorted(child for child in workspaces_root.iterdir() if child.is_dir() and not child.is_symlink())
    except OSError:
        return []


def enrich_metadata(path: Path, metadata: WorkspaceMetadata, *, live_checks: bool) -> WorkspaceMetadata:
    dirty = metadata.git_dirty
    important_untracked = metadata.important_untracked
    if dirty is None or important_untracked is None:
        detected_dirty, detected_untracked = git_state(path)
        dirty = dirty if dirty is not None else detected_dirty
        important_untracked = important_untracked if important_untracked is not None else detected_untracked
    if not live_checks:
        return WorkspaceMetadata(
            **{**asdict(metadata), "git_dirty": dirty, "important_untracked": important_untracked}
        )
    return WorkspaceMetadata(
        **{
            **asdict(metadata),
            "git_dirty": dirty,
            "important_untracked": important_untracked,
            "process_cwd_under_path": metadata.process_cwd_under_path or detect_process_cwd_under(path),
            "tmux_cwd_under_path": metadata.tmux_cwd_under_path or detect_tmux_cwd_under(path),
        }
    )


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    workspaces_root = Path(args.workspaces_root).expanduser()
    worktrees_root = Path(args.worktrees_root).expanduser()
    sessions_root = Path(args.sessions_root).expanduser()
    now = parse_datetime(args.now) or datetime.now(timezone.utc)
    metadata_by_path = load_metadata(Path(args.metadata_json)) if args.metadata_json else {}

    surfaces = [
        surface_report("kanban_workspaces", workspaces_root),
        surface_report("agent_worktrees", worktrees_root),
        surface_report("sessions", sessions_root),
        surface_report("tmp", Path(args.tmp_root)),
        surface_report("docker", Path(args.docker_root)),
        surface_report("containerd", Path(args.containerd_root)),
    ]

    discovered_workspaces = discover_workspaces(workspaces_root)
    kanban_metadata_summary: dict[str, Any] = {"enabled": False}
    if args.load_kanban_db or args.kanban_db:
        kanban_db_path = resolve_kanban_db_path(args)
        db_metadata, kanban_metadata_summary = load_kanban_db_metadata(kanban_db_path, discovered_workspaces)
        metadata_by_path.update(db_metadata)

    workspaces: list[dict[str, Any]] = []
    candidates: list[CandidateAssessment] = []
    for workspace in discovered_workspaces:
        raw_metadata = metadata_by_path.get(str(workspace), WorkspaceMetadata(owner_known=False))
        metadata = enrich_metadata(workspace, raw_metadata, live_checks=not args.no_live_checks)
        breakdown = workspace_size_breakdown(workspace, large_threshold_bytes=args.large_threshold_bytes)
        metadata = WorkspaceMetadata(
            **{
                **asdict(metadata),
                "non_allowlisted_large_files": metadata.non_allowlisted_large_files or bool(breakdown.other_large_entries),
            }
        )
        artifact_paths = [
            workspace / "node_modules",
            workspace / ".next",
            *(workspace / name for name in sorted(BUILD_CACHE_NAMES)),
        ]
        artifact_assessments = []
        for artifact in artifact_paths:
            if not artifact.exists():
                continue
            artifact_size, _ = safe_tree_size(artifact)
            assessment = classify_candidate(
                artifact,
                kind="artifact",
                metadata=metadata,
                now=now,
                estimated_size_bytes=artifact_size,
            )
            artifact_assessments.append(assessment)
            candidates.append(assessment)
        workspace_assessment = classify_candidate(
            workspace,
            kind="workspace",
            metadata=metadata,
            now=now,
            estimated_size_bytes=breakdown.total_bytes,
        )
        candidates.append(workspace_assessment)
        workspaces.append(
            {
                "path": str(workspace),
                "metadata": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in asdict(metadata).items()},
                "metadata_evidence": {
                    "source": metadata.metadata_source,
                    "mapping_confidence": metadata.mapping_confidence,
                    "mapping_reasons": metadata.mapping_reasons,
                    "matched_task_count": metadata.matched_task_count,
                    "owner_known": metadata.owner_known,
                },
                "size_breakdown": asdict(breakdown),
                "artifact_candidates": [asdict(item) | {"auto_cleanable": item.auto_cleanable} for item in artifact_assessments],
                "workspace_candidate": asdict(workspace_assessment) | {"auto_cleanable": workspace_assessment.auto_cleanable},
            }
        )

    candidate_counts = {state: 0 for state in sorted(CANDIDATE_STATES)}
    for candidate in candidates:
        candidate_counts[candidate.state] += 1

    top_pressure_surfaces = sorted(
        (asdict(item) for item in surfaces if item.total_bytes is not None),
        key=lambda item: item["total_bytes"],
        reverse=True,
    )
    root_usage = shutil.disk_usage("/")

    return {
        "schema": "kanban-disk-janitor-report/v1",
        "generated_at": now.isoformat(),
        "mode": "report-first-read-only",
        "safety": {
            "deletes_files": False,
            "enables_plugins": False,
            "creates_cron_jobs": False,
            "prunes_docker_or_containerd": False,
            "fail_closed": True,
        },
        "ttl_policy": {"artifact_ttl_hours": 48, "workspace_ttl_days": 7},
        "root_disk_usage": {
            "path": "/",
            "total_bytes": root_usage.total,
            "used_bytes": root_usage.used,
            "free_bytes": root_usage.free,
            "used_percent": round((root_usage.used / root_usage.total) * 100, 2) if root_usage.total else None,
        },
        "audit_manifest_field_definitions_for_future_apply_mode": AUDIT_MANIFEST_FIELD_DEFINITIONS,
        "audit_manifest_fields_for_future_apply_mode": AUDIT_MANIFEST_FIELDS,
        "kanban_metadata": kanban_metadata_summary,
        "surfaces": [asdict(item) for item in surfaces],
        "top_pressure_surfaces": top_pressure_surfaces,
        "kanban_workspaces": workspaces,
        "candidate_counts": candidate_counts,
    }


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def format_text_report(report: dict[str, Any]) -> str:
    lines = [
        "Kanban disk lifecycle janitor report (read-only)",
        f"Generated: {report['generated_at']}",
        "Safety: report-first only; no deletion, plugin enablement, cron, or Docker/containerd pruning.",
        f"Root disk: {format_bytes(report['root_disk_usage']['used_bytes'])} used / {format_bytes(report['root_disk_usage']['total_bytes'])} ({report['root_disk_usage']['used_percent']}%)",
        "",
        "Pressure surfaces:",
    ]
    for surface in report["surfaces"]:
        status = "readable" if surface["readable"] else surface.get("error") or "unreadable"
        lines.append(f"- {surface['label']}: {surface['path']} — {status}, {format_bytes(surface.get('total_bytes'))}")
    lines.extend(["", "Candidate counts:"])
    for state, count in report["candidate_counts"].items():
        lines.append(f"- {state}: {count}")
    kanban_metadata = report.get("kanban_metadata") or {}
    if kanban_metadata.get("enabled"):
        status = "loaded" if kanban_metadata.get("loaded") else "not loaded"
        lines.extend([
            "",
            f"Kanban metadata: {status} from {kanban_metadata.get('source')}",
            f"  tasks_seen={kanban_metadata.get('tasks_seen', 0)}, mapped_workspaces={kanban_metadata.get('mapped_workspaces', 0)}, ambiguous_workspaces={kanban_metadata.get('ambiguous_workspaces', 0)}",
        ])
        for error in kanban_metadata.get("errors", []):
            lines.append(f"  metadata warning: {error}")
    lines.extend(["", "Kanban workspaces:"])
    for workspace in report["kanban_workspaces"]:
        breakdown = workspace["size_breakdown"]
        candidate = workspace["workspace_candidate"]
        evidence = workspace.get("metadata_evidence") or {}
        lines.append(f"- {workspace['path']}: total {format_bytes(breakdown['total_bytes'])}; workspace state {candidate['state']}")
        lines.append(f"  metadata: source={evidence.get('source')}, confidence={evidence.get('mapping_confidence')}, matches={evidence.get('matched_task_count')}")
        lines.append(f"  node_modules={format_bytes(breakdown['node_modules_bytes'])}, .next={format_bytes(breakdown['next_bytes'])}, build/test caches={format_bytes(breakdown['build_test_cache_bytes'])}")
        if candidate["reasons"]:
            lines.append(f"  reasons: {'; '.join(candidate['reasons'])}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    hermes_home = get_hermes_home()
    parser = argparse.ArgumentParser(description="Read-only BO-075 Kanban disk lifecycle janitor report.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--metadata-json", help="Optional workspace lifecycle metadata fixture/report JSON.")
    parser.add_argument("--load-kanban-db", action="store_true", help="Read live Kanban task metadata from the local SQLite DB in read-only mode.")
    parser.add_argument("--kanban-db", help="Kanban SQLite DB path to read in read-only mode; implies --load-kanban-db.")
    parser.add_argument("--kanban-board", help="Optional Kanban board slug for repo-local DB path resolution.")
    parser.add_argument("--now", help="Override current UTC time for deterministic reports/tests.")
    parser.add_argument("--hermes-home", default=str(hermes_home))
    parser.add_argument("--workspaces-root", default=str(hermes_home / "kanban" / "workspaces"))
    parser.add_argument("--worktrees-root", default=str(hermes_home / "hermes-agent" / ".worktrees"))
    parser.add_argument("--sessions-root", default=str(hermes_home / "sessions"))
    parser.add_argument("--tmp-root", default="/tmp")
    parser.add_argument("--docker-root", default="/var/lib/docker")
    parser.add_argument("--containerd-root", default="/var/lib/containerd")
    parser.add_argument("--large-threshold-bytes", type=int, default=100 * 1024 * 1024)
    parser.add_argument("--no-live-checks", action="store_true", help="Skip /proc and tmux cwd checks; still performs git status when available.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(args)
    if args.format == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text_report(report))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
