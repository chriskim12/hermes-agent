from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gateway.work_state import (
    WorkRecord,
    WorkStateStore,
    classify_ralph_first_progress_evidence,
)


def _record(*, now: datetime | None = None) -> WorkRecord:
    event_at = now or datetime.now(timezone.utc)
    return WorkRecord(
        work_id="CH-392",
        title="Ralph first progress watchdog",
        objective="separate surface materialization from useful Ralph progress",
        owner="hermes",
        executor="omx",
        mode="delegated",
        owner_session_id="agent:main:discord:thread",
        state="running",
        started_at=event_at,
        last_progress_at=event_at,
        next_action="Verify first useful Ralph progress before closeout",
        executor_session_id="proc-ralph-1",
        repo_path="/tmp/repo",
        worktree_path="/tmp/repo",
        proof="ralph_session_surface:pty_leader_injected",
        current_lane="ralph",
        planning_gate="closed",
        next_execution_branch="ralph",
        close_authority="hermes",
        surface_started_at=event_at,
        no_progress_watchdog_seconds=30,
        no_progress_deadline_at=event_at + timedelta(seconds=30),
    )


def test_pty_alive_but_no_first_progress_evidence_becomes_no_progress(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = WorkStateStore(tmp_path / "work-state.json")
    store.upsert(_record(now=now))

    result = store.apply_ralph_first_progress_watchdog(
        "CH-392",
        "agent:main:discord:thread",
        now=now + timedelta(seconds=31),
        evidence={"output": "PTY process still alive; no tool output yet", "progress_entries": []},
    )

    assert result["updated"] is True
    assert result["status"] == "no_progress"
    [record] = store.list_records()
    assert record.state == "handoff_needed"
    assert record.usable_outcome == "no_progress_theater"
    assert record.close_disposition == "close"
    assert record.first_progress_at is None
    assert record.blocked_reason == "first_progress_watchdog_expired"
    assert record.cleanup_required is True
    assert record.cleanup_proof == "no_diff_no_artifact_no_tool_no_progress_entry"
    assert "omx --madmax --high exec" in record.reroute_recommendation


def test_runtime_residue_only_is_no_progress_with_cleanup_accounting(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = WorkStateStore(tmp_path / "work-state.json")
    store.upsert(_record(now=now))

    result = store.apply_ralph_first_progress_watchdog(
        "CH-392",
        "agent:main:discord:thread",
        now=now + timedelta(seconds=5),
        evidence={"artifact_paths": [".omx/ralph-progress.json", ".clawhip/run.log"]},
    )

    assert result["status"] == "no_progress"
    [record] = store.list_records()
    assert record.state == "handoff_needed"
    assert record.blocked_reason == "runtime_residue_only"
    assert record.cleanup_required is True
    assert record.cleanup_proof == "runtime_residue_only_no_product_diff"
    assert record.no_progress_attempts == 1


def test_first_diff_tool_or_progress_entry_accepts_progress(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for evidence, proof_prefix in (
        ({"diff_stat": "app.py | 2 ++"}, "ralph_first_progress:diff_stat"),
        ({"tool_events": [{"tool": "terminal", "status": "ok"}]}, "ralph_first_progress:tool_event"),
        ({"progress_entries": [{"event": "patch_applied"}]}, "ralph_first_progress:progress_entries"),
    ):
        store = WorkStateStore(tmp_path / f"work-state-{proof_prefix.split(':')[-1]}.json")
        store.upsert(_record(now=now))

        result = store.apply_ralph_first_progress_watchdog(
            "CH-392",
            "agent:main:discord:thread",
            now=now + timedelta(seconds=10),
            evidence=evidence,
        )

        assert result["status"] == "progress"
        [record] = store.list_records()
        assert record.state == "running"
        assert record.first_progress_at == now + timedelta(seconds=10)
        assert record.first_progress_proof.startswith(proof_prefix)
        assert record.cleanup_required is False
        assert record.usable_outcome is None
        assert record.close_disposition is None


def test_approval_prompt_signature_blocks_and_requires_cleanup(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = WorkStateStore(tmp_path / "work-state.json")
    store.upsert(_record(now=now))

    result = store.apply_ralph_first_progress_watchdog(
        "CH-392",
        "agent:main:discord:thread",
        now=now + timedelta(seconds=3),
        evidence={"output": "Approval required: approve this command before continuing"},
    )

    assert result["status"] == "blocked"
    [record] = store.list_records()
    assert record.state == "blocked"
    assert record.usable_outcome == "blocked"
    assert record.blocked_reason == "approval_prompt"
    assert record.cleanup_required is True
    assert record.cleanup_proof == "task_owned_runtime_residue_must_be_closed"
    assert "omx --madmax --high exec" in record.reroute_recommendation


def test_bwrap_and_rtm_newaddr_output_are_actionable_blocked_reasons():
    bwrap = classify_ralph_first_progress_evidence(output="bwrap: failed to make loopback")
    rtm = classify_ralph_first_progress_evidence(output="RTM_NEWADDR operation not permitted")

    assert bwrap["status"] == "blocked"
    assert bwrap["blocked_reason"] == "sandbox_bwrap"
    assert rtm["status"] == "blocked"
    assert rtm["blocked_reason"] == "sandbox_rtm_newaddr"
