from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable


class SessionSkillAutoCommit:
    """Track session-authored skill paths and create a fail-closed git checkpoint."""

    def __init__(self, hermes_home: Path, mode: str = "off", session_id: str | None = None):
        self.hermes_home = Path(hermes_home).resolve()
        self.skills_dir = (self.hermes_home / "skills").resolve()
        self.mode = str(mode or "off").strip().lower() or "off"
        self.session_id = session_id or ""
        self._touched_paths: set[Path] = set()
        self._preexisting_dirty_paths: set[Path] = set()
        self.last_result: dict | None = None

    @property
    def touched_paths(self) -> set[Path]:
        return set(self._touched_paths)

    def record_paths(self, paths: Iterable[str]) -> None:
        for raw in paths or []:
            if not raw:
                continue
            p = Path(str(raw)).expanduser()
            if not p.is_absolute():
                p = self.hermes_home / p
            self._touched_paths.add(p.resolve())

    def record_tool_result(self, result_json: str) -> None:
        try:
            payload = json.loads(result_json)
        except Exception:
            return
        if not isinstance(payload, dict) or not payload.get("success"):
            return
        touched = payload.get("touched_paths")
        if isinstance(touched, list):
            self.record_paths(touched)

    def note_skill_manage_attempt(self, function_args: dict) -> None:
        if self.mode != "session_end":
            return
        repo_root = self._resolve_repo_root()
        if repo_root is None:
            return
        for target in self._predict_skill_manage_targets(function_args):
            rel = self._normalize_single_allowed_path(repo_root, target)
            if rel is None:
                continue
            status = self._run_git(repo_root, "status", "--short", "--", rel, check=False)
            if status.stdout.strip():
                self._preexisting_dirty_paths.add(target.resolve())

    def finalize(self) -> dict:
        if self.mode != "session_end":
            self._touched_paths.clear()
            self._preexisting_dirty_paths.clear()
            self.last_result = {
                "status": "noop",
                "reason": "disabled",
                "mode": self.mode,
                "commit_created": False,
                "paths": [],
            }
            return self.last_result

        if not self._touched_paths:
            self._preexisting_dirty_paths.clear()
            self.last_result = {
                "status": "noop",
                "reason": "no_touched_paths",
                "mode": self.mode,
                "commit_created": False,
                "paths": [],
            }
            return self.last_result

        repo_root = self._resolve_repo_root()
        if repo_root is None:
            return self._skip("not_git_repo")

        allowed_paths = self._normalize_allowed_paths(repo_root)
        if isinstance(allowed_paths, dict):
            return self._skip(allowed_paths["reason"])

        if any(path.resolve() in self._preexisting_dirty_paths for path in self._touched_paths):
            return self._skip(
                "preexisting_dirty_touched_paths",
                repo_root=repo_root,
                paths=sorted(allowed_paths),
            )

        unsafe = self._unsafe_git_reason(repo_root)
        if unsafe:
            return self._skip(unsafe, repo_root=repo_root, paths=sorted(allowed_paths))

        pre_staged = self._git_paths(repo_root, "diff", "--cached", "--name-only", "-z")
        if any(not self._path_allowed(path, allowed_paths) for path in pre_staged):
            return self._skip(
                "preexisting_staged_paths_outside_allowlist",
                repo_root=repo_root,
                paths=sorted(allowed_paths),
            )

        self._run_git(repo_root, "add", "-A", "--", *sorted(allowed_paths))
        final_staged = self._git_paths(repo_root, "diff", "--cached", "--name-only", "-z")
        if any(not self._path_allowed(path, allowed_paths) for path in final_staged):
            self._run_git(repo_root, "reset", "-q", "HEAD", "--", *sorted(allowed_paths), check=False)
            return self._skip(
                "staged_paths_outside_allowlist",
                repo_root=repo_root,
                paths=sorted(allowed_paths),
            )

        if not final_staged:
            self._touched_paths.clear()
            self._preexisting_dirty_paths.clear()
            self.last_result = {
                "status": "noop",
                "reason": "no_staged_changes",
                "mode": self.mode,
                "commit_created": False,
                "repo_root": str(repo_root),
                "paths": sorted(allowed_paths),
            }
            return self.last_result

        message = "chore(skills): checkpoint session-authored skill updates"
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "commit", "-m", message],
            text=True,
            capture_output=True,
        )
        if completed.returncode != 0:
            self._run_git(repo_root, "reset", "-q", "HEAD", "--", *sorted(allowed_paths), check=False)
            return self._skip(
                "commit_failed",
                repo_root=repo_root,
                paths=sorted(allowed_paths),
                detail=(completed.stderr or completed.stdout).strip(),
            )

        commit_sha = self._run_git(repo_root, "rev-parse", "HEAD").stdout.strip()
        self._touched_paths.clear()
        self._preexisting_dirty_paths.clear()
        self.last_result = {
            "status": "committed",
            "reason": "committed",
            "mode": self.mode,
            "commit_created": True,
            "repo_root": str(repo_root),
            "paths": sorted(allowed_paths),
            "commit": commit_sha,
            "message": message,
        }
        return self.last_result

    def _normalize_allowed_paths(self, repo_root: Path) -> set[str] | dict:
        allowed: set[str] = set()
        for path in sorted(self._touched_paths):
            rel = self._normalize_single_allowed_path(repo_root, path)
            if rel is None:
                return {"reason": "path_outside_skills" if str(path.resolve()).startswith(str(self.skills_dir)) else "path_outside_repo"}
            allowed.add(rel)
        return allowed

    def _normalize_single_allowed_path(self, repo_root: Path, path: Path) -> str | None:
        resolved = path.resolve()
        try:
            rel = resolved.relative_to(repo_root).as_posix()
        except ValueError:
            return None
        if rel == "skills" or rel.startswith("skills/"):
            return rel
        return None

    def _predict_skill_manage_targets(self, function_args: dict) -> list[Path]:
        action = str(function_args.get("action") or "").strip().lower()
        name = str(function_args.get("name") or "").strip()
        if not action or not name:
            return []

        if action == "create":
            category = str(function_args.get("category") or "").strip()
            skill_dir = self.skills_dir / category / name if category else self.skills_dir / name
            return [skill_dir / "SKILL.md"]

        skill_dir = self._find_skill_dir_by_name(name)
        if skill_dir is None:
            return []

        if action == "edit":
            return [skill_dir / "SKILL.md"]
        if action == "patch":
            file_path = function_args.get("file_path")
            if file_path:
                target = self._resolve_supporting_target(skill_dir, file_path)
                return [] if target is None else [target]
            return [skill_dir / "SKILL.md"]
        if action == "delete":
            return [skill_dir]
        if action in {"write_file", "remove_file"}:
            file_path = function_args.get("file_path")
            if not file_path:
                return []
            target = self._resolve_supporting_target(skill_dir, file_path)
            return [] if target is None else [target]
        return []

    def _find_skill_dir_by_name(self, name: str) -> Path | None:
        if not self.skills_dir.exists():
            return None
        for skill_md in self.skills_dir.rglob("SKILL.md"):
            if skill_md.parent.name == name:
                return skill_md.parent.resolve()
        return None

    def _resolve_supporting_target(self, skill_dir: Path, file_path: str) -> Path | None:
        rel = Path(str(file_path))
        if rel.is_absolute() or any(part == ".." for part in rel.parts):
            return None
        target = (skill_dir / rel).resolve()
        try:
            target.relative_to(skill_dir.resolve())
        except ValueError:
            return None
        return target

    def _resolve_repo_root(self) -> Path | None:
        probe = subprocess.run(
            ["git", "-C", str(self.hermes_home), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
        )
        if probe.returncode != 0:
            return None
        return Path(probe.stdout.strip()).resolve()

    def _unsafe_git_reason(self, repo_root: Path) -> str | None:
        git_dir = self._run_git(repo_root, "rev-parse", "--absolute-git-dir").stdout.strip()
        git_dir_path = Path(git_dir)
        if (git_dir_path / "index.lock").exists():
            return "index_lock_present"
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
            candidate = git_dir_path / marker
            if candidate.exists():
                return marker.lower()
        for dirname in ("rebase-merge", "rebase-apply"):
            if (git_dir_path / dirname).exists():
                return dirname.replace("-", "_")
        conflicts = self._git_paths(repo_root, "diff", "--name-only", "--diff-filter=U", "-z")
        if conflicts:
            return "unmerged_conflicts"
        return None

    def _git_paths(self, repo_root: Path, *args: str) -> list[str]:
        out = self._run_git(repo_root, *args).stdout
        return [p for p in out.split("\0") if p]

    def _path_allowed(self, candidate: str, allowed_paths: set[str]) -> bool:
        for allowed in allowed_paths:
            if candidate == allowed or candidate.startswith(f"{allowed.rstrip('/')}/"):
                return True
        return False

    def _skip(self, reason: str, *, repo_root: Path | None = None, paths: list[str] | None = None, detail: str | None = None) -> dict:
        self._touched_paths.clear()
        self._preexisting_dirty_paths.clear()
        self.last_result = {
            "status": "skipped",
            "reason": reason,
            "mode": self.mode,
            "commit_created": False,
            "repo_root": str(repo_root) if repo_root else None,
            "paths": paths or [],
        }
        if detail:
            self.last_result["detail"] = detail
        return self.last_result

    def _run_git(self, repo_root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            text=True,
            capture_output=True,
        )
        if check and completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout).strip() or f"git {' '.join(args)} failed")
        return completed
