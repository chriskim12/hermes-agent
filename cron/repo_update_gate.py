from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home

ALLOWED_MODES = frozenset({"safe_auto_apply", "staged_sync", "report_only"})
DEFAULT_SILENT_MARKER = "[SILENT]"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY_PATH = REPO_ROOT / "ops" / "repo-upstream-watch.yaml"
DEFAULT_STATE_PATH = get_hermes_home() / "state" / "status-gate" / "repo-upstream-watch.json"


class InventoryError(ValueError):
    """Raised when the repo update inventory is malformed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entrypoint_defaults(inventory_path: Path) -> dict[str, str]:
    script_path = REPO_ROOT / "scripts" / "repo_update_gate.py"
    return {
        "check": f"python {script_path} check --inventory {inventory_path}",
        "apply_plan": f"python {script_path} apply-plan --inventory {inventory_path}",
    }


def _git(repo_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result


def _count_dirty(status_output: str) -> tuple[int, int]:
    modified = 0
    untracked = 0
    for raw_line in status_output.splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            modified += 1
    return modified, untracked


def _verify_inventory_repo(repo: dict[str, Any]) -> dict[str, Any]:
    required = ["name", "path", "mode", "base_remote", "base_branch", "smoke_test", "apply_window"]
    missing = [key for key in required if not repo.get(key)]
    if missing:
        raise InventoryError(f"repo entry missing required keys: {', '.join(missing)}")
    if repo["mode"] not in ALLOWED_MODES:
        raise InventoryError(
            f"repo '{repo['name']}' has unsupported mode '{repo['mode']}'. "
            f"Expected one of: {', '.join(sorted(ALLOWED_MODES))}"
        )
    normalized = dict(repo)
    normalized["path"] = str(Path(normalized["path"]).expanduser())
    if normalized.get("staging_worktree_path"):
        normalized["staging_worktree_path"] = str(Path(normalized["staging_worktree_path"]).expanduser())
    return normalized


def load_inventory(inventory_path: str | Path = DEFAULT_INVENTORY_PATH) -> dict[str, Any]:
    path = Path(inventory_path).expanduser().resolve()
    if not path.exists():
        raise InventoryError(f"inventory not found: {path}")

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise InventoryError("inventory must be a mapping")

    metadata = dict(payload.get("metadata") or {})
    metadata.setdefault("silent_marker", DEFAULT_SILENT_MARKER)
    metadata.setdefault("state_path", str(DEFAULT_STATE_PATH))
    entrypoints = dict(metadata.get("entrypoints") or {})
    defaults = _entrypoint_defaults(path)
    entrypoints.setdefault("check", defaults["check"])
    entrypoints.setdefault("apply_plan", defaults["apply_plan"])
    metadata["entrypoints"] = entrypoints

    repos = payload.get("repos")
    if not isinstance(repos, list) or not repos:
        raise InventoryError("inventory must define a non-empty repos list")

    normalized_repos = [_verify_inventory_repo(repo) for repo in repos]
    return {
        "inventory_path": str(path),
        "schema_version": payload.get("schema_version", 1),
        "metadata": metadata,
        "repos": normalized_repos,
    }


def _observe_repo(repo: dict[str, Any], *, fetch: bool = True) -> dict[str, Any]:
    repo_path = Path(repo["path"])
    base_ref = f"{repo['base_remote']}/{repo['base_branch']}"
    observed: dict[str, Any] = {
        "name": repo["name"],
        "path": str(repo_path),
        "mode": repo["mode"],
        "base_remote": repo["base_remote"],
        "base_branch": repo["base_branch"],
        "base_ref": base_ref,
        "smoke_test": repo["smoke_test"],
        "apply_window": repo["apply_window"],
        "direct_apply_allowed": False,
        "stage_worktree_path": repo.get("staging_worktree_path"),
        "staging_branch": repo.get("staging_branch"),
        "repo_exists": repo_path.exists(),
        "current_branch": None,
        "tracking_branch": None,
        "ahead": None,
        "behind": None,
        "modified_count": None,
        "untracked_count": None,
        "fetch_ok": False,
        "fetch_error": None,
    }
    if not repo_path.exists():
        observed["repo_status"] = "missing_repo"
        observed["apply_verdict"] = "blocked_missing_repo"
        return observed

    if fetch:
        fetch_result = _git(repo_path, "fetch", "--prune", repo["base_remote"], repo["base_branch"], check=False)
        observed["fetch_ok"] = fetch_result.returncode == 0
        if fetch_result.returncode != 0:
            observed["fetch_error"] = (fetch_result.stderr or fetch_result.stdout).strip()
            observed["repo_status"] = "fetch_failed"
            observed["apply_verdict"] = "blocked_fetch_failed"
            return observed
    else:
        observed["fetch_ok"] = True

    remote_ref = _git(repo_path, "show-ref", "--verify", f"refs/remotes/{base_ref}", check=False)
    if remote_ref.returncode != 0:
        observed["repo_status"] = "missing_base_ref"
        observed["apply_verdict"] = "blocked_missing_base_ref"
        return observed

    observed["current_branch"] = _git(repo_path, "branch", "--show-current").stdout.strip() or None

    tracking = _git(repo_path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    observed["tracking_branch"] = tracking.stdout.strip() if tracking.returncode == 0 else None

    status_output = _git(repo_path, "status", "--porcelain=v1").stdout
    modified_count, untracked_count = _count_dirty(status_output)
    observed["modified_count"] = modified_count
    observed["untracked_count"] = untracked_count

    counts = _git(repo_path, "rev-list", "--left-right", "--count", f"HEAD...{base_ref}").stdout.strip()
    ahead_str, behind_str = counts.split()
    observed["ahead"] = int(ahead_str)
    observed["behind"] = int(behind_str)

    if modified_count or untracked_count:
        observed["repo_status"] = "dirty_worktree"
    elif observed["current_branch"] != repo["base_branch"]:
        observed["repo_status"] = "branch_mismatch"
    elif observed["tracking_branch"] != base_ref:
        observed["repo_status"] = "tracking_mismatch"
    elif observed["ahead"]:
        observed["repo_status"] = "ahead_of_base"
    elif observed["behind"]:
        observed["repo_status"] = "behind_base"
    else:
        observed["repo_status"] = "clean"

    observed["apply_verdict"] = _apply_verdict(repo, observed)
    observed["direct_apply_allowed"] = observed["apply_verdict"] == "eligible_for_direct_apply"
    return observed


def _apply_verdict(repo: dict[str, Any], observed: dict[str, Any]) -> str:
    status = observed["repo_status"]

    if status == "missing_repo":
        return "blocked_missing_repo"
    if status == "fetch_failed":
        return "blocked_fetch_failed"
    if status == "missing_base_ref":
        return "blocked_missing_base_ref"

    if repo["mode"] == "report_only":
        return "blocked_report_only"

    if repo["mode"] == "staged_sync":
        if status == "dirty_worktree":
            return "blocked_dirty_worktree"
        return "stage_required"

    if repo.get("bootstrap_required"):
        return "blocked_bootstrap_required"
    if status == "clean":
        return "noop"
    if status == "behind_base":
        return "eligible_for_direct_apply"
    if status == "dirty_worktree":
        return "blocked_dirty_worktree"
    if status == "branch_mismatch":
        return "blocked_branch_mismatch"
    if status == "tracking_mismatch":
        return "blocked_tracking_mismatch"
    if status == "ahead_of_base":
        return "blocked_ahead_of_base"
    return "blocked_policy_unknown"


def _fingerprint_for_report(repos: list[dict[str, Any]]) -> str:
    digestable = [
        {
            "name": repo["name"],
            "repo_status": repo["repo_status"],
            "apply_verdict": repo["apply_verdict"],
            "current_branch": repo["current_branch"],
            "tracking_branch": repo["tracking_branch"],
            "ahead": repo["ahead"],
            "behind": repo["behind"],
            "modified_count": repo["modified_count"],
            "untracked_count": repo["untracked_count"],
        }
        for repo in repos
    ]
    raw = json.dumps(digestable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load_previous_state(state_path: Path) -> dict[str, Any] | None:
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_state(state_path: Path, *, fingerprint: str, repos: list[dict[str, Any]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": _now_iso(),
        "fingerprint": fingerprint,
        "repos": [
            {
                "name": repo["name"],
                "repo_status": repo["repo_status"],
                "apply_verdict": repo["apply_verdict"],
                "current_branch": repo["current_branch"],
                "tracking_branch": repo["tracking_branch"],
                "ahead": repo["ahead"],
                "behind": repo["behind"],
                "modified_count": repo["modified_count"],
                "untracked_count": repo["untracked_count"],
            }
            for repo in repos
        ],
    }
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_check(
    inventory_path: str | Path = DEFAULT_INVENTORY_PATH,
    *,
    fetch: bool = True,
    write_state: bool = True,
) -> dict[str, Any]:
    inventory = load_inventory(inventory_path)
    state_path = Path(inventory["metadata"]["state_path"]).expanduser()
    repos = [_observe_repo(repo, fetch=fetch) for repo in inventory["repos"]]
    fingerprint = _fingerprint_for_report(repos)
    previous_state = _load_previous_state(state_path)
    changed = previous_state is None or previous_state.get("fingerprint") != fingerprint
    if write_state:
        _write_state(state_path, fingerprint=fingerprint, repos=repos)

    overall_status = "clean" if all(repo["repo_status"] == "clean" for repo in repos) else "attention_required"
    return {
        "generated_at": _now_iso(),
        "inventory_path": inventory["inventory_path"],
        "state_path": str(state_path),
        "silent_marker": inventory["metadata"]["silent_marker"],
        "entrypoints": inventory["metadata"]["entrypoints"],
        "changed": changed,
        "status": overall_status,
        "repos": repos,
    }


def build_apply_plan(inventory_path: str | Path = DEFAULT_INVENTORY_PATH, *, fetch: bool = True) -> dict[str, Any]:
    check_report = run_check(inventory_path, fetch=fetch, write_state=False)
    apply_items = []
    for repo in check_report["repos"]:
        item = {
            "name": repo["name"],
            "path": repo["path"],
            "mode": repo["mode"],
            "repo_status": repo["repo_status"],
            "apply_verdict": repo["apply_verdict"],
            "direct_apply_allowed": repo["direct_apply_allowed"],
            "base_ref": repo["base_ref"],
            "smoke_test": repo["smoke_test"],
        }
        if repo.get("stage_worktree_path"):
            item["stage_worktree_path"] = repo["stage_worktree_path"]
        if repo.get("staging_branch"):
            item["staging_branch"] = repo["staging_branch"]
        if repo["apply_verdict"] == "eligible_for_direct_apply":
            item["next_action"] = (
                f"git -C {repo['path']} pull --ff-only {repo['base_remote']} {repo['base_branch']} && "
                f"cd {repo['path']} && {repo['smoke_test']}"
            )
        elif repo["apply_verdict"] == "stage_required":
            stage_path = repo.get("stage_worktree_path") or "<missing-stage-worktree-path>"
            stage_branch = repo.get("staging_branch") or "<missing-staging-branch>"
            item["next_action"] = (
                f"git -C {repo['path']} fetch --prune {repo['base_remote']} {repo['base_branch']} && "
                f"git -C {repo['path']} worktree add -B {stage_branch} {stage_path} {repo['base_ref']}"
            )
        apply_items.append(item)

    return {
        "generated_at": _now_iso(),
        "inventory_path": check_report["inventory_path"],
        "status": check_report["status"],
        "repos": apply_items,
    }


def format_cron_output(report: dict[str, Any]) -> str:
    if report.get("status") == "clean" or report.get("changed") is False:
        return str(report.get("silent_marker") or DEFAULT_SILENT_MARKER)
    return json.dumps(report, indent=2, sort_keys=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inventory-backed repo drift/apply gate skeleton")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser("check", help="Run read-only repo drift classification")
    check_parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY_PATH))
    check_parser.add_argument("--no-fetch", action="store_true", help="Skip git fetch before classification")
    check_parser.add_argument("--no-write-state", action="store_true", help="Do not update the state fingerprint file")
    check_parser.add_argument("--cron", action="store_true", help="Emit [SILENT] when the report is unchanged or clean")

    apply_parser = subparsers.add_parser("apply-plan", help="Render the non-mutating gated apply plan")
    apply_parser.add_argument("--inventory", default=str(DEFAULT_INVENTORY_PATH))
    apply_parser.add_argument("--no-fetch", action="store_true", help="Skip git fetch before planning")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        report = run_check(args.inventory, fetch=not args.no_fetch, write_state=not args.no_write_state)
        if args.cron:
            print(format_cron_output(report))
        else:
            print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if args.command == "apply-plan":
        print(json.dumps(build_apply_plan(args.inventory, fetch=not args.no_fetch), indent=2, sort_keys=True))
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
