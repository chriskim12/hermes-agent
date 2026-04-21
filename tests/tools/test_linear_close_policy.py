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
    (repo / "develop.txt").write_text("develop\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "develop change"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "develop"], cwd=repo, capture_output=True, check=True)
    return repo


def _linear_done_command() -> str:
    return (
        'python3 - <<\'PY\'\n'
        'print({"query":"mutation { issueUpdate(id: \\\"CH-999\\\", input: { stateId: '
        '\\\"11441b27-828e-4dd5-a66f-9236a98d82c9\\\" }) { success } }"})\n'
        'print("https://api.linear.app/graphql")\n'
        'PY'
    )


def test_linear_done_close_blockers_detect_dirty_base_checkout(tmp_path):
    from tools.linear_close_policy import linear_done_close_blockers

    repo = _init_git_repo(tmp_path / "repo")
    (repo / "DIRTY.txt").write_text("dirty\n", encoding="utf-8")

    blockers = linear_done_close_blockers(str(repo))

    assert "base_checkout_dirty" in blockers


def test_linear_done_close_blockers_detect_worktree_and_branch_residue(tmp_path):
    from tools.linear_close_policy import linear_done_close_blockers

    repo = _init_git_repo(tmp_path / "repo")
    worktree_dir = repo / ".worktrees" / "task-1"
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", str(worktree_dir), "-b", "task-1", "HEAD"], cwd=repo, capture_output=True, check=True)

    blockers = linear_done_close_blockers(str(repo))

    assert "worktree_residue" in blockers
    assert "branch_residue" in blockers


def test_terminal_tool_blocks_linear_done_transition_when_repo_has_residue(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    (repo / "DIRTY.txt").write_text("dirty\n", encoding="utf-8")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}
    command = (
        'python3 - <<\'PY\'\n'
        'print({"query":"mutation { issueUpdate(id: \\\"CH-999\\\", input: { stateId: '
        '\\\"11441b27-828e-4dd5-a66f-9236a98d82c9\\\" }) { success } }"})\n'
        'print("https://api.linear.app/graphql")\n'
        'PY'
    )

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "repo hygiene" in result["error"].lower()
    assert "base_checkout_dirty" in result["error"]
    mock_env.execute.assert_not_called()


def test_terminal_tool_allows_linear_done_transition_when_repo_is_clean(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    command = _linear_done_command()

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == 0
    assert result["output"] == "ok"
    mock_env.execute.assert_called_once()



def test_dailychingu_done_allows_clean_develop_with_unrelated_review_lane(tmp_path):
    from tools.linear_close_policy import linear_done_close_blockers

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    worktree_dir = repo / ".worktrees" / "other-review"
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", str(worktree_dir), "-b", "other-review", "HEAD"], cwd=repo, capture_output=True, check=True)

    blockers = linear_done_close_blockers(str(repo))

    assert blockers == []



def test_dailychingu_done_blocks_when_current_task_worktree_still_open(tmp_path):
    from tools.linear_close_policy import linear_done_close_blockers

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    worktree_dir = repo / ".worktrees" / "task-1"
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", str(worktree_dir), "-b", "task-1", "HEAD"], cwd=repo, capture_output=True, check=True)

    blockers = linear_done_close_blockers(str(worktree_dir))

    assert "task_worktree_still_open" in blockers



def test_dailychingu_done_blocks_when_root_still_on_task_branch(tmp_path):
    from tools.linear_close_policy import linear_done_close_blockers

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    subprocess.run(["git", "checkout", "-b", "feature/task-1"], cwd=repo, capture_output=True, check=True)

    blockers = linear_done_close_blockers(str(repo))

    assert "task_branch_not_integrated" in blockers



def test_dailychingu_done_blocks_when_root_is_detached_head(tmp_path):
    from tools.linear_close_policy import linear_done_close_blockers

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=repo, capture_output=True, check=True)

    blockers = linear_done_close_blockers(str(repo))

    assert "task_branch_not_integrated" in blockers



def test_terminal_tool_allows_dailychingu_done_on_clean_develop_with_other_review_lane(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    worktree_dir = repo / ".worktrees" / "other-review"
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", str(worktree_dir), "-b", "other-review", "HEAD"], cwd=repo, capture_output=True, check=True)
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    command = _linear_done_command()

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == 0
    assert result["output"] == "ok"
    mock_env.execute.assert_called_once()



def test_terminal_tool_blocks_dailychingu_done_on_detached_head_root(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    subprocess.run(["git", "checkout", "--detach", "HEAD"], cwd=repo, capture_output=True, check=True)
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}
    command = _linear_done_command()

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "task_branch_not_integrated" in result["error"]
    mock_env.execute.assert_not_called()
