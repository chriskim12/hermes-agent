from datetime import datetime, timezone

from gateway.work_state import (
    SUPERVISOR_ACTION_CONTINUE,
    SUPERVISOR_ACTION_ESCALATE,
    SUPERVISOR_ACTION_TERMINAL,
    SUPERVISOR_ACTION_WAIT,
    WorkRecord,
    classify_delegated_omx_supervisor_action,
)


def _record(**overrides):
    now = datetime.now(timezone.utc)
    values = {
        "work_id": "wk-supervisor-1",
        "title": "delegated work",
        "objective": "classify delegated OMX work",
        "owner": "hermes",
        "executor": "omx",
        "mode": "delegated",
        "owner_session_id": "agent:telegram:chat-1:user-1",
        "state": "stale",
        "started_at": now,
        "last_progress_at": now,
        "next_action": "Continue delegated OMX work",
        "executor_session_id": "proc-omx-1",
        "tmux_session": "omx-supervisor",
        "repo_path": "/repo",
        "worktree_path": "/repo/.worktrees/ch-235",
        "proof": "test",
    }
    values.update(overrides)
    return WorkRecord(**values)


def test_supervisor_missing_work_id_escalates_without_wake_or_deliver():
    decision = classify_delegated_omx_supervisor_action(
        _record(work_id=""),
        {"trusted_auto_watch": True, "deliver_attempts": 0},
    )

    assert decision.action == SUPERVISOR_ACTION_ESCALATE
    assert decision.reason == "missing_work_id"
    assert decision.work_id is None
    assert decision.deliver_request is None
    assert decision.no_broadcast is True


def test_supervisor_bounded_deliver_attempt_allows_once_by_default_then_escalates():
    first = classify_delegated_omx_supervisor_action(
        _record(),
        {"trusted_auto_watch": True, "deliver_attempts": 0, "provider_session_id": "sess-1"},
    )
    repeated = classify_delegated_omx_supervisor_action(
        _record(),
        {"trusted_auto_watch": True, "deliver_attempts": 1, "provider_session_id": "sess-1"},
    )

    assert first.action == SUPERVISOR_ACTION_CONTINUE
    assert first.deliver_request["attempt"] == 1
    assert first.deliver_request["attempts_limit"] == 1
    assert repeated.action == SUPERVISOR_ACTION_ESCALATE
    assert repeated.reason == "deliver_attempt_limit_reached"
    assert repeated.deliver_request is None


def test_supervisor_bounded_deliver_attempt_honors_configured_limit():
    decision = classify_delegated_omx_supervisor_action(
        _record(),
        {"trusted_auto_watch": True, "deliver_attempts": 1, "provider_session_id": "sess-1"},
        max_deliver_attempts=2,
    )

    assert decision.action == SUPERVISOR_ACTION_CONTINUE
    assert decision.attempts_used == 1
    assert decision.deliver_request["attempt"] == 2
    assert decision.deliver_request["attempts_limit"] == 2


def test_supervisor_terminal_record_and_terminal_session_do_not_deliver():
    closed_record = classify_delegated_omx_supervisor_action(
        _record(state="closed"),
        {"trusted_auto_watch": True, "deliver_attempts": 0},
    )
    closed_session = classify_delegated_omx_supervisor_action(
        _record(state="stale"),
        {"trusted_auto_watch": True, "deliver_attempts": 0, "lifecycle_state": "resolved"},
    )

    assert closed_record.action == SUPERVISOR_ACTION_TERMINAL
    assert closed_record.deliver_request is None
    assert closed_session.action == SUPERVISOR_ACTION_TERMINAL
    assert closed_session.reason == "terminal_work_session"
    assert closed_session.deliver_request is None


def test_supervisor_stale_or_blocked_with_valid_tmux_session_can_request_deliver():
    for state in ("stale", "blocked"):
        decision = classify_delegated_omx_supervisor_action(
            _record(state=state),
            {
                "provider": "codex",
                "provider_session_id": "sess-1",
                "trusted_auto_watch": True,
                "deliver_attempts": 0,
                "tmux_session": "omx-supervisor",
                "tmux_pane": "%7",
            },
        )

        assert decision.action == SUPERVISOR_ACTION_CONTINUE
        assert decision.reason == "actionable_delegated_omx_work"
        assert decision.deliver_request["work_id"] == "wk-supervisor-1"
        assert decision.deliver_request["tmux_session"] == "omx-supervisor"
        assert decision.deliver_request["tmux_pane"] == "%7"


def test_supervisor_running_record_waits_without_deliver():
    decision = classify_delegated_omx_supervisor_action(
        _record(state="running"),
        {"trusted_auto_watch": True, "deliver_attempts": 0},
    )

    assert decision.action == SUPERVISOR_ACTION_WAIT
    assert decision.reason == "work_not_actionable"
    assert decision.deliver_request is None


def test_supervisor_missing_ambiguous_or_unsafe_metadata_escalates_instead_of_looping():
    missing_tmux = classify_delegated_omx_supervisor_action(_record(tmux_session=None), {})
    ambiguous = classify_delegated_omx_supervisor_action(
        _record(),
        {"ambiguous": True, "trusted_auto_watch": True, "deliver_attempts": 0},
    )
    mismatched = classify_delegated_omx_supervisor_action(
        _record(tmux_session="trusted-tmux"),
        {"trusted_auto_watch": True, "deliver_attempts": 0, "tmux_session": "other-tmux"},
    )
    untrusted = classify_delegated_omx_supervisor_action(
        _record(),
        {"trusted_auto_watch": False, "deliver_attempts": 0},
    )

    assert missing_tmux.action == SUPERVISOR_ACTION_ESCALATE
    assert missing_tmux.reason == "missing_tmux_session"
    assert ambiguous.action == SUPERVISOR_ACTION_ESCALATE
    assert ambiguous.reason == "ambiguous_work_session"
    assert mismatched.action == SUPERVISOR_ACTION_ESCALATE
    assert mismatched.reason == "tmux_session_mismatch"
    assert untrusted.action == SUPERVISOR_ACTION_ESCALATE
    assert untrusted.reason == "untrusted_work_session"
    assert all(
        decision.deliver_request is None
        for decision in (missing_tmux, ambiguous, mismatched, untrusted)
    )
