"""Tests for secrets source/provider CLI nesting."""

import os
import subprocess
import sys


def _run_cli(tmp_path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / ".hermes")
    env["PYTHONPATH"] = os.getcwd()
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_bitwarden_source_is_nested_under_source(tmp_path):
    result = _run_cli(tmp_path, "secrets", "source", "bitwarden", "status")

    assert result.returncode == 0, result.stderr + result.stdout
    assert "Bitwarden Secrets Manager" in result.stdout


def test_provider_bw_alias_routes_to_same_source_handler(tmp_path):
    result = _run_cli(tmp_path, "secrets", "provider", "bw", "status")

    assert result.returncode == 0, result.stderr + result.stdout
    assert "Bitwarden Secrets Manager" in result.stdout


def test_top_level_bitwarden_is_not_a_secrets_command(tmp_path):
    result = _run_cli(tmp_path, "secrets", "bitwarden", "status")

    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_bitwarden_refresh_is_canonical_source_fetch_name(tmp_path):
    result = _run_cli(tmp_path, "secrets", "source", "bitwarden", "refresh")

    assert result.returncode == 0, result.stderr + result.stdout
    assert "Bitwarden source refresh only" in result.stdout


def test_bitwarden_sync_remains_upstream_compat_alias(tmp_path):
    result = _run_cli(tmp_path, "secrets", "source", "bitwarden", "sync")

    assert result.returncode == 0, result.stderr + result.stdout
    assert "Bitwarden source refresh only" in result.stdout
