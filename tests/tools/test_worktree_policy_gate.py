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



def test_write_file_tool_blocks_base_checkout_repo_path_without_worktree(tmp_path):
    from tools import file_tools

    repo = _init_git_repo(tmp_path / "repo")
    result = json.loads(file_tools.write_file_tool(str(repo / "notes.txt"), "hello", task_id="t1"))

    assert result["error"]
    assert "dedicated worktree" in result["error"].lower()



def test_write_file_tool_allows_managed_worktree_path(tmp_path):
    from tools import file_tools

    repo = _init_git_repo(tmp_path / "repo")
    worktree_dir = repo / ".worktrees" / "task-1"
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", str(worktree_dir), "-b", "task-1", "HEAD"], cwd=repo, capture_output=True, check=True)
    worktree_file = worktree_dir / "notes.txt"

    mock_file_ops = MagicMock()
    mock_result = MagicMock()
    mock_result.to_dict.return_value = {"path": str(worktree_file), "bytes_written": 5}
    mock_file_ops.write_file.return_value = mock_result

    with patch("tools.file_tools._get_file_ops", return_value=mock_file_ops):
        result = json.loads(file_tools.write_file_tool(str(worktree_file), "hello", task_id="t1"))

    assert result["bytes_written"] == 5
    mock_file_ops.write_file.assert_called_once_with(str(worktree_file), "hello")



def test_write_file_tool_blocks_fake_worktree_directory_without_git_metadata(tmp_path):
    from tools import file_tools

    repo = _init_git_repo(tmp_path / "repo")
    fake_worktree_file = repo / ".worktrees" / "fake" / "notes.txt"
    fake_worktree_file.parent.mkdir(parents=True, exist_ok=True)

    result = json.loads(file_tools.write_file_tool(str(fake_worktree_file), "hello", task_id="t1"))

    assert result["error"]
    assert "dedicated worktree" in result["error"].lower()



def test_write_file_tool_blocks_relative_repo_path_against_task_cwd(tmp_path):
    from tools import file_tools

    repo = _init_git_repo(tmp_path / "repo")

    with patch("tools.file_tools._resolve_policy_context", return_value=(str(repo), None)):
        result = json.loads(file_tools.write_file_tool("notes.txt", "hello", task_id="t1"))

    assert result["error"]
    assert "dedicated worktree" in result["error"].lower()



def test_patch_tool_blocks_base_checkout_repo_path_without_worktree(tmp_path):
    from tools import file_tools

    repo = _init_git_repo(tmp_path / "repo")
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {repo / 'README.md'}\n"
        "@@\n"
        "-# repo\n"
        "+# updated\n"
        "*** End Patch"
    )

    result = json.loads(file_tools.patch_tool(mode="patch", patch=patch_text, task_id="t1"))

    assert result["error"]
    assert "dedicated worktree" in result["error"].lower()



def test_terminal_tool_blocks_repo_mutating_command_in_base_checkout(tmp_path, monkeypatch):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command="git add README.md", workdir=str(repo)))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "dedicated worktree" in result["error"].lower()
    mock_env.execute.assert_not_called()



def test_terminal_tool_blocks_git_dash_c_repo_mutating_command_in_base_checkout(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}

    command = f"git -C {repo} add README.md"
    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "dedicated worktree" in result["error"].lower()
    mock_env.execute.assert_not_called()



def test_terminal_tool_blocks_repo_mutating_command_when_default_cwd_is_base_checkout(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command="git commit -m 'x'"))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "dedicated worktree" in result["error"].lower()
    mock_env.execute.assert_not_called()



def test_terminal_tool_blocks_compound_repo_mutating_command_in_base_checkout(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command="git status --short && git add README.md", workdir=str(repo)))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "dedicated worktree" in result["error"].lower()
    mock_env.execute.assert_not_called()



def test_terminal_tool_allows_read_only_command_in_base_checkout(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "clean", "returncode": 0}

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command="git apply --check demo.patch", workdir=str(repo)))

    assert result["exit_code"] == 0
    assert result["output"] == "clean"
    mock_env.execute.assert_called_once()



def test_terminal_tool_allows_repo_mutating_command_inside_managed_worktree(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    worktree_dir = repo / ".worktrees" / "task-1"
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "add", str(worktree_dir), "-b", "task-1", "HEAD"], cwd=repo, capture_output=True, check=True)
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command="git add README.md", workdir=str(worktree_dir)))

    assert result["exit_code"] == 0
    assert result["output"] == "ok"
    mock_env.execute.assert_called_once()



def test_command_is_repo_mutating_detects_wrapper_env_mutations():
    from tools.worktree_policy import command_is_repo_mutating

    assert command_is_repo_mutating(
        "npx @dotenvx/dotenvx set ADMIN_SECRET rotated --env-file env/.env.production"
    )
    assert command_is_repo_mutating("vercel env pull env/.env.preview")
    assert command_is_repo_mutating("vercel env add ADMIN_SECRET production")
    assert command_is_repo_mutating("vercel env update ADMIN_SECRET production")
    assert command_is_repo_mutating("vercel env rm ADMIN_SECRET production --yes")
    assert command_is_repo_mutating("npx tsx scripts/sync-env.ts --target vercel-production")



def test_terminal_tool_blocks_wrapper_env_mutation_command_in_base_checkout(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(
            terminal_tool(
                command="npx @dotenvx/dotenvx set ADMIN_SECRET rotated --env-file env/.env.production",
                workdir=str(repo),
            )
        )

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "dedicated worktree" in result["error"].lower()
    mock_env.execute.assert_not_called()
