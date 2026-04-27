import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

_CHRIS_IN_REVIEW_STATE_ID = "bd49fae3-66b0-4fae-bc61-89501e03e0ba"


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


def _in_review_command(handoff_block: str = "") -> str:
    escaped = handoff_block.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return (
        "python3 - <<'PY'\n"
        'print("https://api.linear.app/graphql")\n'
        'print("mutation { issueUpdate(id: \\\"CH-181\\\", input: { stateId: \\\"'
        + _CHRIS_IN_REVIEW_STATE_ID
        + '\\\", description: \\\"'
        + escaped
        + '\\\" }) { success } }")\n'
        "PY"
    )



def _handoff_text(decision: str = "review verdict only") -> str:
    return "\n".join(
        [
            "HANDOFF_CHANGED: Added the CH-262 live Linear lookup.",
            "HANDOFF_VERIFIED: Focused guard tests passed.",
            "HANDOFF_RISKS: Runtime reload remains unperformed.",
            f"HANDOFF_DECISION: {decision}",
        ]
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



def test_in_review_handoff_allows_live_linear_description_without_command_handoff(tmp_path):
    from tools.linear_close_policy import _linear_in_review_handoff_blockers

    repo = _init_git_repo(tmp_path / "repo")
    command = _in_review_command()

    blockers, detail = _linear_in_review_handoff_blockers(
        repo,
        command,
        fetch_handoff_texts=lambda issue_id: ["## Current handoff\n" + _handoff_text()],
    )

    assert blockers == []
    assert detail is None


def test_in_review_handoff_allows_live_linear_recent_comment_without_command_handoff(tmp_path):
    from tools.linear_close_policy import _linear_in_review_handoff_blockers

    repo = _init_git_repo(tmp_path / "repo")
    command = _in_review_command()

    blockers, detail = _linear_in_review_handoff_blockers(
        repo,
        command,
        fetch_handoff_texts=lambda issue_id: ["older comment", _handoff_text()],
    )

    assert blockers == []
    assert detail is None


def test_in_review_handoff_blocks_invalid_live_decision_without_leaking_body(tmp_path):
    from tools.linear_close_policy import _linear_in_review_handoff_blockers

    repo = _init_git_repo(tmp_path / "repo")
    command = _in_review_command()
    sensitive_body = _handoff_text("review verdict only for repo-only commit") + "\nLEAK_SENTINEL=visible-value"

    blockers, detail = _linear_in_review_handoff_blockers(
        repo,
        command,
        fetch_handoff_texts=lambda issue_id: [sensitive_body],
    )

    assert blockers == ["handoff_decision"]
    assert "review verdict only" in detail
    assert "LEAK_SENTINEL" not in detail
    assert "visible-value" not in detail



def test_in_review_handoff_does_not_use_command_fallback_when_live_handoff_is_invalid(tmp_path):
    from tools.linear_close_policy import _linear_in_review_handoff_blockers

    repo = _init_git_repo(tmp_path / "repo")
    command = _in_review_command(_handoff_text())
    invalid_live_body = _handoff_text("review verdict only with extra words")

    blockers, detail = _linear_in_review_handoff_blockers(
        repo,
        command,
        fetch_handoff_texts=lambda issue_id: [invalid_live_body],
    )

    assert blockers == ["handoff_decision"]
    assert "Pending human decision" in detail

def test_in_review_handoff_fails_closed_when_live_lookup_fails_and_no_command_fallback(tmp_path):
    from tools.linear_close_policy import LinearHandoffLookupError, _linear_in_review_handoff_blockers

    repo = _init_git_repo(tmp_path / "repo")
    command = _in_review_command()

    def fail_lookup(issue_id):
        raise LinearHandoffLookupError("lookup unavailable")

    blockers, detail = _linear_in_review_handoff_blockers(
        repo,
        command,
        fetch_handoff_texts=fail_lookup,
    )

    assert set(blockers) == {"handoff_changed", "handoff_verified", "handoff_risks", "handoff_decision"}
    assert "lookup unavailable" in detail


def test_terminal_tool_allows_in_review_transition_with_live_linear_handoff(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    command = _in_review_command()

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}), \
         patch("tools.linear_close_policy._fetch_linear_handoff_texts", return_value=[_handoff_text()]):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == 0
    assert result["output"] == "ok"
    mock_env.execute.assert_called_once()

def test_terminal_tool_blocks_in_review_transition_without_required_handoff_block(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}
    command = _in_review_command()

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "in review handoff" in result["error"].lower()
    assert "handoff_changed" in result["error"].lower()
    mock_env.execute.assert_not_called()


def test_terminal_tool_allows_in_review_transition_with_valid_review_verdict_handoff(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    handoff = "\n".join(
        [
            "HANDOFF_CHANGED: Added the CH-181 handoff gate.",
            "HANDOFF_VERIFIED: Ran focused policy tests.",
            "HANDOFF_RISKS: Broader Linear wrapper flow is still unverified.",
            "HANDOFF_DECISION: review verdict only",
        ]
    )
    command = _in_review_command(handoff)

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == 0
    assert result["output"] == "ok"
    mock_env.execute.assert_called_once()


def test_terminal_tool_blocks_non_profile_push_authority_handoff(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}
    handoff = "\n".join(
        [
            "HANDOFF_CHANGED: Added workflow hooks.",
            "HANDOFF_VERIFIED: Focused tests passed.",
            "HANDOFF_RISKS: runtime proof pending.",
            "HANDOFF_DECISION: push authority",
        ]
    )
    command = _in_review_command(handoff)

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "push authority semantics" in result["error"].lower()
    mock_env.execute.assert_not_called()


def test_terminal_tool_blocks_non_profile_release_authority_handoff(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_git_repo(tmp_path / "repo")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "should not run", "returncode": 0}
    handoff = "\n".join(
        [
            "HANDOFF_CHANGED: Added workflow hooks.",
            "HANDOFF_VERIFIED: Focused tests passed.",
            "HANDOFF_RISKS: runtime proof pending.",
            "HANDOFF_DECISION: release authority",
        ]
    )
    command = _in_review_command(handoff)

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == -1
    assert result["status"] == "blocked"
    assert "release authority semantics" in result["error"].lower()
    mock_env.execute.assert_not_called()


def test_terminal_tool_allows_dailychingu_push_authority_handoff(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    handoff = "\n".join(
        [
            "HANDOFF_CHANGED: Added workflow hooks.",
            "HANDOFF_VERIFIED: Focused tests passed.",
            "HANDOFF_RISKS: runtime proof pending.",
            "HANDOFF_DECISION: push authority",
        ]
    )
    command = _in_review_command(handoff)

    with patch("tools.terminal_tool._get_env_config", return_value={"env_type": "local", "env_name": "default", "cwd": str(repo), "timeout": 180}), \
         patch("tools.terminal_tool._start_cleanup_thread"), \
         patch("tools.terminal_tool._active_environments", {"default": mock_env}), \
         patch("tools.terminal_tool._last_activity", {"default": 0}), \
         patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}):
        result = json.loads(terminal_tool(command=command, workdir=str(repo)))

    assert result["exit_code"] == 0
    assert result["output"] == "ok"
    mock_env.execute.assert_called_once()


def test_terminal_tool_allows_dailychingu_release_authority_handoff(tmp_path):
    from tools.terminal_tool import terminal_tool

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    mock_env = MagicMock()
    mock_env.execute.return_value = {"output": "ok", "returncode": 0}
    handoff = "\n".join(
        [
            "HANDOFF_CHANGED: Added workflow hooks.",
            "HANDOFF_VERIFIED: Focused tests passed.",
            "HANDOFF_RISKS: runtime proof pending.",
            "HANDOFF_DECISION: release authority",
        ]
    )
    command = _in_review_command(handoff)

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
