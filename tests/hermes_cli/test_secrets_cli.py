"""Tests for hermes secrets CLI (Phase H)."""
import os
import pytest
import yaml
import tempfile
import json

# Skip if not in test environment
pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.expanduser("~/.hermes/secrets-manifest.yaml")),
    reason="Requires secrets manifest"
)


class TestSecretsCLI:
    """Smoke tests for hermes secrets commands."""

    def test_secrets_check_runs(self):
        """hermes secrets check should exit cleanly."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "hermes_cli.main", "secrets", "check"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": os.path.expanduser("~")},
            timeout=30,
        )
        assert "keys present" in result.stdout

    def test_secrets_sync_dry_run(self):
        """hermes secrets sync --dry-run should not modify files."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "hermes_cli.main", "secrets", "sync",
             "--target", "root/default", "--dry-run"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": os.path.expanduser("~")},
            timeout=30,
        )
        assert "DRY-RUN" in result.stdout
        assert "Would apply" in result.stdout

    def test_secrets_preflight_runs(self):
        """hermes secrets preflight should exit cleanly."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "hermes_cli.main", "secrets", "preflight"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": os.path.expanduser("~")},
            timeout=30,
        )
        assert "preflight passed" in result.stdout

    def test_secrets_provider_check(self):
        """hermes secrets provider-check should list bindings."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "hermes_cli.main", "secrets", "provider-check"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": os.path.expanduser("~")},
            timeout=30,
        )
        assert "Provider check complete" in result.stdout

    def test_secrets_break_glass_list(self):
        """hermes secrets break-glass --list should run."""
        import subprocess
        result = subprocess.run(
            ["python", "-m", "hermes_cli.main", "secrets", "break-glass", "--list"],
            capture_output=True, text=True,
            env={**os.environ, "HOME": os.path.expanduser("~")},
            timeout=30,
        )
        # Empty output is fine (no markers)
        assert result.returncode == 0

    def test_no_secret_values_in_output(self):
        """No command should leak raw secret values."""
        import subprocess
        commands = [
            ["python", "-m", "hermes_cli.main", "secrets", "sync", "--target", "root/default", "--dry-run"],
            ["python", "-m", "hermes_cli.main", "secrets", "check"],
            ["python", "-m", "hermes_cli.main", "secrets", "preflight"],
            ["python", "-m", "hermes_cli.main", "secrets", "provider-check"],
        ]
        for cmd in commands:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                env={**os.environ, "HOME": os.path.expanduser("~")},
                timeout=30,
            )
            # Check that no long base64-looking strings appear
            for line in result.stdout.split("\n"):
                # Secret values are typically long strings with special chars
                if len(line) > 60 and any(c in line for c in ["$", "!", "@"]):
                    # Allow BWS ID references and paths
                    if "bws_id" not in line and "/" not in line:
                        pytest.fail(f"Possible secret leak in {cmd[4]}: {line[:80]}")
