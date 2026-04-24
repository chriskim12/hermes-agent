import subprocess
from pathlib import Path


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init"], cwd=path)
    _run(["git", "config", "user.email", "test@test.com"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=path)
    _run(["git", "commit", "-m", "init"], cwd=path)
    return path


def _init_dailychingu_repo(path: Path) -> Path:
    repo = _init_git_repo(path)
    _run(["git", "branch", "-M", "main"], cwd=repo)
    _run(["git", "checkout", "-b", "develop"], cwd=repo)
    (repo / "develop.txt").write_text("develop\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=repo)
    _run(["git", "commit", "-m", "develop base"], cwd=repo)
    return repo


def _add_task_worktree(repo: Path, branch: str = "feature/ch-195") -> Path:
    worktree = repo / ".worktrees" / "ch-195"
    _run(["git", "worktree", "add", "-b", branch, str(worktree), "develop"], cwd=repo)
    (worktree / "task.txt").write_text("task result\n", encoding="utf-8")
    _run(["git", "add", "task.txt"], cwd=worktree)
    _run(["git", "commit", "-m", "task result"], cwd=worktree)
    return worktree


def test_phrase_resolver_maps_high_confidence_push_authority():
    from tools.operator_workflow import PUSH_AUTHORITY, normalize_owner_authority_phrase

    phrases = [
        "push 승인",
        "push도 진행해",
        "push까지 진행해",
        "develop에 반영해",
        "develop으로 올려",
        "task close까지 진행해",
        "반영하고 cleanup까지 해",
        "반영하고 정리해",
    ]

    for phrase in phrases:
        resolved = normalize_owner_authority_phrase(phrase)
        assert resolved.decision == PUSH_AUTHORITY
        assert resolved.executable is True


def test_phrase_resolver_keeps_ambiguous_and_inspection_non_executable():
    from tools.operator_workflow import (
        INSPECTION_ONLY,
        REVIEW_VERDICT_ONLY,
        UNKNOWN_AUTHORITY,
        normalize_owner_authority_phrase,
    )

    assert normalize_owner_authority_phrase("승인").decision == REVIEW_VERDICT_ONLY
    assert normalize_owner_authority_phrase("확인해봐").decision == INSPECTION_ONLY
    for phrase in ["좋아", "진행해", "마무리해", "올려"]:
        resolved = normalize_owner_authority_phrase(phrase)
        assert resolved.decision == UNKNOWN_AUTHORITY
        assert resolved.executable is False


def test_dailychingu_push_authority_integrates_develop_cleans_task_lane_and_stops_before_release(tmp_path):
    from tools.operator_workflow import PushWorkflowRequest, execute_push_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_task_worktree(repo)
    evidence: list[str] = []

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195",
            linear_issue_id="CH-195",
            owner_phrase="push 승인",
            evidence_callback=evidence.append,
        )
    )

    assert result.success is True
    assert result.release_executed is False
    assert (repo / "task.txt").read_text(encoding="utf-8") == "task result\n"
    assert not task_worktree.exists()
    branches = _run(["git", "branch", "--format=%(refname:short)"], cwd=repo).stdout.splitlines()
    assert "feature/ch-195" not in branches
    assert "develop integration truth" in "\n".join(result.evidence)
    assert "release: not executed" in "\n".join(evidence)


def test_push_authority_blocks_without_live_card(tmp_path):
    from tools.operator_workflow import PushWorkflowRequest, execute_push_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_task_worktree(repo)

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195",
            linear_issue_id=None,
            owner_phrase="push 승인",
        )
    )

    assert result.success is False
    assert "no_live_card" in result.blockers


def test_push_authority_blocks_for_non_profile_repo(tmp_path):
    from tools.operator_workflow import PushWorkflowRequest, execute_push_authority_workflow

    repo = _init_git_repo(tmp_path / "not-dailychingu")
    _run(["git", "checkout", "-b", "develop"], cwd=repo)
    task_worktree = repo / ".worktrees" / "task"
    _run(["git", "worktree", "add", "-b", "feature/task", str(task_worktree), "develop"], cwd=repo)

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/task",
            linear_issue_id="CH-195",
            owner_phrase="develop에 반영해",
        )
    )

    assert result.success is False
    assert "repo_profile_missing_or_not_dailychingu" in result.blockers


def test_push_authority_blocks_dirty_base_without_checkout_side_effect(tmp_path):
    from tools.operator_workflow import PushWorkflowRequest, execute_push_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_task_worktree(repo)
    _run(["git", "checkout", "main"], cwd=repo)
    (repo / "base-dirty.txt").write_text("dirty\n", encoding="utf-8")

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195",
            linear_issue_id="CH-195",
            owner_phrase="push 승인",
        )
    )

    assert result.success is False
    assert "dirty_integration_checkout" in result.blockers
    assert _run(["git", "branch", "--show-current"], cwd=repo).stdout.strip() == "main"
    assert task_worktree.exists()


def test_push_authority_blocks_dirty_task_worktree(tmp_path):
    from tools.operator_workflow import PushWorkflowRequest, execute_push_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_task_worktree(repo)
    (task_worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195",
            linear_issue_id="CH-195",
            owner_phrase="반영하고 정리해",
        )
    )

    assert result.success is False
    assert "dirty_task_worktree" in result.blockers
    assert task_worktree.exists()
