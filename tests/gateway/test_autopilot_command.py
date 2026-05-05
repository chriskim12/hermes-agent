"""Focused tests for the CH-385 /autopilot controller entrypoint."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.autopilot import (
    ACTION_DISABLE,
    ACTION_DRY_RUN,
    ACTION_ENABLE,
    ACTION_ONE_SHOT,
    ACTION_STATUS,
    AutopilotParseError,
    AutopilotStateStore,
    classify_linear_target,
    handle_autopilot_command,
    parse_autopilot_args,
    run_autopilot_once,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


class _FakeLinearClient:
    def __init__(self, payloads, *, queue=None):
        self.payloads = payloads
        self.queue = queue
        self.calls = []
        self.queue_calls = []

    def fetch_issue(self, identifier: str):
        self.calls.append(identifier)
        return self.payloads[identifier]

    def list_execution_ready_issues(self, *, team_key="CH", state_name="Execution Ready", limit=100):
        self.queue_calls.append(
            {"team_key": team_key, "state_name": state_name, "limit": limit}
        )
        if isinstance(self.queue, dict):
            return self.queue
        return {"status": "ok", "issues": list(self.queue or [])}


class _FakeWorkStateStore:
    def __init__(self, records=None):
        self.records = records or []
        self.resolve_calls = []
        self.upsert_calls = []
        self.update_calls = []

    def list_records(self):
        return self.records

    def upsert(self, record):
        self.upsert_calls.append(record)
        self.records = [
            existing
            for existing in self.records
            if not (
                existing.work_id == record.work_id
                and existing.owner_session_id == record.owner_session_id
            )
        ]
        self.records.append(record)

    def update_record(self, work_id: str, owner_session_id: str, **updates):
        self.update_calls.append(
            {"work_id": work_id, "owner_session_id": owner_session_id, "updates": updates}
        )
        for record in self.records:
            if record.work_id == work_id and record.owner_session_id == owner_session_id:
                for key, value in updates.items():
                    setattr(record, key, value)
                return True
        return False

    def resolve_delegated_signal_candidate(self, *, work_id: str, live_only: bool):
        self.resolve_calls.append({"work_id": work_id, "live_only": live_only})
        return {"status": "no_match", "reason": "not_found", "matches": []}


class _BrokenWorkStateStore:
    def list_records(self):
        raise RuntimeError("corrupt work_state")

    def resolve_delegated_signal_candidate(self, *, work_id: str, live_only: bool):
        raise RuntimeError("corrupt work_state")


def _issue(
    identifier: str,
    *,
    state_name: str = "Execution Ready",
    state_type: str = "started",
    parent=None,
    children=None,
    description: str | None = None,
    priority: int = 3,
    project: str | None = "Autopilot",
):
    return {
        "status": "ok",
        "identifier": identifier,
        "title": f"{identifier} title",
        "description": description
        if description is not None
        else "## Done when\n- implementation complete\n\n## Verification\n- focused tests pass",
        "priority": priority,
        "url": f"https://linear.app/chriskim12/issue/{identifier.lower()}",
        "state": {"name": state_name, "type": state_type},
        "parent": parent,
        "children": {"nodes": children or []},
        "project": {"name": project} if project else None,
        "labels": {"nodes": []},
    }


def _state_store(tmp_path):
    return AutopilotStateStore(tmp_path / "autopilot-state.json")


@pytest.mark.parametrize(
    "raw,expected_action,expected_target",
    [
        ("", ACTION_STATUS, None),
        ("status", ACTION_STATUS, None),
        ("status ch-385", ACTION_STATUS, "CH-385"),
        ("dry-run", ACTION_DRY_RUN, None),
        ("dry-run ch-385", ACTION_DRY_RUN, "CH-385"),
        ("ON", ACTION_ENABLE, None),
        ("on", ACTION_ENABLE, None),
        ("OFF", ACTION_DISABLE, None),
        ("off", ACTION_DISABLE, None),
        ("ch-385", ACTION_ONE_SHOT, "CH-385"),
    ],
)
def test_parse_autopilot_accepts_only_contract_shapes(raw, expected_action, expected_target):
    command = parse_autopilot_args(raw)

    assert command.action == expected_action
    assert command.target_id == expected_target


@pytest.mark.parametrize(
    "raw",
    [
        "ON CH-385",
        "OFF CH-385",
        "status CH-385 extra",
        "dry-run CH-385 extra",
        "status not-a-card",
        "dry-run not-a-card",
        "CH-nope",
        "CH-385 CH-386",
        "start CH-385",
    ],
)
def test_parse_autopilot_rejects_invalid_shapes_fail_closed(raw):
    with pytest.raises(AutopilotParseError):
        parse_autopilot_args(raw)


def test_status_and_dry_run_are_read_only_no_state_write_no_executor(tmp_path):
    store = _state_store(tmp_path)
    work_state = _FakeWorkStateStore()
    spawner = MagicMock()

    status = handle_autopilot_command(
        "status",
        state_store=store,
        work_state_store=work_state,
        executor_spawner=spawner,
    )
    dry_run = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=work_state,
        executor_spawner=spawner,
    )

    assert status.decision["side_effects"] == {
        "state_written": False,
        "executor_spawned": False,
        "linear_done_mutated": False,
    }
    assert dry_run.decision["side_effects"] == {
        "state_written": False,
        "executor_spawned": False,
        "linear_done_mutated": False,
    }
    assert status.decision["admission"]["admission_bypassed"] is False
    assert dry_run.decision["admission"]["admission_bypassed"] is False
    assert store.path.exists() is False
    spawner.assert_not_called()


def test_targeted_status_and_dry_run_are_read_only_admission_classification_only(tmp_path):
    store = _state_store(tmp_path)
    work_state = _FakeWorkStateStore()
    spawner = MagicMock()
    linear = _FakeLinearClient({"CH-385": _issue("CH-385")})

    status = handle_autopilot_command(
        "status CH-385",
        state_store=store,
        work_state_store=work_state,
        linear_client=linear,
        executor_spawner=spawner,
    )
    dry_run = handle_autopilot_command(
        "dry-run CH-385",
        state_store=store,
        work_state_store=work_state,
        linear_client=linear,
        executor_spawner=spawner,
    )

    assert status.decision["side_effects"] == {
        "state_written": False,
        "executor_spawned": False,
        "linear_done_mutated": False,
    }
    assert dry_run.decision["side_effects"] == {
        "state_written": False,
        "executor_spawned": False,
        "linear_done_mutated": False,
    }
    assert status.decision["linear"]["shape"] == "standalone"
    assert dry_run.decision["linear"]["shape"] == "standalone"
    assert status.decision["admission"]["admission_bypassed"] is False
    assert dry_run.decision["admission"]["admission_bypassed"] is False
    assert linear.calls == ["CH-385", "CH-385"]
    assert work_state.resolve_calls == [
        {"work_id": "CH-385", "live_only": True},
        {"work_id": "CH-385", "live_only": True},
    ]
    assert store.path.exists() is False
    spawner.assert_not_called()


@pytest.mark.parametrize("enabled_value", ["false", "true", "0", "1", 0, 1, None])
def test_state_store_loads_malformed_enabled_values_disabled_fail_closed(tmp_path, enabled_value):
    store = _state_store(tmp_path)
    store.path.write_text(
        json.dumps({"schema_version": 1, "enabled": enabled_value}),
        encoding="utf-8",
    )

    state = store.status()

    assert state["enabled"] is False
    assert state["state_error"] == "invalid_enabled_type"


def test_state_store_loads_boolean_enabled_values_without_coercion(tmp_path):
    store = _state_store(tmp_path)
    store.path.write_text('{"schema_version": 1, "enabled": true}', encoding="utf-8")

    assert store.status()["enabled"] is True


def test_on_off_persist_controller_intent_only(tmp_path):
    store = _state_store(tmp_path)
    work_state = _FakeWorkStateStore()
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)

    enabled = handle_autopilot_command(
        "ON",
        actor="tester",
        state_store=store,
        work_state_store=work_state,
        now=now,
    )
    disabled = handle_autopilot_command(
        "OFF",
        actor="tester",
        state_store=store,
        work_state_store=work_state,
        now=now,
    )

    assert enabled.decision["side_effects"]["state_written"] is True
    assert enabled.decision["side_effects"]["executor_spawned"] is False
    assert enabled.decision["side_effects"]["linear_done_mutated"] is False
    assert enabled.decision["admission"] == {
        "status": "controller_intent_enabled",
        "reason": "future_automatic_starts_still_require_admission",
        "admission_bypassed": False,
    }
    assert disabled.decision["state"]["enabled"] is False
    assert disabled.decision["state"]["updated_by"] == "tester"
    assert disabled.decision["side_effects"] == {
        "state_written": True,
        "executor_spawned": False,
        "linear_done_mutated": False,
    }
    assert store.status()["enabled"] is False


@pytest.mark.parametrize(
    "payload,expected_shape",
    [
        (
            _issue(
                "CH-173",
                children=[{"identifier": "CH-385", "state": {"name": "Execution Ready"}}],
            ),
            "parent",
        ),
        (
            _issue(
                "CH-385",
                parent={"identifier": "CH-173", "state": {"name": "Backlog"}},
            ),
            "child",
        ),
        (_issue("CH-999"), "standalone"),
    ],
)
def test_classify_linear_target_shapes(payload, expected_shape):
    classified = classify_linear_target(payload, payload["identifier"])

    assert classified["shape"] == expected_shape
    assert classified["status"] == "ok"


def test_one_shot_classifies_target_without_executor_or_linear_mutation(tmp_path):
    store = _state_store(tmp_path)
    work_state = _FakeWorkStateStore()
    spawner = MagicMock()
    linear = _FakeLinearClient(
        {
            "CH-385": _issue(
                "CH-385",
                parent={"identifier": "CH-173", "state": {"name": "Backlog"}},
            )
        }
    )

    result = handle_autopilot_command(
        "ch-385",
        state_store=store,
        work_state_store=work_state,
        linear_client=linear,
        executor_spawner=spawner,
    )

    assert result.decision["linear"]["shape"] == "child"
    assert result.decision["linear"]["execution_ready"] is True
    assert result.decision["admission"] == {
        "status": "eligible_for_admission",
        "reason": "one_shot_target_requires_admission_before_executor_start",
        "admission_bypassed": False,
    }
    assert result.decision["side_effects"] == {
        "state_written": False,
        "executor_spawned": False,
        "linear_done_mutated": False,
    }
    assert linear.calls == ["CH-385"]
    assert work_state.resolve_calls == [{"work_id": "CH-385", "live_only": True}]
    assert store.path.exists() is False
    spawner.assert_not_called()


def test_parent_target_requires_child_selection_without_spawning(tmp_path):
    store = _state_store(tmp_path)
    spawner = MagicMock()
    linear = _FakeLinearClient(
        {
            "CH-173": _issue(
                "CH-173",
                children=[{"identifier": "CH-385", "state": {"name": "Execution Ready"}}],
            )
        }
    )

    result = handle_autopilot_command(
        "CH-173",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
        executor_spawner=spawner,
    )

    assert result.decision["linear"]["shape"] == "parent"
    assert result.decision["admission"]["status"] == "requires_child_selection"
    assert result.decision["admission"]["admission_bypassed"] is False
    assert result.decision["side_effects"]["executor_spawned"] is False
    spawner.assert_not_called()


def test_unavailable_linear_target_blocks_fail_closed(tmp_path):
    store = _state_store(tmp_path)
    spawner = MagicMock()
    linear = _FakeLinearClient(
        {
            "CH-404": {
                "status": "unavailable",
                "reason": "LINEAR_API_KEY_missing",
                "identifier": "CH-404",
            }
        }
    )

    result = handle_autopilot_command(
        "CH-404",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
        executor_spawner=spawner,
    )

    assert result.decision["linear"]["shape"] == "unknown"
    assert result.decision["admission"] == {
        "status": "blocked",
        "reason": "linear_target_unavailable_fail_closed",
        "admission_bypassed": False,
    }
    assert result.decision["side_effects"]["executor_spawned"] is False
    assert result.decision["side_effects"]["linear_done_mutated"] is False
    spawner.assert_not_called()


def test_dry_run_enabled_queue_selects_one_eligible_task_with_would_artifacts(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    spawner = MagicMock()
    issue = _issue(
        "CH-401",
        parent={"identifier": "CH-173", "title": "parent", "state": {"name": "Backlog", "type": "backlog"}},
    )
    linear = _FakeLinearClient({}, queue=[issue])

    result = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
        executor_spawner=spawner,
    )

    dry_run = result.decision["dry_run"]
    assert dry_run["status"] == "would_run"
    assert dry_run["selected_issue"]["identifier"] == "CH-401"
    assert dry_run["would_create_work_state"] == {
        "work_id": "CH-401",
        "owner": "hermes",
        "executor": "hermes",
        "mode": "autopilot",
        "state": "created",
        "parent_id": "CH-173",
    }
    assert dry_run["would_goal_contract"]["command"].startswith("/goal ")
    assert "CH-401" in dry_run["would_goal_contract"]["summary"]
    assert result.decision["admission"] == {
        "status": "would_run",
        "reason": "dry_run_selected_execution_ready_issue",
        "admission_bypassed": False,
    }
    assert result.decision["side_effects"] == {
        "state_written": False,
        "executor_spawned": False,
        "linear_done_mutated": False,
    }
    assert linear.queue_calls == [
        {"team_key": "CH", "state_name": "Execution Ready", "limit": 100}
    ]
    assert store.status()["enabled"] is True
    spawner.assert_not_called()


def test_dry_run_multiple_eligible_tasks_uses_deterministic_identifier_order(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    linear = _FakeLinearClient(
        {},
        queue=[
            _issue("CH-410", priority=3),
            _issue("CH-409", priority=3),
            _issue("CH-411", priority=3),
        ],
    )

    result = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
    )

    dry_run = result.decision["dry_run"]
    assert dry_run["status"] == "would_run"
    assert dry_run["selected_issue"]["identifier"] == "CH-409"
    assert [candidate["identifier"] for candidate in dry_run["candidates"]] == [
        "CH-409",
        "CH-410",
        "CH-411",
    ]


def test_dry_run_off_returns_noop_without_linear_scan_or_executor(tmp_path):
    store = _state_store(tmp_path)
    linear = _FakeLinearClient({}, queue=[_issue("CH-401")])
    spawner = MagicMock()

    result = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
        executor_spawner=spawner,
    )

    assert result.decision["dry_run"] == {
        "status": "paused",
        "reason": "controller_disabled_noop",
        "selected_issue": None,
        "would_create_work_state": None,
        "would_goal_contract": None,
        "candidates": [],
        "groups": {"parents": {}, "projects": {}},
    }
    assert result.decision["admission"] == {
        "status": "paused",
        "reason": "controller_disabled_noop",
        "admission_bypassed": False,
    }
    assert linear.queue_calls == []
    spawner.assert_not_called()


def test_dry_run_active_work_state_lock_blocks_candidate(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    locked = SimpleNamespace(
        work_id="CH-401",
        state="running",
        owner="hermes",
        executor="omx",
        mode="delegated",
    )
    linear = _FakeLinearClient({}, queue=[_issue("CH-401")])

    result = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=_FakeWorkStateStore([locked]),
        linear_client=linear,
    )

    dry_run = result.decision["dry_run"]
    assert dry_run["status"] == "blocked"
    assert dry_run["reason"] == "no_eligible_execution_ready_issue"
    assert dry_run["selected_issue"] is None
    assert dry_run["candidates"] == [
        {
            "identifier": "CH-401",
            "eligible": False,
            "reason": "active_work_state_lock",
            "shape": "standalone",
            "parent_id": None,
            "project": "Autopilot",
        }
    ]


def test_dry_run_unavailable_work_state_fails_closed_before_selection(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    linear = _FakeLinearClient({}, queue=[_issue("CH-401")])

    result = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=_BrokenWorkStateStore(),
        linear_client=linear,
    )

    dry_run = result.decision["dry_run"]
    assert dry_run["status"] == "blocked"
    assert dry_run["reason"] == "work_state_unavailable_fail_closed"
    assert dry_run["selected_issue"] is None
    assert dry_run["would_create_work_state"] is None
    assert dry_run["would_goal_contract"] is None
    assert dry_run["work_state_reason"] == "work_state_unavailable:RuntimeError"
    assert result.decision["admission"] == {
        "status": "blocked",
        "reason": "work_state_unavailable_fail_closed",
        "admission_bypassed": False,
    }
    assert linear.queue_calls == []


def test_dry_run_real_corrupt_work_state_file_fails_closed_before_selection(tmp_path):
    from gateway.work_state import WorkStateStore

    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    work_state_path = tmp_path / "gateway_work_state.json"
    work_state_path.write_text("{ not json }", encoding="utf-8")
    linear = _FakeLinearClient({}, queue=[_issue("CH-401")])

    result = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=WorkStateStore(work_state_path),
        linear_client=linear,
    )

    dry_run = result.decision["dry_run"]
    assert dry_run["status"] == "blocked"
    assert dry_run["reason"] == "work_state_unavailable_fail_closed"
    assert dry_run["selected_issue"] is None
    assert dry_run["would_create_work_state"] is None
    assert dry_run["would_goal_contract"] is None
    assert dry_run["work_state_reason"] == "work_state_unavailable:JSONDecodeError"
    assert linear.queue_calls == []


def test_dry_run_malformed_work_state_payload_fails_closed_before_selection(tmp_path):
    from gateway.work_state import WorkStateStore

    cases = [
        ({"sessions": [{"work_id": "CH-999"}]}, "work_state_unavailable:invalid_payload_schema"),
        (["bad"], "work_state_unavailable:invalid_record_item_type"),
        ({"records": ["bad"]}, "work_state_unavailable:invalid_record_item_type"),
        ({"records": "bad"}, "work_state_unavailable:invalid_records_type"),
    ]
    for index, (payload, expected_reason) in enumerate(cases):
        store = _state_store(tmp_path / f"state-{index}")
        store.set_enabled(True, actor="tester")
        work_state_path = tmp_path / f"gateway_work_state_{index}.json"
        work_state_path.write_text(json.dumps(payload), encoding="utf-8")
        linear = _FakeLinearClient({}, queue=[_issue("CH-401")])

        result = handle_autopilot_command(
            "dry-run",
            state_store=store,
            work_state_store=WorkStateStore(work_state_path),
            linear_client=linear,
        )

        dry_run = result.decision["dry_run"]
        assert dry_run["status"] == "blocked"
        assert dry_run["reason"] == "work_state_unavailable_fail_closed"
        assert dry_run["selected_issue"] is None
        assert dry_run["work_state_reason"] == expected_reason
        assert linear.queue_calls == []


def test_dry_run_missing_linear_access_fails_closed(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    linear = _FakeLinearClient(
        {},
        queue={"status": "unavailable", "reason": "LINEAR_API_KEY_missing"},
    )

    result = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
    )

    assert result.decision["dry_run"] == {
        "status": "blocked",
        "reason": "linear_queue_unavailable_fail_closed",
        "selected_issue": None,
        "would_create_work_state": None,
        "would_goal_contract": None,
        "candidates": [],
        "groups": {"parents": {}, "projects": {}},
        "linear_reason": "LINEAR_API_KEY_missing",
    }
    assert result.decision["admission"] == {
        "status": "blocked",
        "reason": "linear_queue_unavailable_fail_closed",
        "admission_bypassed": False,
    }


def test_dry_run_missing_verification_blocks_candidate(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    linear = _FakeLinearClient(
        {},
        queue=[_issue("CH-401", description="## Done when\n- implementation complete")],
    )

    result = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
    )

    dry_run = result.decision["dry_run"]
    assert dry_run["status"] == "blocked"
    assert dry_run["reason"] == "no_eligible_execution_ready_issue"
    assert dry_run["candidates"][0]["reason"] == "missing_verification"
    assert dry_run["selected_issue"] is None


def test_dry_run_parent_terminal_refuses_targeted_parent(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    linear = _FakeLinearClient(
        {
            "CH-173": _issue(
                "CH-173",
                state_name="Done",
                state_type="completed",
                children=[_issue("CH-401")],
            )
        }
    )

    result = handle_autopilot_command(
        "dry-run CH-173",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
    )

    dry_run = result.decision["dry_run"]
    assert dry_run["status"] == "blocked"
    assert dry_run["reason"] == "parent_terminal"
    assert dry_run["selected_issue"] is None
    assert result.decision["admission"]["reason"] == "parent_terminal"


def test_targeted_parent_dry_run_selects_at_most_one_eligible_child(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    linear = _FakeLinearClient(
        {
            "CH-173": _issue(
                "CH-173",
                state_name="Backlog",
                state_type="backlog",
                children=[_issue("CH-402"), _issue("CH-401")],
            )
        }
    )

    result = handle_autopilot_command(
        "dry-run CH-173",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
    )

    dry_run = result.decision["dry_run"]
    assert dry_run["status"] == "would_run"
    assert dry_run["selected_issue"]["identifier"] == "CH-401"
    assert [candidate["identifier"] for candidate in dry_run["candidates"]] == ["CH-401", "CH-402"]
    assert dry_run["would_create_work_state"]["parent_id"] == "CH-173"


def test_dry_run_forbidden_side_effect_boundary_is_explicit(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    spawner = MagicMock()
    linear = _FakeLinearClient({}, queue=[_issue("CH-401")])

    result = handle_autopilot_command(
        "dry-run",
        state_store=store,
        work_state_store=_FakeWorkStateStore(),
        linear_client=linear,
        executor_spawner=spawner,
    )

    assert result.decision["side_effects"] == {
        "state_written": False,
        "executor_spawned": False,
        "linear_done_mutated": False,
    }
    assert "would_create_work_state" in result.decision["dry_run"]
    assert result.decision["dry_run"]["would_create_work_state"]["work_id"] == "CH-401"
    assert store.status()["enabled"] is True
    spawner.assert_not_called()


def test_run_autopilot_once_materializes_one_task_lock_contract_and_executor(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    work_state = _FakeWorkStateStore()
    linear = _FakeLinearClient({}, queue=[_issue("CH-401")])
    spawner = MagicMock(return_value={"session_id": "exec-1", "proof": "spawned"})
    now = datetime(2026, 5, 5, 5, 0, tzinfo=timezone.utc)

    result = run_autopilot_once(
        state_store=store,
        work_state_store=work_state,
        linear_client=linear,
        executor_spawner=spawner,
        actor="tester",
        owner_session_id="owner-1",
        now=now,
    )

    assert result["status"] == "started"
    assert result["selected_issue"]["identifier"] == "CH-401"
    assert result["work_state_written"] is True
    assert result["executor_spawned"] is True
    assert result["linear_done_mutated"] is False
    assert result["work_id"] == "CH-401"
    assert result["owner_session_id"] == "owner-1"
    assert len(work_state.upsert_calls) == 1
    record = work_state.records[0]
    assert record.work_id == "CH-401"
    assert record.owner == "hermes"
    assert record.executor == "hermes"
    assert record.mode == "autopilot"
    assert record.state == "running"
    assert record.executor_session_id == "exec-1"
    assert record.proof == "spawned"
    assert "Execute Linear CH-401" in record.objective
    spawner.assert_called_once()
    spawn_kwargs = spawner.call_args.kwargs
    assert spawn_kwargs["work_id"] == "CH-401"
    assert spawn_kwargs["owner_session_id"] == "owner-1"
    assert spawn_kwargs["goal_contract"].startswith("/goal")


def test_run_autopilot_once_refuses_when_controller_off_without_queue_scan(tmp_path):
    store = _state_store(tmp_path)
    work_state = _FakeWorkStateStore()
    linear = _FakeLinearClient({}, queue=[_issue("CH-401")])
    spawner = MagicMock()

    result = run_autopilot_once(
        state_store=store,
        work_state_store=work_state,
        linear_client=linear,
        executor_spawner=spawner,
        actor="tester",
        owner_session_id="owner-1",
    )

    assert result["status"] == "paused"
    assert result["reason"] == "controller_disabled_noop"
    assert result["work_state_written"] is False
    assert result["executor_spawned"] is False
    assert result["linear_done_mutated"] is False
    assert linear.queue_calls == []
    spawner.assert_not_called()


def test_run_autopilot_once_rolls_back_lock_when_executor_spawn_fails(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    work_state = _FakeWorkStateStore()
    linear = _FakeLinearClient({}, queue=[_issue("CH-401")])
    spawner = MagicMock(side_effect=RuntimeError("executor unavailable"))

    result = run_autopilot_once(
        state_store=store,
        work_state_store=work_state,
        linear_client=linear,
        executor_spawner=spawner,
        actor="tester",
        owner_session_id="owner-1",
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "executor_spawn_failed"
    assert result["work_state_written"] is True
    assert result["executor_spawned"] is False
    assert result["linear_done_mutated"] is False
    record = work_state.records[0]
    assert record.state == "blocked"
    assert record.close_disposition == "close"
    assert record.usable_outcome == "blocked"
    assert "executor_spawn_failed" in record.blocked_reason


def test_run_autopilot_once_off_after_run_blocks_next_start(tmp_path):
    store = _state_store(tmp_path)
    store.set_enabled(True, actor="tester")
    work_state = _FakeWorkStateStore()
    linear = _FakeLinearClient({}, queue=[_issue("CH-401"), _issue("CH-402")])
    spawner = MagicMock(return_value={"session_id": "exec-1", "proof": "spawned"})

    first = run_autopilot_once(
        state_store=store,
        work_state_store=work_state,
        linear_client=linear,
        executor_spawner=spawner,
        actor="tester",
        owner_session_id="owner-1",
    )
    handle_autopilot_command("OFF", state_store=store, work_state_store=work_state, actor="tester")
    second = run_autopilot_once(
        state_store=store,
        work_state_store=work_state,
        linear_client=linear,
        executor_spawner=spawner,
        actor="tester",
        owner_session_id="owner-2",
    )

    assert first["status"] == "started"
    assert second["status"] == "paused"
    assert second["work_state_written"] is False
    assert spawner.call_count == 1


def test_command_registry_exposes_autopilot_to_gateway_surfaces():
    from hermes_cli.commands import (
        is_gateway_known_command,
        resolve_command,
        should_bypass_active_session,
    )

    command = resolve_command("autopilot")

    assert command is not None
    assert command.name == "autopilot"
    assert command.args_hint == "[status|dry-run|ON|OFF|CH-123]"
    assert is_gateway_known_command("autopilot") is True
    assert should_bypass_active_session("autopilot") is True


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_runner():
    from gateway.run import GatewayRunner

    source = _make_source()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {Platform.TELEGRAM: MagicMock(send=AsyncMock())}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._session_db.get_session.return_value = None
    runner._voice_mode = {}
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
async def test_gateway_autopilot_bypasses_running_agent_without_interrupt(monkeypatch):
    calls = []

    def _fake_handle(raw_args, *, actor=None):
        calls.append({"raw_args": raw_args, "actor": actor})
        return SimpleNamespace(message="autopilot handled")

    monkeypatch.setattr("gateway.autopilot.handle_autopilot_command", _fake_handle)
    runner = _make_runner()
    session_key = build_session_key(_make_source())
    running_agent = SimpleNamespace(interrupt=MagicMock())
    runner._running_agents[session_key] = running_agent
    runner._running_agents_ts[session_key] = 0

    result = await runner._handle_message(_make_event("/autopilot status CH-385"))

    assert result == "autopilot handled"
    assert calls == [{"raw_args": "status CH-385", "actor": "tester"}]
    running_agent.interrupt.assert_not_called()
    assert runner._pending_messages == {}


def test_cli_autopilot_dispatch_uses_same_controller(monkeypatch):
    import cli as cli_mod
    from cli import HermesCLI

    captured = []
    calls = []

    def _fake_handle(raw_args, *, actor=None):
        calls.append({"raw_args": raw_args, "actor": actor})
        return SimpleNamespace(message="cli autopilot handled")

    monkeypatch.setattr("gateway.autopilot.handle_autopilot_command", _fake_handle)
    monkeypatch.setattr(cli_mod, "_cprint", lambda message, *a, **k: captured.append(str(message)))

    cli = object.__new__(HermesCLI)

    assert cli.process_command("/autopilot dry-run CH-385") is True
    assert calls == [{"raw_args": "dry-run CH-385", "actor": "local-cli"}]
    assert captured == ["cli autopilot handled"]
