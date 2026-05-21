from __future__ import annotations

import json
import subprocess
from pathlib import Path

from model_tools import handle_function_call


POLICY_TEMPLATE = """version: 1
repo:
  name: example/repo
  class: runtime_tooling
authority:
  policy_source: .hermes/repo-policy.yaml
  agents_pointer: AGENTS.md
roles:
  work: task_worktree
  landing: verified_fork_main
  release: upstream_sync_or_local_release_note
  live_apply: gateway_runtime_queue
workflow:
  canonical_landing: verified_landing
  work_done_means: verified_landing_candidate
  release_source: main
  release_target: main
  live_apply: gateway_runtime_queue
branches:
  landing: main
  release_base: main
gates:
  green_allowed:
  - code_changes
  - tests
  - static_checks
  - cleanup
  - local_commit
  yellow_queue:
  - live_apply_pending
  - gateway_restart_needed
  - human_review_needed
  red_requires_explicit_approval:
  - push
  - upstream_pr
  - merge
  - release
  - deploy
  - env_secret_change
  - gateway_restart_reload
  - live_runtime_apply
  - destructive_cleanup
runtime:
  restart_policy: queue_apply_one_restart
  live_apply_queue: required
  queue_entry_requires:
  - changed_artifact
  - commit_or_ref
  - reason
  - exact_apply_action
  - post_apply_proof
  - rollback_or_stop_condition
guard:
  canonical_checkout: {canonical}
  protected_branches:
  - main
  mutation_on_canonical_checkout: block
closeout:
  required_sections:
  - 결론
  - 실제 반영
  - 아직 안 한 것
  - 다음 판단
  - Policy check
  - Green 완료
  - Yellow 대기
  - Red 필요
  - 검증
  - Git 상태
  - Live 상태
  - Gateway restart 필요
  - Live runtime 반영됨
  - 대기열 포함됨
"""


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "canonical"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], repo)
    (repo / ".hermes").mkdir()
    (repo / ".hermes" / "repo-policy.yaml").write_text(
        POLICY_TEMPLATE.format(canonical=str(repo)), encoding="utf-8"
    )
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "."], repo)
    _run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed"], repo)
    return repo


def _tool_result(text: str) -> dict:
    return json.loads(text)


def test_write_file_blocks_protected_canonical_checkout_before_file_changes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    target = repo / "blocked.txt"

    result = _tool_result(handle_function_call("write_file", {"path": str(target), "content": "bad"}))

    assert result["blocked_by"] == "repo_policy_canonical_checkout_guard"
    assert "canonical_checkout" in result["error"]
    assert "branch=main" in result["error"]
    assert "task-owned worktree/branch" in result["error"]
    assert not target.exists()


def test_write_file_allows_task_worktree_branch_for_same_repo(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    worktree = tmp_path / "task-worktree"
    _run(["git", "worktree", "add", "-b", "feature/test", str(worktree)], repo)
    target = worktree / "allowed.txt"

    result = _tool_result(handle_function_call("write_file", {"path": str(target), "content": "ok"}))

    assert "error" not in result
    assert result.get("bytes_written") == 2
    assert target.read_text(encoding="utf-8") == "ok"


def test_hook_script_blocks_protected_canonical_commit_path(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    hook_script = Path(__file__).resolve().parents[2] / "scripts" / "repo_policy_guard_hook.py"

    result = subprocess.run(
        ["python", str(hook_script)],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    assert result.returncode == 1
    assert "commit/push from protected canonical checkout is blocked" in result.stderr
    assert "branch=main" in result.stderr


def test_terminal_destructive_command_blocks_protected_canonical_workdir(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    target = repo / "terminal-blocked.txt"

    result = _tool_result(handle_function_call("terminal", {"command": "printf bad > terminal-blocked.txt", "workdir": str(repo)}))

    assert result["blocked_by"] == "repo_policy_canonical_checkout_guard"
    assert not target.exists()
