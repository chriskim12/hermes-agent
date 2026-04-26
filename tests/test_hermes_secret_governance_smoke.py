from __future__ import annotations

import json
import stat
from pathlib import Path

import yaml

from scripts.hermes_secret_governance_smoke import run_governance_smoke


def _write(path: Path, content: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def test_governance_smoke_passes_without_printing_secret_values(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    manifest = tmp_path / "manifest.yaml"
    _write(hermes_home / ".env", "DATABASE_URL=postgres://user:super-secret@example/db\n", 0o600)
    _write(hermes_home / "config.yaml", "provider: openai\napi_key_env: OPENAI_API_KEY\n", 0o600)
    _write(
        manifest,
        yaml.safe_dump(
            {
                "version": 1,
                "secrets": [
                    {
                        "name": "DATABASE_URL",
                        "owner": "hermes-runtime",
                        "writable_ssot": {
                            "type": "bitwarden-secrets-manager",
                            "project": "hermes",
                            "secret_name": "DATABASE_URL",
                        },
                        "projections": [
                            {"type": "env_file", "path": "~/.hermes/.env", "key": "DATABASE_URL"}
                        ],
                        "verification": ["projection_key_present", "bws_metadata_present"],
                    }
                ],
                "secret_bearing_files": [
                    {"path": "~/.hermes/.env", "max_mode": "0600"},
                    {"path": "~/.hermes/config.yaml", "max_mode": "0600"},
                ],
            },
            sort_keys=False,
        ),
    )
    bws = tmp_path / "bws.json"
    _write(bws, json.dumps([{"key": "DATABASE_URL", "id": "secret-id", "projectId": "project-id"}]))

    report = run_governance_smoke(
        manifest_path=manifest,
        hermes_home=hermes_home,
        bws_metadata_path=bws,
        scan_paths=[hermes_home / "config.yaml"],
    )

    assert report.ok is True
    assert report.summary == {"pass": 7, "warn": 0, "fail": 0}
    text = report.to_markdown()
    assert "super-secret" not in text
    assert "DATABASE_URL" in text
    assert "[REDACTED]" not in text  # report should not read/emit raw values at all


def test_governance_smoke_fails_for_missing_bws_metadata_and_loose_file_mode(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    manifest = tmp_path / "manifest.yaml"
    env_path = _write(hermes_home / ".env", "OPENAI_API_KEY=sk-test-secret\n", 0o664)
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o664
    _write(
        manifest,
        yaml.safe_dump(
            {
                "version": 1,
                "secrets": [
                    {
                        "name": "OPENAI_API_KEY",
                        "owner": "hermes-provider",
                        "writable_ssot": {
                            "type": "bitwarden-secrets-manager",
                            "project": "hermes",
                            "secret_name": "OPENAI_API_KEY",
                        },
                        "projections": [
                            {"type": "env_file", "path": "~/.hermes/.env", "key": "OPENAI_API_KEY"}
                        ],
                        "verification": ["projection_key_present", "bws_metadata_present"],
                    }
                ],
                "secret_bearing_files": [{"path": "~/.hermes/.env", "max_mode": "0600"}],
            },
            sort_keys=False,
        ),
    )
    bws = tmp_path / "bws.json"
    _write(bws, "[]")

    report = run_governance_smoke(
        manifest_path=manifest,
        hermes_home=hermes_home,
        bws_metadata_path=bws,
        scan_paths=[],
    )

    assert report.ok is False
    codes = {check.code for check in report.checks if check.status == "fail"}
    assert "bws_metadata_present" in codes
    assert "file_mode_max" in codes
    markdown = report.to_markdown()
    assert "sk-test-secret" not in markdown
    assert "0o664" in markdown


def test_governance_smoke_flags_inline_secret_assignment_in_scan_paths(tmp_path: Path) -> None:
    hermes_home = tmp_path / "hermes"
    manifest = tmp_path / "manifest.yaml"
    _write(hermes_home / ".env", "DATABASE_URL=postgres://user:pw@example/db\n", 0o600)
    prompt = _write(
        hermes_home / "cron" / "job.txt",
        "send report with DATABASE_URL=postgres://user:pw@example/db in prompt\n",
        0o600,
    )
    _write(
        manifest,
        yaml.safe_dump(
            {
                "version": 1,
                "secrets": [
                    {
                        "name": "DATABASE_URL",
                        "owner": "hermes-runtime",
                        "writable_ssot": {
                            "type": "bitwarden-secrets-manager",
                            "project": "hermes",
                            "secret_name": "DATABASE_URL",
                        },
                        "projections": [
                            {"type": "env_file", "path": "~/.hermes/.env", "key": "DATABASE_URL"}
                        ],
                        "verification": ["projection_key_present"],
                    }
                ],
                "secret_bearing_files": [],
            },
            sort_keys=False,
        ),
    )

    report = run_governance_smoke(
        manifest_path=manifest,
        hermes_home=hermes_home,
        bws_metadata_path=None,
        scan_paths=[prompt],
    )

    assert report.ok is False
    leaked = [check for check in report.checks if check.code == "inline_secret_assignment"]
    assert leaked and leaked[0].status == "fail"
    assert "postgres://user:pw" not in report.to_markdown()
