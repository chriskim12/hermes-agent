#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def main() -> int:
    root_result = _git("rev-parse", "--show-toplevel")
    if root_result.returncode != 0:
        return 0
    repo_root = Path(root_result.stdout.strip()).resolve()
    policy_path = repo_root / ".hermes" / "repo-policy.yaml"
    if not policy_path.exists():
        return 0
    try:
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"repo-policy guard: cannot read {policy_path}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(policy, dict):
        return 0
    guard = policy.get("guard")
    if not isinstance(guard, dict) or guard.get("mutation_on_canonical_checkout") != "block":
        return 0
    canonical_raw = guard.get("canonical_checkout")
    protected = guard.get("protected_branches")
    if not isinstance(canonical_raw, str) or not isinstance(protected, list):
        print("repo-policy guard: malformed guard config", file=sys.stderr)
        return 1
    canonical = Path(canonical_raw).expanduser().resolve(strict=False)
    branch_result = _git("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else ""
    if branch in set(str(x) for x in protected) and _is_relative_to(repo_root, canonical):
        repo = policy.get("repo") if isinstance(policy.get("repo"), dict) else {}
        repo_name = repo.get("name") or repo_root.name
        print(
            "Repo-policy hardblock: commit/push from protected canonical checkout is blocked. "
            f"repo={repo_name}; canonical_checkout={canonical}; branch={branch}; "
            "use a task-owned worktree/branch instead.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
