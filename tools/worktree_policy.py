"""Dedicated-worktree policy helpers for repo-mutating execution.

This module intentionally stays separate from gateway/work_state.  It only
answers a narrow question: when a path/command targets a git checkout, should
mutable execution be forced into a managed worktree under ``.worktrees/``?
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


_MUTATING_COMMAND_RE = re.compile(
    r"""^\s*(?:\(+\s*)?(?:sudo\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*(?:
        git(?:\s+-C\s+\S+)?\s+(?:
            add|commit|merge|pull|rebase|cherry-pick|am|rm|mv|clean|reset|stash|clone|checkout|switch|worktree\s+(?:add|move|remove|prune)|
            apply(?!\s+--check\b)|
            branch(?!\s+--(?:show-current|list)\b)|
            restore(?!\s+--source\b)
        )\b|
        (?:rm|mv|cp|install|mkdir|touch)\b|
        (?:printf|echo)\b.*>>?\s*\S+|
        tee\b|
        sed\s+-[^\n\r;|&]*i\b|
        perl\s+-[^\n\r;|&]*i\b
    )""",
    re.IGNORECASE | re.VERBOSE,
)

_READ_ONLY_GIT_COMMAND_RE = re.compile(
    r"""^\s*(?:\(+\s*)?(?:sudo\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*git(?:\s+-C\s+\S+)?\s+(?:
        status|diff|show|log|rev-parse|fetch|remote|ls-files|\
        branch\s+--(?:show-current|list)\b|\
        worktree\s+list\b|\
        apply\s+--check\b
    )\b.*$""",
    re.IGNORECASE | re.VERBOSE,
)

_COMMAND_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\n")


def _normalized_policy_path(
    path_value: str | Path,
    *,
    cwd_hint: str | Path | None = None,
    host_cwd_hint: str | Path | None = None,
) -> Path:
    raw = Path(path_value).expanduser()
    if raw.is_absolute():
        return raw.resolve(strict=False)

    base = Path(host_cwd_hint or cwd_hint or os.getcwd()).expanduser()
    return (base / raw).resolve(strict=False)


def _find_managed_worktree_root(path_value: str | Path) -> Optional[Path]:
    current = _normalized_policy_path(path_value)
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        parent = candidate.parent
        if parent.name != ".worktrees":
            continue
        repo_root = parent.parent
        if not (repo_root / ".git").exists():
            continue
        if not (candidate / ".git").exists():
            continue
        return candidate
    return None


def _find_checkout_root(
    path_value: str | Path,
    *,
    cwd_hint: str | Path | None = None,
    host_cwd_hint: str | Path | None = None,
) -> Optional[Path]:
    """Return the base git checkout root when *path_value* lives inside one."""

    if not path_value:
        return None

    managed_worktree_root = _find_managed_worktree_root(
        _normalized_policy_path(path_value, cwd_hint=cwd_hint, host_cwd_hint=host_cwd_hint)
    )
    if managed_worktree_root is not None:
        return managed_worktree_root.parent.parent

    current = _normalized_policy_path(path_value, cwd_hint=cwd_hint, host_cwd_hint=host_cwd_hint)
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def is_path_in_managed_worktree(
    path_value: str | Path,
    *,
    cwd_hint: str | Path | None = None,
    host_cwd_hint: str | Path | None = None,
) -> bool:
    managed_worktree_root = _find_managed_worktree_root(
        _normalized_policy_path(path_value, cwd_hint=cwd_hint, host_cwd_hint=host_cwd_hint)
    )
    if managed_worktree_root is None:
        return False

    resolved = _normalized_policy_path(path_value, cwd_hint=cwd_hint, host_cwd_hint=host_cwd_hint)
    if resolved.is_file():
        resolved = resolved.parent

    try:
        resolved.relative_to(managed_worktree_root)
        return True
    except ValueError:
        return False


def requires_dedicated_worktree_for_path(
    path_value: str | Path,
    *,
    cwd_hint: str | Path | None = None,
    host_cwd_hint: str | Path | None = None,
) -> bool:
    checkout_root = _find_checkout_root(path_value, cwd_hint=cwd_hint, host_cwd_hint=host_cwd_hint)
    if checkout_root is None:
        return False
    return not is_path_in_managed_worktree(path_value, cwd_hint=cwd_hint, host_cwd_hint=host_cwd_hint)


def command_is_repo_mutating(command: str) -> bool:
    if not command or not str(command).strip():
        return False

    for segment in _COMMAND_SEGMENT_SPLIT_RE.split(command):
        candidate = segment.strip()
        if not candidate:
            continue
        if _READ_ONLY_GIT_COMMAND_RE.match(candidate):
            continue
        if _MUTATING_COMMAND_RE.match(candidate):
            return True
    return False


def build_dedicated_worktree_error(target: str | Path) -> str:
    return (
        f"Repo-mutating execution is blocked in the base checkout ({target}). "
        "Use a dedicated worktree under .worktrees/ (for example `hermes -w` or an explicit workdir inside `.worktrees/...`) before mutating repo files."
    )
