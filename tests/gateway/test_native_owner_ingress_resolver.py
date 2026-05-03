from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from gateway.work_state import (
    WorkRecord,
    WorkStateStore,
    normalize_native_owner_ingress_packet,
)


def _delegated_record(
    *,
    work_id: str = "CH-359",
    owner_session_id: str = "discord:owner-thread",
    executor_session_id: str | None = "proc-omx-1",
    tmux_session: str | None = "omx-ch-359",
    repo_path: str | None = "/repo/hermes-agent",
    worktree_path: str | None = "/repo/hermes-agent/.worktrees/ch-359",
    state: str = "running",
) -> WorkRecord:
    now = datetime.now(timezone.utc)
    return WorkRecord(
        work_id=work_id,
        title="Native clawhip owner ingress",
        objective="resolve native clawhip/OMX signal to exactly one Hermes owner record",
        owner="hermes",
        executor="omx",
        mode="delegated",
        owner_session_id=owner_session_id,
        executor_session_id=executor_session_id,
        tmux_session=tmux_session,
        repo_path=repo_path,
        worktree_path=worktree_path,
        state=state,
        started_at=now,
        last_progress_at=now,
        next_action="wake owner with bounded follow-up",
    )


def test_representative_clawhip_packet_normalizes_contract_fields(tmp_path):
    packet = {
        "work": {"id": "CH-359"},
        "owner": {"name": "Hermes"},
        "executor": {"name": "OMX", "session_id": "proc-omx-1"},
        "event_type": "Action Required",
        "status": "blocked",
        "context": {
            "next_action": "inspect blocked OMX handoff",
            "proof": "clawhip:event:abc123",
            "repo_path": str(tmp_path / "repo"),
            "worktree_path": str(tmp_path / "repo/.worktrees/ch-359"),
            "repo_name": "hermes-agent",
        },
        "tmuxSession": "omx-ch-359",
    }

    normalized = normalize_native_owner_ingress_packet(packet)

    assert normalized["work_id"] == "CH-359"
    assert normalized["owner"] == "hermes"
    assert normalized["executor"] == "omx"
    assert normalized["state"] == "blocked"
    assert normalized["normalized_event"] == "action_required"
    assert normalized["next_action"] == "inspect blocked OMX handoff"
    assert normalized["proof"] == "clawhip:event:abc123"
    assert normalized["executor_session_id"] == "proc-omx-1"
    assert normalized["tmux_session"] == "omx-ch-359"
    assert normalized["repo_name"] == "hermes-agent"
    assert normalized["repo_path"] == str((tmp_path / "repo").resolve())
    assert normalized["worktree_path"] == str((tmp_path / "repo/.worktrees/ch-359").resolve())


def test_work_id_priority_wakes_single_resolved_owner_without_webhook_session(tmp_path):
    store = WorkStateStore(tmp_path / "work-state.json")
    store.upsert(
        _delegated_record(
            work_id="CH-359",
            owner_session_id="discord:correct-owner-thread",
            executor_session_id="proc-right",
            worktree_path=str(tmp_path / "real-worktree"),
        )
    )
    store.upsert(
        _delegated_record(
            work_id="CH-360",
            owner_session_id="discord:wrong-thread",
            executor_session_id="proc-right",
            worktree_path=str(tmp_path / "wrong-worktree"),
        )
    )

    verdict = store.resolve_native_owner_ingress_packet(
        {
            "work_id": "CH-359",
            "owner": "hermes",
            "executor": "omx",
            "event_type": "handoff_needed",
            "next_action": "wake exact owner",
            "proof": "clawhip:packet:1",
            # Conflicting lower-priority selector must not override work_id.
            "executor_session_id": "proc-right",
        }
    )

    assert verdict["status"] == "single_match"
    assert verdict["selected_by"] == "work_id"
    assert verdict["owner_wake"] == {
        "work_id": "CH-359",
        "owner_session_id": "discord:correct-owner-thread",
        "source": "work_record",
        "webhook_session_id": None,
    }
    assert verdict["no_broadcast"] is True
    assert len(verdict["matches"]) == 1


def test_zero_match_observes_only_and_does_not_broadcast(tmp_path):
    store = WorkStateStore(tmp_path / "work-state.json")
    store.upsert(_delegated_record(work_id="CH-359", executor_session_id="proc-known"))

    verdict = store.resolve_native_owner_ingress_packet(
        {
            "owner": "hermes",
            "executor": "omx",
            "executor_session_id": "proc-missing",
            "event_type": "blocked",
            "next_action": "observe only",
            "proof": "clawhip:packet:missing",
        }
    )

    assert verdict["status"] == "missing"
    assert verdict["selected_by"] == "executor_session"
    assert verdict["matches"] == []
    assert verdict["no_broadcast"] is True
    assert "owner_wake" not in verdict


def test_ambiguous_match_rejects_without_owner_wake(tmp_path):
    store = WorkStateStore(tmp_path / "work-state.json")
    shared_worktree = str(tmp_path / "shared-worktree")
    store.upsert(
        _delegated_record(
            work_id="CH-359-A",
            owner_session_id="discord:owner-a",
            executor_session_id=None,
            tmux_session=None,
            worktree_path=shared_worktree,
        )
    )
    store.upsert(
        _delegated_record(
            work_id="CH-359-B",
            owner_session_id="discord:owner-b",
            executor_session_id=None,
            tmux_session=None,
            worktree_path=shared_worktree,
        )
    )

    verdict = store.resolve_native_owner_ingress_packet(
        {
            "owner": "hermes",
            "executor": "omx",
            "worktree_path": shared_worktree,
            "event_type": "blocked",
            "next_action": "reject ambiguous",
            "proof": "clawhip:packet:ambiguous",
        }
    )

    assert verdict["status"] == "ambiguous"
    assert verdict["selected_by"] == "worktree_path"
    assert len(verdict["matches"]) == 2
    assert verdict["no_broadcast"] is True
    assert "owner_wake" not in verdict


def test_priority_falls_back_from_session_to_worktree_then_repo_path(tmp_path):
    store = WorkStateStore(tmp_path / "work-state.json")
    worktree = tmp_path / "repo/.worktrees/ch-359"
    repo = tmp_path / "repo"
    store.upsert(
        _delegated_record(
            work_id="CH-359",
            executor_session_id=None,
            tmux_session=None,
            repo_path=str(repo),
            worktree_path=str(worktree),
        )
    )

    by_worktree = store.resolve_native_owner_ingress_packet(
        {"owner": "hermes", "executor": "omx", "worktree_path": str(worktree)}
    )
    by_repo = store.resolve_native_owner_ingress_packet(
        {"owner": "hermes", "executor": "omx", "repo_path": str(repo)}
    )
    repo_name_only = store.resolve_native_owner_ingress_packet(
        {"owner": "hermes", "executor": "omx", "repo_name": "hermes-agent"}
    )

    assert by_worktree["status"] == "single_match"
    assert by_worktree["selected_by"] == "worktree_path"
    assert by_repo["status"] == "single_match"
    assert by_repo["selected_by"] == "repo_path"
    assert repo_name_only["status"] == "missing"
    assert repo_name_only["reason"] == "insufficient_native_owner_correlation"
    assert repo_name_only["no_broadcast"] is True
