import json
import subprocess
from pathlib import Path

import pytest
import yaml

from cron.repo_update_gate import format_cron_output, load_inventory, run_check


@pytest.fixture
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Yuuka")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "yuuka@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Yuuka")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "yuuka@example.com")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _git_global(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _seed_remote(remote: Path, seed: Path) -> None:
    _git_global("init", "--bare", str(remote))
    _git_global("clone", str(remote), str(seed))
    _git(seed, "checkout", "-b", "main")
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "push", "-u", "origin", "main")
    _git_global("-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main")


def _clone_repo(remote: Path, clone: Path) -> None:
    _git_global("clone", str(remote), str(clone))


def _advance_remote(seed: Path, filename: str, content: str, message: str) -> None:
    (seed / filename).write_text(content, encoding="utf-8")
    _git(seed, "add", filename)
    _git(seed, "commit", "-m", message)
    _git(seed, "push", "origin", "main")


def _write_inventory(tmp_path: Path, repos: list[dict]) -> Path:
    inventory_path = tmp_path / "repo-upstream-watch.yaml"
    inventory = {
        "schema_version": 1,
        "metadata": {
            "silent_marker": "[SILENT]",
            "state_path": str(tmp_path / "state" / "repo-upstream-watch.json"),
            "entrypoints": {
                "check": f"python {tmp_path / 'repo_update_gate.py'} check --inventory {inventory_path}",
                "apply_plan": f"python {tmp_path / 'repo_update_gate.py'} apply-plan --inventory {inventory_path}",
            },
        },
        "repos": repos,
    }
    inventory_path.write_text(yaml.safe_dump(inventory, sort_keys=False), encoding="utf-8")
    return inventory_path


def test_load_inventory_rejects_unknown_mode(tmp_path: Path) -> None:
    inventory_path = _write_inventory(
        tmp_path,
        [
            {
                "name": "bad",
                "path": "/tmp/missing",
                "mode": "maybe_auto",
                "base_remote": "origin",
                "base_branch": "main",
                "smoke_test": "true",
                "apply_window": "manual",
            }
        ],
    )

    with pytest.raises(ValueError, match="mode"):
        load_inventory(inventory_path)


def test_run_check_classifies_repos_and_silences_when_unchanged(tmp_path: Path, git_identity) -> None:
    omx_remote = tmp_path / "omx-remote.git"
    omx_seed = tmp_path / "omx-seed"
    _seed_remote(omx_remote, omx_seed)
    omx_repo = tmp_path / "oh-my-codex"
    _clone_repo(omx_remote, omx_repo)
    _advance_remote(omx_seed, "upstream.txt", "one\n", "advance omx")

    claw_remote = tmp_path / "claw-remote.git"
    claw_seed = tmp_path / "claw-seed"
    _seed_remote(claw_remote, claw_seed)
    claw_repo = tmp_path / "clawhip"
    _clone_repo(claw_remote, claw_repo)
    (claw_repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    hermes_remote = tmp_path / "hermes-remote.git"
    hermes_seed = tmp_path / "hermes-seed"
    _seed_remote(hermes_remote, hermes_seed)
    hermes_repo = tmp_path / "hermes-agent"
    _clone_repo(hermes_remote, hermes_repo)
    _git(hermes_repo, "checkout", "-b", "feature/local")

    inventory_path = _write_inventory(
        tmp_path,
        [
            {
                "name": "omx",
                "path": str(omx_repo),
                "mode": "safe_auto_apply",
                "base_remote": "origin",
                "base_branch": "main",
                "smoke_test": "npm test",
                "apply_window": "manual",
            },
            {
                "name": "clawhip",
                "path": str(claw_repo),
                "mode": "report_only",
                "base_remote": "origin",
                "base_branch": "main",
                "smoke_test": "cargo test",
                "apply_window": "manual",
            },
            {
                "name": "hermes-agent",
                "path": str(hermes_repo),
                "mode": "staged_sync",
                "base_remote": "origin",
                "base_branch": "main",
                "smoke_test": "python -m pytest tests/ -q",
                "apply_window": "manual",
                "staging_branch": "sync/upstream-main",
                "staging_worktree_path": str(tmp_path / "worktrees" / "hermes-upstream-main"),
            },
        ],
    )

    report1 = run_check(inventory_path)
    repos1 = {repo["name"]: repo for repo in report1["repos"]}

    assert report1["changed"] is True
    assert report1["status"] == "attention_required"
    assert repos1["omx"]["repo_status"] == "behind_base"
    assert repos1["omx"]["apply_verdict"] == "eligible_for_direct_apply"
    assert repos1["omx"]["direct_apply_allowed"] is True
    assert repos1["clawhip"]["repo_status"] == "dirty_worktree"
    assert repos1["clawhip"]["direct_apply_allowed"] is False
    assert repos1["hermes-agent"]["repo_status"] == "branch_mismatch"
    assert repos1["hermes-agent"]["apply_verdict"] == "stage_required"
    assert repos1["hermes-agent"]["stage_worktree_path"] == str(tmp_path / "worktrees" / "hermes-upstream-main")

    state_path = Path(report1["state_path"])
    assert state_path.exists() is True
    first_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert first_state["fingerprint"]

    report2 = run_check(inventory_path)
    assert report2["changed"] is False
    assert format_cron_output(report2) == "[SILENT]"
