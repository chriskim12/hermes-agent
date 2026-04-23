"""Repo-specific release-close policy helpers.

Start narrow: DailyChingu currently requires `develop -> main` release truth and
post-release local/remote sync on `main`.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Optional

_DAILYCHINGU_REPO_NAME = "dailychingu"
_DAILYCHINGU_INTEGRATION_BRANCH = "develop"
_DAILYCHINGU_PRODUCTION_BRANCH = "main"
_DAILYCHINGU_REMOTE = "origin"


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _resolve_repo_root(path_value: str | Path) -> Optional[Path]:
    candidate = Path(path_value).expanduser().resolve(strict=False)
    if candidate.is_file():
        candidate = candidate.parent
    result = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve(strict=False)


def _dailychingu_policy_applies(repo_root: Path) -> bool:
    return repo_root.name == _DAILYCHINGU_REPO_NAME


def _git_ref_exists(repo_root: Path, ref_name: str) -> bool:
    return _run_git(repo_root, "rev-parse", "--verify", "--quiet", ref_name).returncode == 0


def _command_targets_production_push(command: str) -> bool:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = command.split()
    if len(tokens) < 4 or tokens[0] != "git" or tokens[1] != "push":
        return False

    push_args = tokens[2:]
    for arg in push_args:
        if arg.startswith("-"):
            continue
        if arg in {_DAILYCHINGU_PRODUCTION_BRANCH, f"refs/heads/{_DAILYCHINGU_PRODUCTION_BRANCH}"}:
            return True
        if arg.endswith(f":{_DAILYCHINGU_PRODUCTION_BRANCH}") or arg.endswith(
            f":refs/heads/{_DAILYCHINGU_PRODUCTION_BRANCH}"
        ):
            return True
    return False


def release_close_blockers(repo_path: str | Path) -> list[str]:
    repo_root = _resolve_repo_root(repo_path)
    if repo_root is None or not _dailychingu_policy_applies(repo_root):
        return []

    blockers: list[str] = []
    if _git_ref_exists(repo_root, _DAILYCHINGU_INTEGRATION_BRANCH) and _git_ref_exists(repo_root, _DAILYCHINGU_PRODUCTION_BRANCH):
        merged = _run_git(
            repo_root,
            "merge-base",
            "--is-ancestor",
            _DAILYCHINGU_INTEGRATION_BRANCH,
            _DAILYCHINGU_PRODUCTION_BRANCH,
        )
        if merged.returncode != 0:
            blockers.append("release_path_missing_develop_to_main")

    remote_ref = f"{_DAILYCHINGU_REMOTE}/{_DAILYCHINGU_PRODUCTION_BRANCH}"
    if _git_ref_exists(repo_root, remote_ref) and _git_ref_exists(repo_root, _DAILYCHINGU_PRODUCTION_BRANCH):
        sync = _run_git(
            repo_root,
            "rev-list",
            "--left-right",
            "--count",
            f"{remote_ref}...{_DAILYCHINGU_PRODUCTION_BRANCH}",
        )
        if sync.returncode == 0 and sync.stdout.strip() != "0\t0":
            blockers.append("local_main_not_fast_forward_synced_to_origin_main")

    return blockers


def build_release_push_block_error(repo_path: str | Path, command: str) -> Optional[str]:
    repo_root = _resolve_repo_root(repo_path)
    if repo_root is None or not _dailychingu_policy_applies(repo_root):
        return None
    if not _command_targets_production_push(command):
        return None

    blockers = release_close_blockers(repo_root)
    if "release_path_missing_develop_to_main" not in blockers:
        return None

    return (
        "DailyChingu production push is blocked: release must go through "
        "`develop -> main` first. Merge/fast-forward `develop` into `main` "
        "before pushing `main`."
    )
