import subprocess
from pathlib import Path
from unittest.mock import patch


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


def _add_migration_task_worktree(repo: Path, branch: str = "feature/ch-195-migration") -> Path:
    worktree = repo / ".worktrees" / "ch-195-migration"
    _run(["git", "worktree", "add", "-b", branch, str(worktree), "develop"], cwd=repo)
    migrations = worktree / "supabase" / "migrations"
    migrations.mkdir(parents=True, exist_ok=True)
    (migrations / "20260424000100_create_push_gate.sql").write_text(
        "create table push_gate(id bigint primary key);\n",
        encoding="utf-8",
    )
    _run(["git", "add", "supabase/migrations/20260424000100_create_push_gate.sql"], cwd=worktree)
    _run(["git", "commit", "-m", "add migration"], cwd=worktree)
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


def test_push_authority_applies_dev_migration_gate_before_cleanup(tmp_path):
    from tools.operator_workflow import (
        DevMigrationGateResult,
        PushWorkflowRequest,
        execute_push_authority_workflow,
    )

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_migration_task_worktree(repo)
    applied: list[tuple[str, ...]] = []

    def apply_dev_migrations(files: tuple[str, ...]) -> DevMigrationGateResult:
        applied.append(files)
        return DevMigrationGateResult(success=True, evidence=f"dev applied: {','.join(files)}")

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195-migration",
            linear_issue_id="CH-195",
            owner_phrase="push 승인",
            dev_db_target="dailychingu-dev",
            dev_migration_callback=apply_dev_migrations,
        )
    )

    expected = ("supabase/migrations/20260424000100_create_push_gate.sql",)
    assert result.success is True
    assert result.dev_migration_files == expected
    assert result.dev_migrations_applied is True
    assert applied == [expected]
    assert "dev migration gate: dev applied" in "\n".join(result.evidence)
    assert not task_worktree.exists()


def test_push_authority_blocks_migration_without_dev_apply_callback(tmp_path):
    from tools.operator_workflow import PushWorkflowRequest, execute_push_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_migration_task_worktree(repo)

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195-migration",
            linear_issue_id="CH-195",
            owner_phrase="push 승인",
            dev_db_target="dailychingu-dev",
        )
    )

    assert result.success is False
    assert "dev_migration_apply_missing" in result.blockers
    assert task_worktree.exists()


def test_push_authority_blocks_migration_for_non_dev_target(tmp_path):
    from tools.operator_workflow import (
        DevMigrationGateResult,
        PushWorkflowRequest,
        execute_push_authority_workflow,
    )

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_migration_task_worktree(repo)
    called = False

    def should_not_apply(files: tuple[str, ...]) -> DevMigrationGateResult:
        nonlocal called
        called = True
        return DevMigrationGateResult(success=True)

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195-migration",
            linear_issue_id="CH-195",
            owner_phrase="push 승인",
            dev_db_target="dailychingu-production",
            dev_migration_callback=should_not_apply,
        )
    )

    assert result.success is False
    assert "dev_migration_target_not_dev" in result.blockers
    assert called is False
    assert task_worktree.exists()


def test_push_authority_blocks_migration_callback_exception(tmp_path):
    from tools.operator_workflow import PushWorkflowRequest, execute_push_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_migration_task_worktree(repo)

    def broken_apply(files: tuple[str, ...]):
        raise RuntimeError("dev db unavailable")

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195-migration",
            linear_issue_id="CH-195",
            owner_phrase="push 승인",
            dev_db_target="dailychingu-dev",
            dev_migration_callback=broken_apply,
        )
    )

    assert result.success is False
    assert "dev_migration_apply_failed" in result.blockers
    assert "dev db unavailable" in "\n".join(result.evidence)
    assert task_worktree.exists()


def test_push_authority_blocks_migration_detection_failure(tmp_path):
    import tools.operator_workflow as operator_workflow
    from tools.operator_workflow import PushWorkflowRequest, execute_push_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_migration_task_worktree(repo)
    original_run_git = operator_workflow._run_git

    def fail_diff(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout="", stderr="bad ref")
        return original_run_git(repo_path, *args)

    with patch("tools.operator_workflow._run_git", side_effect=fail_diff):
        result = execute_push_authority_workflow(
            PushWorkflowRequest(
                repo_path=repo,
                task_worktree=task_worktree,
                task_branch="feature/ch-195-migration",
                linear_issue_id="CH-195",
                owner_phrase="push 승인",
                dev_db_target="dailychingu-dev",
                dev_migration_callback=lambda files: operator_workflow.DevMigrationGateResult(success=True),
            )
        )

    assert result.success is False
    assert "migration_detection_failed" in result.blockers
    assert task_worktree.exists()


def test_push_authority_does_not_apply_dev_migration_when_base_dirty(tmp_path):
    from tools.operator_workflow import (
        DevMigrationGateResult,
        PushWorkflowRequest,
        execute_push_authority_workflow,
    )

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_migration_task_worktree(repo)
    _run(["git", "checkout", "main"], cwd=repo)
    (repo / "base-dirty.txt").write_text("dirty\n", encoding="utf-8")
    called = False

    def should_not_apply(files: tuple[str, ...]) -> DevMigrationGateResult:
        nonlocal called
        called = True
        return DevMigrationGateResult(success=True)

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195-migration",
            linear_issue_id="CH-195",
            owner_phrase="push 승인",
            dev_db_target="dailychingu-dev",
            dev_migration_callback=should_not_apply,
        )
    )

    assert result.success is False
    assert "dirty_integration_checkout" in result.blockers
    assert called is False
    assert _run(["git", "branch", "--show-current"], cwd=repo).stdout.strip() == "main"
    assert task_worktree.exists()


def test_push_authority_does_not_apply_dev_migration_when_branch_not_mergeable(tmp_path):
    from tools.operator_workflow import (
        DevMigrationGateResult,
        PushWorkflowRequest,
        execute_push_authority_workflow,
    )

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    task_worktree = _add_migration_task_worktree(repo)
    (repo / "develop-advanced.txt").write_text("new develop truth\n", encoding="utf-8")
    _run(["git", "add", "develop-advanced.txt"], cwd=repo)
    _run(["git", "commit", "-m", "advance develop"], cwd=repo)
    called = False

    def should_not_apply(files: tuple[str, ...]) -> DevMigrationGateResult:
        nonlocal called
        called = True
        return DevMigrationGateResult(success=True)

    result = execute_push_authority_workflow(
        PushWorkflowRequest(
            repo_path=repo,
            task_worktree=task_worktree,
            task_branch="feature/ch-195-migration",
            linear_issue_id="CH-195",
            owner_phrase="push 승인",
            dev_db_target="dailychingu-dev",
            dev_migration_callback=should_not_apply,
        )
    )

    assert result.success is False
    assert "unintegratable_task_branch" in result.blockers
    assert called is False
    assert task_worktree.exists()


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



def _add_develop_migration(repo: Path) -> tuple[str, ...]:
    _run(["git", "checkout", "develop"], cwd=repo)
    migrations = repo / "supabase" / "migrations"
    migrations.mkdir(parents=True, exist_ok=True)
    migration = migrations / "20260424000200_release_gate.sql"
    migration.write_text("alter table push_gate add column released_at timestamptz;\n", encoding="utf-8")
    _run(["git", "add", "supabase/migrations/20260424000200_release_gate.sql"], cwd=repo)
    _run(["git", "commit", "-m", "add release migration"], cwd=repo)
    return ("supabase/migrations/20260424000200_release_gate.sql",)


def test_release_authority_promotes_develop_to_main_without_migration(tmp_path):
    from tools.operator_workflow import ReleaseWorkflowRequest, execute_release_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    evidence: list[str] = []

    result = execute_release_authority_workflow(
        ReleaseWorkflowRequest(
            repo_path=repo,
            linear_issue_id="CH-196",
            owner_phrase="release 승인",
            evidence_callback=evidence.append,
        )
    )

    assert result.success is True
    assert result.release_executed is True
    assert result.release_commit == _run(["git", "rev-parse", "develop"], cwd=repo).stdout.strip()
    assert _run(["git", "branch", "--show-current"], cwd=repo).stdout.strip() == "main"
    assert "main release truth" in "\n".join(evidence)


def test_release_authority_applies_prod_migration_gate_before_main_release(tmp_path):
    from tools.operator_workflow import (
        ProdMigrationGateResult,
        ReleaseWorkflowRequest,
        execute_release_authority_workflow,
    )

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    expected = _add_develop_migration(repo)
    applied: list[tuple[str, ...]] = []

    def apply_prod_migrations(files: tuple[str, ...]) -> ProdMigrationGateResult:
        applied.append(files)
        return ProdMigrationGateResult(success=True, evidence=f"prod applied: {','.join(files)}")

    result = execute_release_authority_workflow(
        ReleaseWorkflowRequest(
            repo_path=repo,
            linear_issue_id="CH-196",
            owner_phrase="release 승인",
            prod_db_target="dailychingu-production",
            prod_migration_callback=apply_prod_migrations,
        )
    )

    assert result.success is True
    assert result.release_executed is True
    assert result.prod_migration_files == expected
    assert result.prod_migrations_applied is True
    assert applied == [expected]
    assert "prod migration gate: prod applied" in "\n".join(result.evidence)
    assert _run(["git", "rev-parse", "main"], cwd=repo).stdout == _run(["git", "rev-parse", "develop"], cwd=repo).stdout


def test_release_authority_blocks_migration_without_prod_apply_callback(tmp_path):
    from tools.operator_workflow import ReleaseWorkflowRequest, execute_release_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    _add_develop_migration(repo)

    result = execute_release_authority_workflow(
        ReleaseWorkflowRequest(
            repo_path=repo,
            linear_issue_id="CH-196",
            owner_phrase="release 승인",
            prod_db_target="dailychingu-production",
        )
    )

    assert result.success is False
    assert "prod_migration_apply_missing" in result.blockers
    assert _run(["git", "rev-parse", "main"], cwd=repo).stdout != _run(["git", "rev-parse", "develop"], cwd=repo).stdout


def test_release_authority_blocks_migration_for_non_prod_target_before_callback(tmp_path):
    from tools.operator_workflow import (
        ProdMigrationGateResult,
        ReleaseWorkflowRequest,
        execute_release_authority_workflow,
    )

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    _add_develop_migration(repo)
    called = False

    def should_not_apply(files: tuple[str, ...]) -> ProdMigrationGateResult:
        nonlocal called
        called = True
        return ProdMigrationGateResult(success=True)

    result = execute_release_authority_workflow(
        ReleaseWorkflowRequest(
            repo_path=repo,
            linear_issue_id="CH-196",
            owner_phrase="release 승인",
            prod_db_target="dailychingu-dev",
            prod_migration_callback=should_not_apply,
        )
    )

    assert result.success is False
    assert "prod_migration_target_not_prod" in result.blockers
    assert called is False


def test_release_authority_blocks_prod_migration_detection_failure(tmp_path):
    import tools.operator_workflow as operator_workflow
    from tools.operator_workflow import ReleaseWorkflowRequest, execute_release_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    _add_develop_migration(repo)
    original_run_git = operator_workflow._run_git

    def fail_diff(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(args=["git", *args], returncode=1, stdout="", stderr="bad ref")
        return original_run_git(repo_path, *args)

    with patch("tools.operator_workflow._run_git", side_effect=fail_diff):
        result = execute_release_authority_workflow(
            ReleaseWorkflowRequest(
                repo_path=repo,
                linear_issue_id="CH-196",
                owner_phrase="release 승인",
                prod_db_target="dailychingu-production",
                prod_migration_callback=lambda files: operator_workflow.ProdMigrationGateResult(success=True),
            )
        )

    assert result.success is False
    assert "prod_migration_detection_failed" in result.blockers


def test_release_authority_does_not_apply_prod_migration_when_main_not_fast_forwardable(tmp_path):
    from tools.operator_workflow import (
        ProdMigrationGateResult,
        ReleaseWorkflowRequest,
        execute_release_authority_workflow,
    )

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    _add_develop_migration(repo)
    _run(["git", "checkout", "main"], cwd=repo)
    (repo / "main-only.txt").write_text("main drift\n", encoding="utf-8")
    _run(["git", "add", "main-only.txt"], cwd=repo)
    _run(["git", "commit", "-m", "main drift"], cwd=repo)
    called = False

    def should_not_apply(files: tuple[str, ...]) -> ProdMigrationGateResult:
        nonlocal called
        called = True
        return ProdMigrationGateResult(success=True)

    result = execute_release_authority_workflow(
        ReleaseWorkflowRequest(
            repo_path=repo,
            linear_issue_id="CH-196",
            owner_phrase="release 승인",
            prod_db_target="dailychingu-production",
            prod_migration_callback=should_not_apply,
        )
    )

    assert result.success is False
    assert "unreleasable_integration_branch" in result.blockers
    assert called is False


def test_release_authority_blocks_dirty_release_checkout_without_checkout_side_effect(tmp_path):
    from tools.operator_workflow import ReleaseWorkflowRequest, execute_release_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    _run(["git", "checkout", "develop"], cwd=repo)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    result = execute_release_authority_workflow(
        ReleaseWorkflowRequest(
            repo_path=repo,
            linear_issue_id="CH-196",
            owner_phrase="release 승인",
        )
    )

    assert result.success is False
    assert "dirty_release_checkout" in result.blockers
    assert _run(["git", "branch", "--show-current"], cwd=repo).stdout.strip() == "develop"


def test_release_authority_blocks_broad_release_inspection_phrase_without_main_side_effect(tmp_path):
    from tools.operator_workflow import ReleaseWorkflowRequest, execute_release_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")
    before_main = _run(["git", "rev-parse", "main"], cwd=repo).stdout.strip()

    result = execute_release_authority_workflow(
        ReleaseWorkflowRequest(
            repo_path=repo,
            linear_issue_id="CH-196",
            owner_phrase="release status 확인",
        )
    )

    assert result.success is False
    assert "authority_not_release:release authority" in result.blockers
    assert _run(["git", "rev-parse", "main"], cwd=repo).stdout.strip() == before_main


def test_release_authority_blocks_without_live_card(tmp_path):
    from tools.operator_workflow import ReleaseWorkflowRequest, execute_release_authority_workflow

    repo = _init_dailychingu_repo(tmp_path / "dailychingu")

    result = execute_release_authority_workflow(
        ReleaseWorkflowRequest(
            repo_path=repo,
            linear_issue_id=None,
            owner_phrase="release 승인",
        )
    )

    assert result.success is False
    assert "no_live_card" in result.blockers
