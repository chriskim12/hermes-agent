from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import hermes_bws_governance_smoke as smoke


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema": "hermes-runtime-secrets/v1",
                "project": "hermes",
                "entries": [
                    {
                        "key": "HERMES_GATEWAY_TOKEN",
                        "classification": "hermes-owned-secret",
                        "sourceOfTruth": {"type": "bws", "project": "hermes"},
                        "runtimeProjection": {"envFile": "~/.hermes/.env", "required": True},
                    },
                    {
                        "key": "LINEAR_API_KEY",
                        "classification": "hermes-owned-secret",
                        "sourceOfTruth": {"type": "bws", "project": "hermes"},
                        "runtimeProjection": {"envFile": "~/.hermes/.env", "required": False},
                    },
                ],
            }
        )
    )
    return path


def test_project_scoped_bws_lookup_does_not_accept_global_key_match(monkeypatch, tmp_path):
    manifest = smoke.load_manifest(_manifest(tmp_path))
    env_file = tmp_path / ".env"
    env_file.write_text("HERMES_GATEWAY_TOKEN=redacted\n")
    env_file.chmod(0o600)

    monkeypatch.setattr(smoke, "bws_project_ids_by_name", lambda: {"hermes": "project-hermes"})
    monkeypatch.setattr(smoke, "bws_keys_for_project", lambda project_id: {"HERMES_GATEWAY_TOKEN"})

    checks = smoke.evaluate(manifest, env_file, tmp_path)

    bws_check = next(check for check in checks if check.name == "bws_project_secret_keys")
    assert not bws_check.ok
    assert "hermes:LINEAR_API_KEY" in bws_check.detail


def test_project_scoped_bws_lookup_passes_when_declared_project_has_keys(monkeypatch, tmp_path):
    manifest = smoke.load_manifest(_manifest(tmp_path))
    env_file = tmp_path / ".env"
    env_file.write_text("HERMES_GATEWAY_TOKEN=redacted\n")
    env_file.chmod(0o600)

    monkeypatch.setattr(smoke, "bws_project_ids_by_name", lambda: {"hermes": "project-hermes"})
    monkeypatch.setattr(smoke, "bws_keys_for_project", lambda project_id: {"HERMES_GATEWAY_TOKEN", "LINEAR_API_KEY"})

    checks = smoke.evaluate(manifest, env_file, tmp_path)

    assert all(check.ok for check in checks)


def test_runtime_projection_reports_missing_required_key(tmp_path):
    manifest = smoke.load_manifest(_manifest(tmp_path))
    env_file = tmp_path / ".env"
    env_file.write_text("LINEAR_API_KEY=redacted\n")
    env_file.chmod(0o600)

    checks = smoke.evaluate(manifest, env_file, tmp_path, skip_bws=True)

    projection_check = next(check for check in checks if check.name == "runtime_projection_keys")
    assert not projection_check.ok
    assert projection_check.detail == "missing=HERMES_GATEWAY_TOKEN"


def test_inline_secret_scan_ignores_env_example_but_flags_tracked_assignment(tmp_path):
    (tmp_path / ".env.example").write_text("API_TOKEN=***\n")
    (tmp_path / "module.py").write_text("LINEAR_API_KEY=actual-looking-value\n")

    check = smoke.inline_secret_scan(tmp_path, {Path(".env.example")})

    assert not check.ok
    assert "module.py:LINEAR_API_KEY" in check.detail


def test_env_file_mode_blocks_group_or_world_readable(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("HERMES_GATEWAY_TOKEN=redacted\n")
    env_file.chmod(0o644)

    check = smoke.safe_mode(env_file)

    assert not check.ok
    assert check.detail == "mode=0o644"
