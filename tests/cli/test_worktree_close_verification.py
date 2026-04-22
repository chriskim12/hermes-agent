"""Real CLI tests for minimal worktree enforcement and close verification."""

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "test-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _force_remove_worktree(info: dict | None) -> None:
    if not info:
        return
    subprocess.run(
        ["git", "worktree", "remove", info["path"], "--force"],
        cwd=info["repo_root"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "branch", "-D", info["branch"]],
        cwd=info["repo_root"],
        capture_output=True,
        check=False,
    )


def test_validate_mutable_workspace_rejects_base_checkout_path(git_repo):
    import cli as cli_mod

    error = cli_mod._validate_mutable_workspace_paths(str(git_repo), str(git_repo))

    assert error is not None
    assert "base checkout" in error.lower()


def test_validate_executor_workdir_rejects_base_checkout_path(git_repo):
    import cli as cli_mod

    workspace_path = git_repo / ".worktrees" / "hermes-proof"
    error = cli_mod._validate_mutable_workspace_paths(
        str(git_repo),
        str(workspace_path),
        executor_workdir=str(git_repo),
    )

    assert error is not None
    assert "executor" in error.lower()
    assert "base checkout" in error.lower()


def test_cleanup_worktree_reports_success_when_repo_clean(git_repo):
    import cli as cli_mod

    info = None
    try:
        info = cli_mod._setup_worktree(str(git_repo))
        assert info is not None

        result = cli_mod._cleanup_worktree(info)

        assert result is True
        assert not Path(info["path"]).exists()
    finally:
        _force_remove_worktree(info)


def test_cleanup_worktree_reports_failure_when_base_checkout_dirty(git_repo):
    import cli as cli_mod

    info = None
    try:
        info = cli_mod._setup_worktree(str(git_repo))
        assert info is not None

        (git_repo / "DIRTY_BASE.txt").write_text("dirty")

        result = cli_mod._cleanup_worktree(info)

        assert result is False
        assert not Path(info["path"]).exists()
        assert (git_repo / "DIRTY_BASE.txt").exists()
    finally:
        _force_remove_worktree(info)
