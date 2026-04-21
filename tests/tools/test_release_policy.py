import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, capture_output=True, check=True)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)
    return path


def _init_dailychingu_repo(path: Path) -> Path:
    repo = _init_git_repo(path)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "-b", "develop"], cwd=repo, capture_output=True, check=True)
    (repo / "feature.txt").write_text("develop only\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "develop change"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True, check=True)
    return repo


def _init_dailychingu_repo_with_remote(path: Path) -> Path:
    repo = _init_dailychingu_repo(path)
    remote = path.parent / f"{path.name}-origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main", "develop"], cwd=repo, capture_output=True, check=True)
    return repo


def test_release_close_blockers_require_develop_merged_into_main(tmp_path):
    from tools.release_policy import release_close_blockers

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")

    blockers = release_close_blockers(str(repo))

    assert "release_path_missing_develop_to_main" in blockers


def test_release_close_blockers_require_local_main_synced_to_origin_main(tmp_path):
    from tools.release_policy import release_close_blockers

    repo = _init_dailychingu_repo_with_remote(tmp_path / "dailychingu")
    subprocess.run(["git", "merge", "--ff-only", "develop"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "push", "origin", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=repo, capture_output=True, check=True)

    blockers = release_close_blockers(str(repo))

    assert "local_main_not_fast_forward_synced_to_origin_main" in blockers


def test_terminal_tool_blocks_dailychingu_main_push_when_develop_not_merged(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command="git push origin main", workdir=str(repo)))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "develop -> main" in result["error"]
    mock_env.execute.assert_not_called()
