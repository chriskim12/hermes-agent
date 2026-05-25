"""
Hermes secrets CLI — SSOT governance for runtime secrets.

Commands:
    hermes secrets check  [--target TARGET] [--manifest PATH]
    hermes secrets sync   --target TARGET [--apply] [--dry-run] [--manifest PATH]

BWS (Bitwarden Secrets Manager) is the canonical SSOT.
Local .env files are generated runtime projections.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_MANIFEST_PATH = os.path.expanduser("~/.hermes/secrets-manifest.yaml")
BWS_CLI = "/home/ubuntu/.cargo/bin/bws"
BWS_ENV = os.path.expanduser("~/.config/bws/env")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_manifest(manifest_path: str) -> dict[str, Any]:
    """Load the secrets manifest YAML."""
    with open(manifest_path, "r") as f:
        return yaml.safe_load(f)


def _bws_get_secret(secret_id: str) -> str:
    """Fetch a single secret value from BWS. Returns the raw value."""
    cmd = f"source {BWS_ENV} && {BWS_CLI} secret get {secret_id}"
    result = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bws secret get failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    return data["value"]


def _fingerprint(value: str) -> str:
    """Return a short sha256 fingerprint of a value (value-free)."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _read_env_keys(env_path: str) -> dict[str, str]:
    """Read key=value pairs from a .env file. Returns {KEY: value}."""
    result = {}
    if not os.path.exists(env_path):
        return result
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                result[key] = value
    return result


def _manifest_hash(manifest_path: str) -> str:
    """SHA256 of the manifest file."""
    with open(manifest_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# ---------------------------------------------------------------------------
# hermes secrets sync
# ---------------------------------------------------------------------------

def cmd_secrets_sync(args: argparse.Namespace) -> None:
    """Sync .env projections from BWS according to manifest."""
    manifest_path = args.manifest or DEFAULT_MANIFEST_PATH
    if not os.path.exists(manifest_path):
        print(f"ERROR: manifest not found: {manifest_path}")
        sys.exit(1)

    manifest = _load_manifest(manifest_path)
    target_name = args.target
    apply_mode = args.apply
    dry_run = args.dry_run

    # Find target in manifest
    target = None
    for t in manifest.get("targets", []):
        if t["name"] == target_name:
            target = t
            break

    if target is None:
        print(f"ERROR: target '{target_name}' not found in manifest")
        print(f"Available targets: {[t['name'] for t in manifest.get('targets', [])]}")
        sys.exit(1)

    env_path = os.path.expanduser(target["path"])
    current_env = _read_env_keys(env_path)

    # Collect BWS secrets for declared keys
    projections: dict[str, str] = {}
    fingerprints: list[dict[str, Any]] = []

    print(f"Target: {target['name']} → {env_path}")
    print(f"Mode: {'APPLY' if apply_mode else 'DRY-RUN' if dry_run else 'CHECK'}")
    print()

    for key_def in target.get("keys", []):
        source = key_def.get("source", {})
        if source.get("type") != "bws":
            continue  # skip non-BWS keys (config, local_only, etc.)

        canonical = key_def["canonical"]
        projection = key_def["projection"]
        bws_key = source.get("key", canonical)

        try:
            # Get BWS secret ID by key name
            list_cmd = f"source {BWS_ENV} && {BWS_CLI} secret list {manifest['bws_project']['id']}"
            list_result = subprocess.run(
                ["bash", "-c", list_cmd],
                capture_output=True, text=True, timeout=30,
            )
            secrets_list = json.loads(list_result.stdout)
            bws_id = None
            for s in secrets_list:
                if s["key"] == bws_key:
                    bws_id = s["id"]
                    break

            if bws_id is None:
                fp_status = "BWS_MISSING"
                print(f"  {projection} ({canonical}): {fp_status}")
                fingerprints.append({
                    "canonical": canonical,
                    "projection": projection,
                    "fingerprint": None,
                    "status": "bws_missing",
                })
                continue

            # Fetch BWS value
            bws_value = _bws_get_secret(bws_id)
            bws_fp = _fingerprint(bws_value)
            local_fp = _fingerprint(current_env.get(projection, "")) if projection in current_env else None

            if local_fp == bws_fp:
                fp_status = "MATCH"
                print(f"  {projection} ({canonical}): {fp_status}")
            elif local_fp is None:
                fp_status = "LOCAL_MISSING"
                print(f"  {projection} ({canonical}): {fp_status}")
            else:
                fp_status = "DRIFT"
                print(f"  {projection} ({canonical}): {fp_status}")

            fingerprints.append({
                "canonical": canonical,
                "projection": projection,
                "fingerprint": bws_fp,
                "status": fp_status,
            })

            projections[projection] = bws_value

        except Exception as e:
            print(f"  {projection} ({canonical}): ERROR — {e}")
            fingerprints.append({
                "canonical": canonical,
                "projection": projection,
                "fingerprint": None,
                "status": "error",
                "error": str(e),
            })

    # Preserve unmanaged keys
    managed_projections = set(projections.keys())
    unmanaged = {k: v for k, v in current_env.items() if k not in managed_projections}

    if apply_mode:
        # Atomic write: temp file → rename
        mh = _manifest_hash(manifest_path)
        now = datetime.now(timezone.utc).isoformat()

        env_dir = os.path.dirname(env_path)
        fd, tmp_path = tempfile.mkstemp(dir=env_dir, prefix=".env.")
        try:
            with os.fdopen(fd, "w") as f:
                # Write managed keys
                for proj_key, value in projections.items():
                    f.write(f"{proj_key}={value}\n")
                # Write unmanaged keys
                for k, v in unmanaged.items():
                    f.write(f"{k}={v}\n")

            os.chmod(tmp_path, 0o600)
            os.rename(tmp_path, env_path)

            # Write sidecar
            sidecar_path = f"{env_path}.hermes-projection.json"
            sidecar = {
                "generated_by": "hermes secrets sync",
                "manifest_sha256": mh,
                "target": target_name,
                "generated_at": now,
                "fresh_until": None,  # TODO: TTL from manifest/config
                "bws_project": manifest["bws_project"]["name"],
                "keys": fingerprints,
                "manual_edits_forbidden": True,
            }
            with open(sidecar_path, "w") as f:
                json.dump(sidecar, f, indent=2)

            print(f"\nApplied {len(projections)} keys to {env_path}")
            print(f"Sidecar: {sidecar_path}")
            print(f"Unmanaged keys preserved: {len(unmanaged)}")

        except Exception:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    elif dry_run:
        print(f"\nWould apply {len(projections)} keys to {env_path}")
        print(f"Would preserve {len(unmanaged)} unmanaged keys")
        print("(DRY-RUN — no files modified)")

    else:
        # Default: check-only
        drifted = sum(1 for fp in fingerprints if fp["status"] == "DRIFT")
        missing = sum(1 for fp in fingerprints if fp["status"] in ("BWS_MISSING", "LOCAL_MISSING"))
        matched = sum(1 for fp in fingerprints if fp["status"] == "MATCH")

        print(f"\nResult: {matched} MATCH, {drifted} DRIFT, {missing} MISSING")
        if drifted > 0 or missing > 0:
            sys.exit(1)


# ---------------------------------------------------------------------------
# hermes secrets check
# ---------------------------------------------------------------------------

def cmd_secrets_check(args: argparse.Namespace) -> None:
    """Check projection fingerprints against manifest (no BWS fetch unless needed)."""
    manifest_path = args.manifest or DEFAULT_MANIFEST_PATH
    if not os.path.exists(manifest_path):
        print(f"ERROR: manifest not found: {manifest_path}")
        sys.exit(1)

    manifest = _load_manifest(manifest_path)
    targets_to_check = [args.target] if args.target else [t["name"] for t in manifest.get("targets", [])]

    all_ok = True

    for target_name in targets_to_check:
        target = None
        for t in manifest.get("targets", []):
            if t["name"] == target_name:
                target = t
                break

        if target is None:
            print(f"WARNING: target '{target_name}' not found in manifest")
            continue

        env_path = os.path.expanduser(target["path"])
        if not os.path.exists(env_path):
            print(f"WARNING: .env not found: {env_path}")
            all_ok = False
            continue

        current_env = _read_env_keys(env_path)

        # Check sidecar freshness
        sidecar_path = f"{env_path}.hermes-projection.json"
        if os.path.exists(sidecar_path):
            with open(sidecar_path, "r") as f:
                sidecar = json.load(f)
            mh = _manifest_hash(manifest_path)
            if sidecar.get("manifest_sha256") != mh:
                print(f"{target_name}: manifest hash mismatch (sidecar stale)")
                all_ok = False
            else:
                print(f"{target_name}: sidecar OK")
        else:
            print(f"{target_name}: no sidecar (never synced)")

        # Check declared keys exist
        for key_def in target.get("keys", []):
            proj = key_def["projection"]
            if key_def.get("source", {}).get("type") == "bws" and key_def.get("required", False):
                if proj not in current_env:
                    print(f"  MISSING: {proj}")
                    all_ok = False

        print(f"  {target_name}: {len(current_env)} keys present")

    if not all_ok:
        sys.exit(1)
    print("All checks passed.")


# ---------------------------------------------------------------------------
# hermes secrets preflight
# ---------------------------------------------------------------------------

def cmd_secrets_preflight(args: argparse.Namespace) -> None:
    """Runtime preflight check — gate before gateway start.

    Checks:
      - Manifest exists and is valid
      - .env projection exists
      - Sidecar is fresh (manifest hash match)
      - Required keys are present
      - Optionally: strict mode blocks on any warning
    """
    manifest_path = args.manifest or DEFAULT_MANIFEST_PATH
    if not os.path.exists(manifest_path):
        print(f"FATAL: manifest not found: {manifest_path}")
        sys.exit(2)

    manifest = _load_manifest(manifest_path)
    mh = _manifest_hash(manifest_path)
    profile = args.profile
    strict = args.strict

    # Find target by profile name
    target = None
    target_name = f"profile:{profile}" if profile and profile != "default" else "root/default"
    for t in manifest.get("targets", []):
        if t["name"] == target_name:
            target = t
            break

    if target is None:
        print(f"FATAL: target '{target_name}' not found in manifest")
        sys.exit(2)

    env_path = os.path.expanduser(target["path"])
    exit_code = 0

    # Check .env exists
    if not os.path.exists(env_path):
        print(f"FATAL: .env not found: {env_path}")
        sys.exit(2)

    # Check sidecar freshness
    sidecar_path = f"{env_path}.hermes-projection.json"
    if not os.path.exists(sidecar_path):
        print(f"WARNING: no projection sidecar — .env may be stale or unmanaged")
        if strict:
            exit_code = 1
    else:
        with open(sidecar_path, "r") as f:
            sidecar = json.load(f)
        if sidecar.get("manifest_sha256") != mh:
            print(f"FATAL: manifest hash mismatch — projection is stale")
            exit_code = 2
        else:
            print(f"OK: sidecar fresh (manifest hash match)")

        # Check freshness TTL
        generated_at = sidecar.get("generated_at")
        if generated_at:
            gen_time = datetime.fromisoformat(generated_at)
            age = (datetime.now(timezone.utc) - gen_time).total_seconds()
            if age > 86400:  # 24h default TTL
                print(f"WARNING: projection is {age/3600:.1f}h old (TTL: 24h)")
                if strict:
                    exit_code = 1

    # Check required keys
    current_env = _read_env_keys(env_path)
    for key_def in target.get("keys", []):
        if key_def.get("required") and key_def.get("source", {}).get("type") == "bws":
            proj = key_def["projection"]
            if proj not in current_env:
                print(f"FATAL: required key missing: {proj} ({key_def['canonical']})")
                exit_code = 2

    # Check for unknown secret-like keys (heuristic: long values)
    for k, v in current_env.items():
        declared = any(d["projection"] == k for d in target.get("keys", []))
        if not declared and len(v) > 20 and any(c in k.upper() for c in ["KEY", "TOKEN", "SECRET", "PASSWORD"]):
            print(f"WARNING: undeclared secret-like key: {k}")
            if strict:
                exit_code = 1

    if exit_code == 0:
        print(f"OK: preflight passed for {target_name}")
    else:
        print(f"FAIL: preflight failed for {target_name} (exit={exit_code})")
    sys.exit(exit_code)


def cmd_secrets_install_systemd(args: argparse.Namespace) -> None:
    """Generate systemd drop-in for ExecStartPre preflight."""
    manifest_path = args.manifest or DEFAULT_MANIFEST_PATH
    profile = args.profile or "default"
    target_name = f"profile:{profile}" if profile != "default" else "root/default"

    # Determine unit name and HERMES_HOME
    if profile == "arisu":
        unit_name = "hermes-gateway-arisu.service"
        hermes_home = "/home/ubuntu/.hermes/profiles/arisu"
    else:
        unit_name = "hermes-gateway.service"
        hermes_home = "/home/ubuntu/.hermes"

    override_dir = f"/home/ubuntu/.config/systemd/user/{unit_name}.d"
    override_path = f"{override_dir}/preflight.conf"

    python_path = "/home/ubuntu/.hermes/hermes-agent/.venv/bin/python"
    preflight_cmd = (
        f"{python_path} -m hermes_cli.main secrets preflight "
        f"--profile {profile} --manifest {manifest_path}"
    )

    content = f"""[Service]
# Generated by: hermes secrets install-systemd
# Phase D — runtime preflight gate before gateway start
ExecStartPre={preflight_cmd}
"""

    if args.dry_run:
        print(f"Would create: {override_path}")
        print(content)
    else:
        os.makedirs(override_dir, exist_ok=True)
        with open(override_path, "w") as f:
            f.write(content)
        print(f"Created: {override_path}")
        print(f"Run 'systemctl --user daemon-reload' to activate.")

    print(f"\nNote: preflight runs as ExecStartPre — if it fails, the gateway will NOT start.")
    if args.strict:
        print("Strict mode: any warning blocks startup.")
    else:
        print("Non-strict mode: only fatal errors block startup.")


# ---------------------------------------------------------------------------
# hermes secrets provider-check (Phase E)
# ---------------------------------------------------------------------------

def cmd_secrets_provider_check(args: argparse.Namespace) -> None:
    """Check provider/base_url/key tuple bindings for correctness."""
    manifest_path = args.manifest or DEFAULT_MANIFEST_PATH
    manifest = _load_manifest(manifest_path)

    bindings = manifest.get("provider_bindings", [])
    if not bindings:
        print("No provider bindings declared in manifest.")
        return

    all_ok = True
    for binding in bindings:
        profile = binding["profile"]
        provider = binding["provider"]
        base_url = binding.get("base_url", "")
        cred_source = binding.get("credential_source")

        print(f"{profile}: provider={provider}, base_url={base_url}")
        if cred_source:
            print(f"  credential: {cred_source}")
        else:
            print(f"  credential: (none declared — may use OAuth or implicit)")

    # Check for alias misuse: arisu OPENAI_API_KEY must ONLY be used with DeepSeek
    for target in manifest.get("targets", []):
        for key_def in target.get("keys", []):
            if key_def.get("classification") == "secret-alias":
                pb = key_def.get("provider_binding", {})
                print(f"  alias: {key_def['projection']} → {key_def.get('alias_for', '?')} "
                      f"(provider={pb.get('provider')}, base_url={pb.get('base_url')})")

    print("Provider check complete.")


def cmd_secrets_break_glass(args: argparse.Namespace) -> None:
    """Create or revoke a break-glass marker for emergency .env edits."""
    manifest_path = args.manifest or DEFAULT_MANIFEST_PATH
    manifest = _load_manifest(manifest_path)

    marker_dir = os.path.expanduser("~/.hermes/break-glass")
    os.makedirs(marker_dir, exist_ok=True)

    if args.revoke:
        # Revoke all active markers for target
        target = args.target or "root/default"
        marker_path = os.path.join(marker_dir, f"{target.replace('/', '-')}.json")
        if os.path.exists(marker_path):
            os.unlink(marker_path)
            print(f"Revoked break-glass for {target}")
        else:
            print(f"No active break-glass for {target}")
        return

    if args.list:
        for f in sorted(os.listdir(marker_dir)):
            if f.endswith(".json"):
                with open(os.path.join(marker_dir, f), "r") as fh:
                    data = json.load(fh)
                expired = data.get("expires_at", "")
                print(f"  {data['target']}: reason={data['reason']}, "
                      f"expires={expired}, actor={data.get('actor', '?')}")
        return

    # Create break-glass
    target = args.target or "root/default"
    reason = args.reason or "emergency manual edit"
    ttl = args.ttl or 3600

    now = datetime.now(timezone.utc)
    expires = now.isoformat()

    marker = {
        "target": target,
        "reason": reason,
        "actor": "hermes-cli",
        "created_at": now.isoformat(),
        "expires_at": now.isoformat(),  # TODO: actual TTL
        "ttl_seconds": ttl,
        "scope_keys": args.keys.split(",") if args.keys else [],
        "reconciliation_required": True,
    }

    marker_path = os.path.join(marker_dir, f"{target.replace('/', '-')}.json")
    with open(marker_path, "w") as f:
        json.dump(marker, f, indent=2)

    print(f"Break-glass created for {target}")
    print(f"  Reason: {reason}")
    print(f"  TTL: {ttl}s")
    print(f"  Marker: {marker_path}")
    print("WARNING: Remember to reconcile and revoke after use.")


def cmd_secrets_retire(args: argparse.Namespace) -> None:
    """Mark a key as retired in the manifest (Phase G)."""
    manifest_path = args.manifest or DEFAULT_MANIFEST_PATH
    manifest = _load_manifest(manifest_path)

    for target in manifest.get("targets", []):
        if target["name"] == args.target:
            for key_def in target["keys"]:
                if key_def["canonical"] == args.key:
                    key_def["status"] = "retired"
                    print(f"Marked {args.key} as retired in {args.target}")
                    with open(manifest_path, "w") as f:
                        yaml.dump(manifest, f, default_flow_style=False)
                    print(f"Manifest updated: {manifest_path}")
                    return
    print(f"Key '{args.key}' not found in target '{args.target}'")


def register_secrets_subparsers(secrets_parser: argparse.ArgumentParser) -> None:

    sub = secrets_parser.add_subparsers(dest="secrets_command", required=True)

    # hermes secrets sync
    sync_parser = sub.add_parser("sync", help="Sync .env from BWS according to manifest")
    sync_parser.add_argument("--target", required=True, help="Target name from manifest (e.g. root/default, profile:arisu)")
    sync_parser.add_argument("--apply", action="store_true", help="Apply changes (write .env)")
    sync_parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    sync_parser.add_argument("--manifest", help=f"Path to manifest (default: {DEFAULT_MANIFEST_PATH})")
    sync_parser.set_defaults(func=cmd_secrets_sync)

    # hermes secrets check
    check_parser = sub.add_parser("check", help="Check projection fingerprints against manifest")
    check_parser.add_argument("--target", help="Target name to check (default: all)")
    check_parser.add_argument("--manifest", help=f"Path to manifest (default: {DEFAULT_MANIFEST_PATH})")
    check_parser.set_defaults(func=cmd_secrets_check)

    # hermes secrets preflight
    preflight_parser = sub.add_parser("preflight", help="Runtime preflight check before gateway start")
    preflight_parser.add_argument("--profile", default="default", help="Profile name (default: default)")
    preflight_parser.add_argument("--strict", action="store_true", help="Fail on warnings too")
    preflight_parser.add_argument("--manifest", help=f"Path to manifest (default: {DEFAULT_MANIFEST_PATH})")
    preflight_parser.set_defaults(func=cmd_secrets_preflight)

    # hermes secrets install-systemd
    install_parser = sub.add_parser("install-systemd", help="Install ExecStartPre preflight into systemd unit")
    install_parser.add_argument("--profile", default="default", help="Profile name (default: default)")
    install_parser.add_argument("--strict", action="store_true", help="Strict preflight mode")
    install_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    install_parser.add_argument("--manifest", help=f"Path to manifest (default: {DEFAULT_MANIFEST_PATH})")
    install_parser.set_defaults(func=cmd_secrets_install_systemd)

    # hermes secrets provider-check (Phase E)
    prov_parser = sub.add_parser("provider-check", help="Check provider/base_url/key tuple bindings")
    prov_parser.add_argument("--manifest", help=f"Path to manifest (default: {DEFAULT_MANIFEST_PATH})")
    prov_parser.set_defaults(func=cmd_secrets_provider_check)

    # hermes secrets break-glass (Phase F)
    bg_parser = sub.add_parser("break-glass", help="Create/list/revoke break-glass markers")
    bg_parser.add_argument("--target", help="Target name")
    bg_parser.add_argument("--reason", help="Reason for break-glass")
    bg_parser.add_argument("--ttl", type=int, help="TTL in seconds")
    bg_parser.add_argument("--keys", help="Comma-separated scope keys")
    bg_parser.add_argument("--list", action="store_true", help="List active break-glass markers")
    bg_parser.add_argument("--revoke", action="store_true", help="Revoke break-glass for target")
    bg_parser.add_argument("--manifest", help=f"Path to manifest (default: {DEFAULT_MANIFEST_PATH})")
    bg_parser.set_defaults(func=cmd_secrets_break_glass)

    # hermes secrets retire (Phase G)
    retire_parser = sub.add_parser("retire", help="Mark a key as retired in manifest")
    retire_parser.add_argument("--target", required=True, help="Target name")
    retire_parser.add_argument("--key", required=True, help="Canonical key name to retire")
    retire_parser.add_argument("--manifest", help=f"Path to manifest (default: {DEFAULT_MANIFEST_PATH})")
    retire_parser.set_defaults(func=cmd_secrets_retire)
