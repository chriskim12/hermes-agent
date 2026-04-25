#!/usr/bin/env python3
"""Run the official OMX Hermes adapter baseline gate.

This is intentionally a thin wrapper around upstream ``omx adapt hermes``. It
keeps Hermes as owner and OMX as the observed adapter runtime: the gate reads
Hermes runtime evidence, writes no Hermes internals, and classifies adapter
readiness separately from actual runtime health.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_HERMES_ROOT = Path.home() / ".hermes" / "hermes-agent"
ADAPTER_NOT_INITIALIZED = "adapter_not_initialized"
PLANNING_ARTIFACTS_MISSING = "planning_artifacts_missing"


@dataclass(frozen=True)
class AdaptResult:
    command: str
    exit_code: int
    data: dict[str, Any]
    stderr: str = ""


def _run_json(command: list[str], *, cwd: Path, env: dict[str, str]) -> AdaptResult:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    try:
        data = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        data = {
            "parse_error": str(exc),
            "raw_stdout": completed.stdout,
        }
    return AdaptResult(
        command=" ".join(command),
        exit_code=completed.returncode,
        data=data,
        stderr=completed.stderr.strip(),
    )


def _issue_codes(doctor: dict[str, Any]) -> set[str]:
    return {
        str(issue.get("code"))
        for issue in doctor.get("issues", [])
        if isinstance(issue, dict) and issue.get("code")
    }


def classify_baseline(results: Iterable[AdaptResult]) -> dict[str, Any]:
    by_name: dict[str, AdaptResult] = {}
    for result in results:
        for name in ("probe", "status", "doctor"):
            if f" hermes {name} " in f" {result.command} ":
                by_name[name] = result

    probe = by_name.get("probe")
    status = by_name.get("status")
    doctor = by_name.get("doctor")
    runtime_state = None
    hermes_root = None
    if probe:
        runtime = probe.data.get("targetRuntime") or {}
        runtime_state = runtime.get("state")
        evidence = runtime.get("evidence") or {}
        hermes_root = evidence.get("hermesRoot")

    adapter_state = None
    if status:
        adapter_state = (status.data.get("adapter") or {}).get("state")

    issue_codes = _issue_codes(doctor.data if doctor else {})
    command_failures = [
        {"command": result.command, "exit_code": result.exit_code, "stderr": result.stderr}
        for result in by_name.values()
        if result.exit_code != 0 or "parse_error" in result.data
    ]
    runtime_ok = runtime_state == "running"
    expected_setup_gaps = issue_codes & {ADAPTER_NOT_INITIALIZED, PLANNING_ARTIFACTS_MISSING}
    unexpected_issues = sorted(issue_codes - {ADAPTER_NOT_INITIALIZED, PLANNING_ARTIFACTS_MISSING})

    status_text = "pass"
    if command_failures or not runtime_ok or unexpected_issues:
        status_text = "fail"
    elif expected_setup_gaps:
        status_text = "warn"

    return {
        "status": status_text,
        "runtime_state": runtime_state,
        "hermes_root": hermes_root,
        "adapter_state": adapter_state,
        "expected_setup_gaps": sorted(expected_setup_gaps),
        "unexpected_issues": unexpected_issues,
        "command_failures": command_failures,
    }


def run_baseline(*, hermes_root: Path, cwd: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["OMX_ADAPT_HERMES_ROOT"] = str(hermes_root)
    commands = [
        ["omx", "adapt", "hermes", "probe", "--json"],
        ["omx", "adapt", "hermes", "status", "--json"],
        ["omx", "adapt", "hermes", "doctor", "--json"],
    ]
    results = [_run_json(command, cwd=cwd, env=env) for command in commands]
    return {
        "gate": "omx_adapt_hermes_baseline",
        "cwd": str(cwd),
        "hermes_root": str(hermes_root),
        "classification": classify_baseline(results),
        "results": [
            {
                "command": result.command,
                "exit_code": result.exit_code,
                "stderr": result.stderr,
                "data": result.data,
            }
            for result in results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes-root",
        default=os.environ.get("OMX_ADAPT_HERMES_ROOT", str(DEFAULT_HERMES_ROOT)),
        help="Hermes root OMX should observe; defaults to ~/.hermes/hermes-agent.",
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Working directory for OMX-owned .omx/adapters/hermes evidence.",
    )
    args = parser.parse_args(argv)
    report = run_baseline(hermes_root=Path(args.hermes_root).expanduser(), cwd=Path(args.cwd).expanduser())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["classification"]["status"] in {"pass", "warn"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
