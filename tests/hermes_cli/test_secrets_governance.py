from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from hermes_cli import secrets_governance as gov


def _base_manifest(env_path: Path) -> dict:
    return {
        "schema_version": 1,
        "bws_project": {"name": "hermes", "id": "project-hermes"},
        "targets": [
            {
                "name": "root/default",
                "path": str(env_path),
                "kind": "runtime-env",
                "keys": [
                    {
                        "canonical": "ROOT_API_KEY",
                        "projection": "ROOT_API_KEY",
                        "source": {"type": "bws", "project": "hermes", "key": "ROOT_API_KEY"},
                        "classification": "secret",
                        "status": "active",
                    }
                ],
            }
        ],
        "provider_bindings": [
            {"provider": "root", "env": "ROOT_API_KEY", "canonical": "ROOT_API_KEY"}
        ],
    }


def test_validate_manifest_rejects_cross_project_bws_source(tmp_path: Path) -> None:
    manifest = _base_manifest(tmp_path / ".env")
    manifest["targets"][0]["keys"][0]["source"]["project"] = "other"

    errors = gov.validate_manifest(manifest)

    assert any("does not match bws_project" in error for error in errors)


def test_validate_manifest_rejects_duplicate_projection_even_when_retired(tmp_path: Path) -> None:
    manifest = _base_manifest(tmp_path / ".env")
    manifest["targets"][0]["keys"].append(
        {
            "canonical": "OLD_ROOT_API_KEY",
            "projection": "ROOT_API_KEY",
            "source": {"type": "bws", "project": "hermes", "key": "OLD_ROOT_API_KEY"},
            "classification": "secret",
            "status": "retired",
        }
    )

    errors = gov.validate_manifest(manifest)

    assert any("duplicate projection ROOT_API_KEY" in error for error in errors)


def test_governance_report_fails_local_only_secret_like_env_key(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ROOT_API_KEY=value\nUNDECLARED_TOKEN=value\nPLAIN_SETTING=1\n", encoding="utf-8")
    manifest = _base_manifest(env)

    report = gov.governance_report(manifest)

    assert not report.ok
    assert any("UNDECLARED_TOKEN" in error for error in report.errors)
    assert not any("PLAIN_SETTING" in error for error in report.errors)


def test_governance_report_fails_retired_projection_still_present(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ROOT_API_KEY=value\nOLD_API_KEY=value\n", encoding="utf-8")
    manifest = _base_manifest(env)
    manifest["targets"][0]["keys"].append(
        {
            "canonical": "OLD_API_KEY",
            "projection": "OLD_API_KEY",
            "source": {"type": "bws", "project": "hermes", "key": "OLD_API_KEY"},
            "classification": "secret",
            "status": "retired",
        }
    )

    report = gov.governance_report(manifest)

    assert not report.ok
    assert any("retired projection still present" in error for error in report.errors)


def test_cli_governance_check_emits_value_free_json(tmp_path: Path, capsys) -> None:
    env = tmp_path / ".env"
    env.write_text("ROOT_API_KEY=super-secret-value\n", encoding="utf-8")
    manifest = _base_manifest(env)
    manifest_path = tmp_path / "secrets-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    args = argparse.Namespace(manifest=str(manifest_path), env=None, json=True)

    assert gov.cmd_governance_check(args) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["ok"] is True
    assert "super-secret-value" not in output
