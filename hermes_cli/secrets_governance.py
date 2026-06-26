"""Value-free env/secret governance checks.

This module is intentionally read-only. It validates a BWS-style manifest and
runtime projection files without fetching, printing, or mutating secret values.
It is the upstream-compatible carry of Chris's stricter env/secret SSOT rule:
BWS/manifest is authority; local ``.env`` files are projections/cache.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

_SECRET_NAME_RE = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|API)", re.IGNORECASE
)
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ACTIVE_STATUSES = {
    "active",
    "manual-operator-input",
    "named-exception",
    "local-temporary-exception",
}
_ALLOWED_STATUSES = _ACTIVE_STATUSES | {"retired"}


@dataclass
class GovernanceReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    inventory: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "inventory": dict(self.inventory),
        }


def _load_yaml(path: str | Path) -> dict[str, Any]:
    loaded = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("manifest must be a mapping")
    return loaded


def _read_env_keys(path: str | Path) -> dict[str, str]:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return {}
    result: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if _ENV_KEY_RE.match(key):
            result[key] = value.strip().strip('"').strip("'")
    return result


def _project_aliases(project: Mapping[str, Any]) -> set[str]:
    aliases = set()
    for key in ("name", "id"):
        value = str(project.get(key) or "").strip()
        if value:
            aliases.add(value)
    return aliases


def validate_manifest(manifest: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    project = manifest.get("bws_project")
    if project is None:
        project = {}
    if not isinstance(project, Mapping):
        errors.append("bws_project must be a mapping")
        project = {}
    project_aliases = _project_aliases(project)

    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        errors.append("targets must be a non-empty list")
        targets = []

    projection_seen: dict[str, str] = {}
    bws_source_seen = False
    for target_index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            errors.append(f"targets[{target_index}] must be a mapping")
            continue
        target_name = str(target.get("name") or f"target[{target_index}]")
        path = str(target.get("path") or "").strip()
        if not path:
            errors.append(f"{target_name}: path is required")
        keys = target.get("keys")
        if not isinstance(keys, list):
            errors.append(f"{target_name}: keys must be a list")
            continue
        for key_index, item in enumerate(keys):
            if not isinstance(item, Mapping):
                errors.append(f"{target_name}.keys[{key_index}] must be a mapping")
                continue
            canonical = str(item.get("canonical") or "").strip()
            projection = str(item.get("projection") or canonical).strip()
            status = str(item.get("status") or "active").strip()
            source = item.get("source") or {}
            if not canonical:
                errors.append(f"{target_name}.keys[{key_index}]: canonical is required")
            if not projection:
                errors.append(f"{target_name}.{canonical or key_index}: projection is required")
            if status not in _ALLOWED_STATUSES:
                errors.append(f"{target_name}.{canonical}: invalid status {status!r}")
            if projection:
                previous = projection_seen.get(projection)
                owner = f"{target_name}.{canonical or key_index}({status})"
                if previous is not None:
                    errors.append(f"duplicate projection {projection}: {previous} and {owner}")
                else:
                    projection_seen[projection] = owner
            if isinstance(source, Mapping) and source.get("type") == "bws":
                bws_source_seen = True
                src_project = str(source.get("project") or "").strip()
                if src_project and project_aliases and src_project not in project_aliases:
                    errors.append(
                        f"{target_name}.{canonical}: source project {src_project!r} does not match bws_project"
                    )
                if not source.get("key"):
                    errors.append(f"{target_name}.{canonical}: bws source key is required")

    if bws_source_seen and not str(project.get("id") or "").strip():
        errors.append("bws_project.id is required when bws sources are present")

    bindings = manifest.get("provider_bindings") or []
    if not isinstance(bindings, list):
        errors.append("provider_bindings must be a list")
    else:
        for idx, binding in enumerate(bindings):
            if not isinstance(binding, Mapping):
                errors.append(f"provider_bindings[{idx}] must be a mapping")
                continue
            env_key = str(binding.get("env") or binding.get("projection") or "").strip()
            canonical = str(binding.get("canonical") or "").strip()
            if env_key and env_key not in projection_seen:
                errors.append(f"provider binding {idx}: env {env_key} is not declared as a projection")
            if canonical and canonical not in " ".join(projection_seen.values()):
                errors.append(f"provider binding {idx}: canonical {canonical} is not declared in targets")
    return errors


def governance_report(manifest: Mapping[str, Any], *, env_paths: list[str] | None = None) -> GovernanceReport:
    report = GovernanceReport()
    report.errors.extend(validate_manifest(manifest))

    declared: dict[str, dict[str, str]] = {}
    retired: set[str] = set()
    targets_raw = manifest.get("targets")
    targets: Sequence[Any] = targets_raw if isinstance(targets_raw, list) else []
    default_env_paths: list[str] = []
    for target in targets:
        if not isinstance(target, Mapping):
            continue
        path = str(target.get("path") or "").strip()
        if path:
            default_env_paths.append(path)
        for item in target.get("keys") or []:
            if not isinstance(item, Mapping):
                continue
            canonical = str(item.get("canonical") or "").strip()
            projection = str(item.get("projection") or canonical).strip()
            status = str(item.get("status") or "active").strip()
            if projection:
                declared[projection] = {"canonical": canonical, "status": status}
                if status == "retired":
                    retired.add(projection)

    checked_paths = env_paths or default_env_paths
    local_only_secret_keys: list[str] = []
    retired_present: list[str] = []
    for env_path in checked_paths:
        env = _read_env_keys(env_path)
        for key in sorted(env):
            if key in retired:
                retired_present.append(f"{key} in {env_path}")
            if key not in declared and _SECRET_NAME_RE.search(key):
                local_only_secret_keys.append(f"{key} in {env_path}")

    if retired_present:
        report.errors.extend(f"retired projection still present: {item}" for item in retired_present)
    if local_only_secret_keys:
        report.errors.extend(f"local-only secret-like key without manifest authority: {item}" for item in local_only_secret_keys)

    report.inventory = {
        "declared_projection_count": len(declared),
        "checked_env_paths": checked_paths,
        "local_only_secret_like_count": len(local_only_secret_keys),
        "retired_projection_present_count": len(retired_present),
    }
    return report


def cmd_governance_check(args: argparse.Namespace) -> int:
    try:
        manifest = _load_yaml(args.manifest)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: could not load manifest: {exc}", file=sys.stderr)
        return 2
    report = governance_report(manifest, env_paths=args.env or None)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        if report.ok:
            print("OK: env/secret governance checks passed")
        else:
            print("FAIL: env/secret governance checks failed")
            for error in report.errors:
                print(f"- {error}")
        for warning in report.warnings:
            print(f"WARN: {warning}")
    return 0 if report.ok else 1


def register_cli(parent_parser: argparse._SubParsersAction) -> None:
    parser = parent_parser.add_parser(
        "governance-check",
        help="Read-only env/secret SSOT governance check from a manifest",
    )
    parser.add_argument("--manifest", required=True, help="Path to secrets manifest YAML")
    parser.add_argument("--env", action="append", help="Runtime .env projection to check; repeatable")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.set_defaults(func=cmd_governance_check)
