#!/usr/bin/env python3
"""Metadata-only Hermes Bitwarden SSOT smoke.

This smoke intentionally never prints or stores secret values. It validates that
manifest-declared runtime secret keys resolve inside the declared Bitwarden
Secrets Manager project, not merely somewhere in the account.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SECRET_ASSIGNMENT_RE = re.compile(r"(?m)^\s*(?:export\s+)?([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|DATABASE_URL|PRIVATE_OPENSSH_B64))\s*=\s*([^#\n]+)")
SAFE_PLACEHOLDER_RE = re.compile(r"^(?:\*+|<[^>]+>|[a-z0-9_-]*(?:\.\.\.|…)[a-z0-9_-]*|your[-_a-z0-9]*|generate[-_a-z0-9]*|example|changeme|xxx|)$", re.I)
DEFAULT_EXCLUDES = {
    ".env",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if data.get("schema") != "hermes-runtime-secrets/v1":
        raise ValueError(f"unsupported manifest schema: {data.get('schema')!r}")
    keys = [entry.get("key") for entry in data.get("entries", [])]
    if len(keys) != len(set(keys)):
        dupes = sorted({key for key in keys if keys.count(key) > 1})
        raise ValueError(f"duplicate manifest keys: {dupes}")
    return data


def env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    for line in path.read_text(errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            keys.add(key)
    return keys


def safe_mode(path: Path) -> Check:
    if not path.exists():
        return Check("env_file_mode", False, f"missing:{path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    return Check("env_file_mode", (mode & 0o077) == 0, f"mode={oct(mode)}")


def run_json(args: list[str]) -> Any:
    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-500:] or f"command failed: {args[:2]}")
    return json.loads(proc.stdout or "[]")


def bws_project_ids_by_name() -> dict[str, str]:
    projects = run_json(["bws", "project", "list", "--output", "json"])
    return {project.get("name"): project.get("id") for project in projects if project.get("name") and project.get("id")}


def bws_keys_for_project(project_id: str) -> set[str]:
    # Listing by project_id is the critical guard: it prevents global key-name
    # matches from satisfying a project-scoped manifest entry.
    secrets = run_json(["bws", "secret", "list", project_id, "--output", "json"])
    return {secret.get("key") for secret in secrets if secret.get("key")}


def iter_text_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if any(part in DEFAULT_EXCLUDES for part in path.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size > 512_000:
            continue
        yield path


def inline_secret_scan(root: Path, allowed_examples: set[Path], governed_keys: set[str] | None = None) -> Check:
    findings: list[str] = []
    governed_keys = governed_keys or set()
    for path in iter_text_files(root):
        rel = path.relative_to(root)
        if rel in allowed_examples:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for match in SECRET_ASSIGNMENT_RE.finditer(text):
            key = match.group(1)
            if governed_keys and key not in governed_keys:
                continue
            value = match.group(2).strip().strip('"\'')
            if SAFE_PLACEHOLDER_RE.match(value):
                continue
            findings.append(f"{rel}:{key}")
            if len(findings) >= 10:
                break
        if len(findings) >= 10:
            break
    if findings:
        return Check("inline_secret_scan", False, "possible inline assignments: " + ", ".join(findings))
    scope = "governed keys" if governed_keys else "secret-looking keys"
    return Check("inline_secret_scan", True, f"no tracked inline assignments found for {scope}")


def evaluate(manifest: dict[str, Any], env_file: Path, repo_root: Path, *, skip_bws: bool = False) -> list[Check]:
    entries = manifest.get("entries", [])
    checks: list[Check] = []
    checks.append(Check("manifest_entries", bool(entries), f"entries={len(entries)}"))
    checks.append(safe_mode(env_file))

    projected = env_keys(env_file)
    required_projection = [entry["key"] for entry in entries if entry.get("runtimeProjection", {}).get("required")]
    missing_projection = sorted(set(required_projection) - projected)
    checks.append(
        Check(
            "runtime_projection_keys",
            not missing_projection,
            "missing=" + ",".join(missing_projection) if missing_projection else f"present={len(required_projection)}",
        )
    )

    governed_keys = {entry["key"] for entry in entries}
    checks.append(inline_secret_scan(repo_root, {Path(".env.example")}, governed_keys))

    if skip_bws:
        checks.append(Check("bws_project_secret_keys", True, "skipped"))
        return checks

    project_ids = bws_project_ids_by_name()
    project_cache: dict[str, set[str]] = {}
    missing_by_project: dict[str, list[str]] = {}
    for entry in entries:
        sot = entry.get("sourceOfTruth", {})
        if sot.get("type") != "bws":
            continue
        project = sot.get("project")
        if not project:
            missing_by_project.setdefault("<unset>", []).append(entry["key"])
            continue
        project_id = project_ids.get(project)
        if not project_id:
            missing_by_project.setdefault(project, []).append(entry["key"])
            continue
        project_cache.setdefault(project, bws_keys_for_project(project_id))
        if entry["key"] not in project_cache[project]:
            missing_by_project.setdefault(project, []).append(entry["key"])

    if missing_by_project:
        safe_detail = "; ".join(f"{project}:" + ",".join(keys) for project, keys in sorted(missing_by_project.items()))
        checks.append(Check("bws_project_secret_keys", False, "missing=" + safe_detail))
    else:
        checked = sum(1 for entry in entries if entry.get("sourceOfTruth", {}).get("type") == "bws")
        projects = sorted({entry.get("sourceOfTruth", {}).get("project") for entry in entries if entry.get("sourceOfTruth", {}).get("type") == "bws"})
        checks.append(Check("bws_project_secret_keys", True, f"checked={checked}; projects={','.join(projects)}"))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Metadata-only Hermes BWS governance smoke")
    parser.add_argument("--manifest", type=Path, default=Path("ops/hermes-runtime-secrets.json"))
    parser.add_argument("--env-file", type=Path, default=Path.home() / ".hermes/.env")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--skip-bws", action="store_true", help="skip live BWS metadata lookup for unit/local tests")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    checks = evaluate(manifest, args.env_file.expanduser(), args.repo_root.resolve(), skip_bws=args.skip_bws)
    ok = all(check.ok for check in checks)
    result = {
        "ok": ok,
        "checks": [{"name": check.name, "ok": check.ok, "detail": check.detail} for check in checks],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
