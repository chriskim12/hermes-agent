"""Tests for session-authored skill auto-commit behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def _init_hermes_git_repo(hermes_home: Path) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    _git(hermes_home, "init", "-b", "main")
    _git(hermes_home, "config", "user.name", "Test User")
    _git(hermes_home, "config", "user.email", "test@example.com")

    skill = hermes_home / "skills" / "existing-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: existing-skill\ndescription: Existing skill.\n---\n\n# Existing\n",
        encoding="utf-8",
    )
    _git(hermes_home, "add", "skills/existing-skill/SKILL.md")
    _git(hermes_home, "commit", "-m", "chore: seed skills repo")


class TestSessionSkillAutoCommit:
    def test_finalize_commits_only_touched_skill_paths(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        _init_hermes_git_repo(hermes_home)

        touched = hermes_home / "skills" / "new-skill" / "SKILL.md"
        touched.parent.mkdir(parents=True, exist_ok=True)
        touched.write_text(
            "---\nname: new-skill\ndescription: New skill.\n---\n\n# New\n",
            encoding="utf-8",
        )

        unrelated = hermes_home / "scripts" / "scratch.py"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("print('stay dirty')\n", encoding="utf-8")

        from agent.skill_autocommit import SessionSkillAutoCommit

        manager = SessionSkillAutoCommit(
            hermes_home=hermes_home,
            mode="session_end",
            session_id="sess-1",
        )
        manager.record_paths([str(touched)])

        result = manager.finalize()

        assert result["status"] == "committed"
        assert result["commit_created"] is True
        assert "skills/new-skill/SKILL.md" in _git(hermes_home, "show", "--name-only", "--format=", "HEAD").splitlines()
        assert "scripts/scratch.py" not in _git(hermes_home, "show", "--name-only", "--format=", "HEAD").splitlines()
        status = _git(hermes_home, "status", "--short")
        assert "?? scripts/" in status

    def test_finalize_skips_when_preexisting_staged_paths_exist_outside_allowlist(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        _init_hermes_git_repo(hermes_home)

        touched = hermes_home / "skills" / "new-skill" / "SKILL.md"
        touched.parent.mkdir(parents=True, exist_ok=True)
        touched.write_text(
            "---\nname: new-skill\ndescription: New skill.\n---\n\n# New\n",
            encoding="utf-8",
        )

        staged_other = hermes_home / "scripts" / "already-staged.py"
        staged_other.parent.mkdir(parents=True, exist_ok=True)
        staged_other.write_text("print('staged')\n", encoding="utf-8")
        _git(hermes_home, "add", "scripts/already-staged.py")

        from agent.skill_autocommit import SessionSkillAutoCommit

        manager = SessionSkillAutoCommit(
            hermes_home=hermes_home,
            mode="session_end",
            session_id="sess-2",
        )
        manager.record_paths([str(touched)])

        result = manager.finalize()

        assert result["status"] == "skipped"
        assert result["reason"] == "preexisting_staged_paths_outside_allowlist"
        assert _git(hermes_home, "rev-list", "--count", "HEAD") == "1"
        staged = _git(hermes_home, "diff", "--cached", "--name-only").splitlines()
        assert staged == ["scripts/already-staged.py"]

    def test_finalize_skips_when_touched_skill_path_was_already_dirty(self, tmp_path):
        hermes_home = tmp_path / ".hermes"
        _init_hermes_git_repo(hermes_home)

        skill_md = hermes_home / "skills" / "existing-skill" / "SKILL.md"
        skill_md.write_text(
            "---\nname: existing-skill\ndescription: Existing skill.\n---\n\n# Existing\n\npreexisting dirty\n",
            encoding="utf-8",
        )
        _git(hermes_home, "add", "skills/existing-skill/SKILL.md")

        from agent.skill_autocommit import SessionSkillAutoCommit

        manager = SessionSkillAutoCommit(
            hermes_home=hermes_home,
            mode="session_end",
            session_id="sess-3",
        )
        manager.note_skill_manage_attempt({"action": "patch", "name": "existing-skill"})
        manager.record_paths([str(skill_md)])

        result = manager.finalize()

        assert result["status"] == "skipped"
        assert result["reason"] == "preexisting_dirty_touched_paths"
        assert _git(hermes_home, "rev-list", "--count", "HEAD") == "1"
        staged = _git(hermes_home, "diff", "--cached", "--name-only").splitlines()
        assert staged == ["skills/existing-skill/SKILL.md"]


def test_shutdown_memory_provider_finalizes_skill_autocommit(monkeypatch, tmp_path):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value={"skills": {"auto_commit": {"mode": "session_end"}}}),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key-1234567890",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    fake = MagicMock()
    fake.finalize.return_value = {"status": "noop", "reason": "disabled", "commit_created": False}
    agent._skill_autocommit = fake
    agent.shutdown_memory_provider([])

    fake.finalize.assert_called_once_with()
    assert agent._last_skill_autocommit_result["status"] == "noop"
