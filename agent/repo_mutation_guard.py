from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from agent.repo_policy import POLICY_RELATIVE_PATH
from agent.tool_dispatch_helpers import _is_destructive_command, _extract_file_mutation_targets


_FILE_MUTATION_TOOLS = {"write_file", "patch"}
_TERMINAL_TOOL = "terminal"


@dataclass(frozen=True)
class MutationGuardBlock:
    repo_name: str
    repo_path: Path
    canonical_checkout: Path
    branch: str
    target_path: Path
    tool_name: str

    def message(self) -> str:
        return (
            "Repo-policy hardblock: refusing to mutate protected canonical checkout. "
            f"repo={self.repo_name}; canonical_checkout={self.canonical_checkout}; "
            f"target={self.target_path}; branch={self.branch}; tool={self.tool_name}. "
            "Use a task-owned worktree/branch instead."
        )


def _run_git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_root_for_path(path: Path) -> Path | None:
    probe = path if path.is_dir() else path.parent
    probe = probe.expanduser()
    if not probe.exists():
        # For not-yet-created files, walk up to the nearest existing parent.
        for parent in [probe, *probe.parents]:
            if parent.exists():
                probe = parent
                break
    result = _run_git(probe, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root).resolve() if root else None


def _load_policy(repo_root: Path) -> dict[str, Any] | None:
    policy_path = repo_root / POLICY_RELATIVE_PATH
    if not policy_path.exists():
        return None
    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def _head_branch(repo_root: Path) -> str | None:
    result = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _normalize_target(raw: str | os.PathLike[str]) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    # resolve(strict=False) keeps not-yet-created leaf paths usable.
    return p.resolve(strict=False)


def _guard_block_for_path(path: Path, tool_name: str) -> MutationGuardBlock | None:
    repo_root = _git_root_for_path(path)
    if repo_root is None:
        return None
    policy = _load_policy(repo_root)
    if not policy:
        return None
    guard = policy.get("guard")
    if not isinstance(guard, dict):
        return None
    if guard.get("mutation_on_canonical_checkout") != "block":
        return None
    canonical_raw = guard.get("canonical_checkout")
    protected = guard.get("protected_branches")
    if not isinstance(canonical_raw, str) or not canonical_raw.strip():
        return None
    if not isinstance(protected, list) or not all(isinstance(x, str) for x in protected):
        return None
    canonical = Path(canonical_raw).expanduser().resolve(strict=False)
    branch = _head_branch(repo_root)
    if branch not in set(protected):
        return None
    # The guard is deliberately path + branch based. Worktrees for the same git
    # repo are allowed unless their actual path is the configured canonical checkout.
    if not _is_relative_to(path, canonical):
        return None
    repo = policy.get("repo") if isinstance(policy.get("repo"), dict) else {}
    repo_name = str(repo.get("name") or repo_root.name)
    return MutationGuardBlock(
        repo_name=repo_name,
        repo_path=repo_root,
        canonical_checkout=canonical,
        branch=branch,
        target_path=path,
        tool_name=tool_name,
    )


def _terminal_targets(args: dict[str, Any]) -> Iterable[Path]:
    command = args.get("command")
    if not isinstance(command, str) or not _is_destructive_command(command):
        return []
    workdir = args.get("workdir")
    base = _normalize_target(workdir) if isinstance(workdir, str) and workdir.strip() else Path.cwd().resolve()
    targets: list[Path] = [base]

    # Capture simple destructive command path operands and overwrite redirects.
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = []
    mutating_commands = {"rm", "rmdir", "cp", "install", "mv", "truncate", "dd"}
    for i, token in enumerate(tokens):
        if token in mutating_commands:
            for candidate in tokens[i + 1 : i + 4]:
                if candidate.startswith("-") or "=" in candidate:
                    continue
                if candidate in {"&&", "||", ";", "|"}:
                    break
                targets.append((base / candidate).resolve(strict=False) if not Path(candidate).is_absolute() else Path(candidate).expanduser().resolve(strict=False))
        if token in {">", "1>"} and i + 1 < len(tokens):
            out = tokens[i + 1]
            targets.append((base / out).resolve(strict=False) if not Path(out).is_absolute() else Path(out).expanduser().resolve(strict=False))
    for match in re.finditer(r"(?<!>)>(?!>)\s*([^\s;&|]+)", command):
        out = match.group(1).strip().strip("'\"")
        if out:
            targets.append((base / out).resolve(strict=False) if not Path(out).is_absolute() else Path(out).expanduser().resolve(strict=False))
    return targets


def _mutation_targets(tool_name: str, args: dict[str, Any]) -> list[Path]:
    if tool_name in _FILE_MUTATION_TOOLS:
        return [_normalize_target(p) for p in _extract_file_mutation_targets(tool_name, args) if p]
    if tool_name == _TERMINAL_TOOL:
        return list(_terminal_targets(args))
    return []


def repo_policy_mutation_block_message(tool_name: str, args: dict[str, Any]) -> str | None:
    """Return a fail-closed block message for protected canonical mutations.

    v1 rule: ``canonical checkout + protected branch + mutation = block``.
    The policy lives in the target repo's ``.hermes/repo-policy.yaml`` under:

    ``guard.canonical_checkout``
    ``guard.protected_branches``
    ``guard.mutation_on_canonical_checkout: block``
    """
    if not isinstance(args, dict):
        return None
    for target in _mutation_targets(tool_name, args):
        block = _guard_block_for_path(target, tool_name)
        if block is not None:
            return block.message()
    return None


def guard_result_json(message: str) -> str:
    return json.dumps({"error": message, "blocked_by": "repo_policy_canonical_checkout_guard"}, ensure_ascii=False)
