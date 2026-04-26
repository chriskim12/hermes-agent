#!/usr/bin/env python3
"""Read-only Hermes secret SSOT governance smoke.

This script verifies that Hermes runtime secrets are represented as projections
from a declared writable SSOT. It intentionally checks metadata, key presence,
file modes, and obvious inline secret assignments without printing raw values.
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

import yaml

SAFE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
INLINE_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b([A-Z][A-Z0-9_]{2,})\s*=\s*([^\s'\"]{8,})"
)
SECRETISH_KEY_RE = re.compile(r"(?:TOKEN|SECRET|KEY|PASSWORD|DATABASE_URL|DB_URL|API_KEY)")


@dataclass(frozen=True)
class Check:
    code: str
    status: str
    subject: str
    detail: str


@dataclass(frozen=True)
class SmokeReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    @property
    def summary(self) -> dict[str, int]:
        counts = {"pass": 0, "warn": 0, "fail": 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def to_markdown(self) -> str:
        lines = [
            "# Hermes Secret Governance Smoke",
            "",
            f"Status: {'PASS' if self.ok else 'FAIL'}",
            f"Summary: pass={self.summary.get('pass', 0)} warn={self.summary.get('warn', 0)} fail={self.summary.get('fail', 0)}",
            "",
            "| status | code | subject | detail |",
            "|---|---|---|---|",
        ]
        for check in self.checks:
            lines.append(
                "| {status} | {code} | {subject} | {detail} |".format(
                    status=check.status,
                    code=_md(check.code),
                    subject=_md(check.subject),
                    detail=_md(check.detail),
                )
            )
        lines.append("")
        return "\n".join(lines)


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"manifest must be a mapping: {path}")
    return data


def _expand(path_text: str, hermes_home: Path) -> Path:
    if path_text == "~/.hermes":
        return hermes_home
    if path_text.startswith("~/.hermes/"):
        return hermes_home / path_text.removeprefix("~/.hermes/")
    return Path(os.path.expanduser(path_text))


def _parse_env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
            if SAFE_KEY_RE.match(key):
                keys.add(key)
    return keys


def _load_bws_metadata(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("secrets"), list):
        items = data["secrets"]
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("BWS metadata must be a list or {'secrets': [...]} object")
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("key") or item.get("name") or item.get("secretName")
        if isinstance(name, str):
            out[name] = item
    return out


def _load_bws_metadata_from_cli() -> dict[str, dict[str, Any]]:
    proc = subprocess.run(
        ["bws", "secret", "list", "--output", "json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError("bws secret list failed; not printing stderr because it may contain sensitive context")
    tmp = json.loads(proc.stdout)
    if not isinstance(tmp, list):
        raise ValueError("unexpected bws output shape")
    return _load_bws_metadata_from_object(tmp)


def _load_bws_metadata_from_object(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        name = item.get("key") or item.get("name") or item.get("secretName")
        if isinstance(name, str):
            out[name] = item
    return out


def _mode(path: Path) -> int | None:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return None


def _check_projection(secret: dict[str, Any], hermes_home: Path) -> Iterable[Check]:
    name = str(secret.get("name", ""))
    projections = secret.get("projections") or []
    if not projections:
        yield Check("projection_declared", "fail", name, "no projections declared")
        return
    for projection in projections:
        if not isinstance(projection, dict):
            yield Check("projection_declared", "fail", name, "projection entry is not a mapping")
            continue
        if projection.get("type") != "env_file":
            yield Check("projection_declared", "warn", name, f"unsupported projection type {projection.get('type')}")
            continue
        path = _expand(str(projection.get("path", "")), hermes_home)
        key = str(projection.get("key") or name)
        keys = _parse_env_keys(path)
        if key in keys:
            yield Check("projection_key_present", "pass", key, f"key present in {projection.get('path')}")
        elif path.exists():
            yield Check("projection_key_present", "fail", key, f"key missing from {projection.get('path')}")
        else:
            yield Check("projection_key_present", "fail", key, f"projection file missing: {projection.get('path')}")


def _check_bws(secret: dict[str, Any], metadata: dict[str, dict[str, Any]], bws_metadata_available: bool) -> Check:
    name = str(secret.get("name", ""))
    ssot = secret.get("writable_ssot") or {}
    expected = str(ssot.get("secret_name") or name)
    if not bws_metadata_available:
        return Check("bws_metadata_present", "warn", expected, "BWS metadata unavailable; skipped read-only SSOT metadata check")
    if expected in metadata:
        return Check("bws_metadata_present", "pass", expected, "Bitwarden metadata entry exists")
    return Check("bws_metadata_present", "fail", expected, "Bitwarden metadata entry missing")


def _check_file_modes(entries: list[Any], hermes_home: Path) -> Iterable[Check]:
    for entry in entries:
        if not isinstance(entry, dict):
            yield Check("file_mode_max", "fail", "<invalid>", "secret_bearing_files entry is not a mapping")
            continue
        declared_path = str(entry.get("path", ""))
        path = _expand(declared_path, hermes_home)
        max_mode_text = str(entry.get("max_mode", "0600"))
        max_mode = int(max_mode_text, 8)
        mode = _mode(path)
        if mode is None:
            yield Check("file_mode_max", "warn", declared_path, "file missing; mode check skipped")
        elif mode & ~max_mode:
            yield Check("file_mode_max", "fail", declared_path, f"mode {oct(mode)} exceeds max {max_mode_text}")
        else:
            yield Check("file_mode_max", "pass", declared_path, f"mode {oct(mode)} within max {max_mode_text}")


def _scan_inline_secret_assignments(paths: Iterable[Path], hermes_home: Path) -> Iterable[Check]:
    for raw_path in paths:
        path = _expand(os.fspath(raw_path), hermes_home)
        if path.is_dir():
            candidates = [p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        else:
            candidates = [path]
        scanned = 0
        findings: dict[str, set[str]] = {}
        for candidate in candidates:
            if not candidate.exists() or candidate.stat().st_size > 1_000_000:
                continue
            scanned += 1
            text = candidate.read_text(encoding="utf-8", errors="replace")
            matches = [
                m for m in INLINE_SECRET_ASSIGNMENT_RE.finditer(text)
                if SECRETISH_KEY_RE.search(m.group(1))
            ]
            if matches:
                findings[str(candidate)] = {m.group(1) for m in matches}
        subject = str(path)
        if findings:
            compact = "; ".join(
                f"{file}: {', '.join(sorted(names))}" for file, names in sorted(findings.items())[:10]
            )
            extra = "" if len(findings) <= 10 else f"; +{len(findings) - 10} more files"
            yield Check("inline_secret_assignment", "fail", subject, f"possible inline secret assignment for keys: {compact}{extra}")
        else:
            yield Check("inline_secret_assignment", "pass", subject, f"no obvious inline secret assignment across {scanned} files")


def run_governance_smoke(
    *,
    manifest_path: Path,
    hermes_home: Path,
    bws_metadata_path: Path | None,
    scan_paths: list[Path | str],
    bws_metadata: dict[str, dict[str, Any]] | None = None,
) -> SmokeReport:
    manifest = _load_yaml(manifest_path)
    bws_available = bws_metadata_path is not None or bws_metadata is not None
    if bws_metadata is None:
        bws_metadata = _load_bws_metadata(bws_metadata_path) if bws_metadata_path is not None else {}
    checks: list[Check] = []

    version = manifest.get("version")
    checks.append(Check("manifest_version", "pass" if version else "fail", str(manifest_path), f"version={version!r}"))

    secrets = manifest.get("secrets") or []
    if not isinstance(secrets, list) or not secrets:
        checks.append(Check("manifest_secrets", "fail", str(manifest_path), "no secrets declared"))
        secrets = []

    for secret in secrets:
        if not isinstance(secret, dict):
            checks.append(Check("manifest_secret_entry", "fail", str(manifest_path), "secret entry is not a mapping"))
            continue
        name = str(secret.get("name", ""))
        ssot = secret.get("writable_ssot") or {}
        if ssot.get("type") == "bitwarden-secrets-manager":
            checks.append(Check("writable_ssot_declared", "pass", name, "Bitwarden Secrets Manager"))
        else:
            checks.append(Check("writable_ssot_declared", "fail", name, "writable SSOT must be bitwarden-secrets-manager"))
        checks.extend(_check_projection(secret, hermes_home))
        if "bws_metadata_present" in (secret.get("verification") or []):
            checks.append(_check_bws(secret, bws_metadata, bws_available))

    files = manifest.get("secret_bearing_files") or []
    checks.extend(_check_file_modes(files, hermes_home))
    checks.extend(_scan_inline_secret_assignments(scan_paths, hermes_home))
    return SmokeReport(checks)


def _default_manifest() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "hermes-secret-ssot-manifest.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Hermes secret governance smoke")
    parser.add_argument("--manifest", type=Path, default=_default_manifest())
    parser.add_argument("--hermes-home", type=Path, default=Path.home() / ".hermes")
    parser.add_argument("--bws-metadata", type=Path, default=None, help="JSON metadata from 'bws secret list --output json'. Values are never required.")
    parser.add_argument("--use-bws-cli", action="store_true", help="Fetch Bitwarden metadata with bws secret list. Read-only; no secret values are printed.")
    parser.add_argument("--scan-path", type=Path, action="append", default=[], help="File or directory to scan for obvious inline secret assignments; may repeat.")
    parser.add_argument("--output", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args(argv)

    bws_path = args.bws_metadata
    bws_metadata_override: dict[str, dict[str, Any]] | None = None
    if args.use_bws_cli:
        try:
            bws_metadata_override = _load_bws_metadata_from_cli()
        except Exception as exc:  # noqa: BLE001 - CLI should fail safely and without sensitive stderr
            print(f"BWS metadata read failed: {exc}", file=sys.stderr)
            return 2

    scan_paths = args.scan_path
    if not scan_paths:
        manifest_data = _load_yaml(args.manifest)
        scan_paths = [Path(p) for p in (manifest_data.get("scan_paths") or [])]
    report = run_governance_smoke(
        manifest_path=args.manifest,
        hermes_home=args.hermes_home,
        bws_metadata_path=bws_path,
        scan_paths=scan_paths,
        bws_metadata=bws_metadata_override,
    )
    if args.output == "json":
        print(json.dumps({"ok": report.ok, "summary": report.summary, "checks": [check.__dict__ for check in report.checks]}, indent=2))
    else:
        print(report.to_markdown())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
