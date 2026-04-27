from datetime import datetime, timezone

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.work_state import (
    WorkRecord,
    WorkSessionRegistry,
    WorkStateStore,
    _default_clawhip_deliver_executor,
    classify_work_session_action_required,
)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


class _RegistryRunner:
    def __init__(self, path):
        self.work_session_registry = WorkSessionRegistry(path)


class _AlertingRegistryRunner:
    def __init__(self, tmp_path):
        self.work_state_store = _seed_verified_delegated_work_record(
            tmp_path,
            tmux_session="ch242-webhook",
            repo_path="/repo",
            worktree_path="/repo/.worktrees/ch-242",
        )
        self.work_session_registry = WorkSessionRegistry(
            tmp_path / "work_sessions.json",
            work_state_store=self.work_state_store,
        )
        self.delegated_packets = []

    async def handle_delegated_ingress_packet(
        self,
        payload,
        *,
        route_name="",
        delivery_id=None,
    ):
        self.delegated_packets.append(
            {
                "payload": dict(payload),
                "route_name": route_name,
                "delivery_id": delivery_id,
            }
        )
        return {"status": "accepted", "resolution": "single_match"}


class _FakeTmuxWatchRegistrar:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, watch_record):
        self.calls.append(dict(watch_record))
        return {"ok": self.ok}


class _FakeTmuxWatchCleanup:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, cleanup_record):
        self.calls.append(dict(cleanup_record))
        return {"ok": self.ok}


def _seed_verified_delegated_work_record(
    tmp_path,
    *,
    work_id="wk-delegated-ch-239",
    tmux_session="ch239-smoke",
    repo_path="/home/ubuntu/repos/dailychingu",
    worktree_path="/home/ubuntu/repos/dailychingu/.worktrees/ch-239",
):
    store = WorkStateStore(tmp_path / "work_state.json")
    now = datetime.now(timezone.utc)
    store.upsert(
        WorkRecord(
            work_id=work_id,
            title="delegated clawhip watch",
            objective="verify Hermes-owned delegated work before auto-watch",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id="agent:telegram:chat-1:user-1",
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Wait for delegated OMX result",
            executor_session_id="proc-omx-1",
            tmux_session=tmux_session,
            repo_path=repo_path,
            worktree_path=worktree_path,
            proof="terminal_background:omx_exec",
        )
    )
    return store


@pytest.mark.parametrize(
    ("payload", "expected_state", "should_alert"),
    [
        (
            {
                "event": "ActionRequired",
                "pane_snapshot": {"visible_text": "No output for 12 minutes"},
                "idle_seconds": 720,
                "stale_minutes": 10,
            },
            "stale",
            True,
        ),
        (
            {
                "event": "ActionRequired",
                "pane_snapshot": {"visible_text": "Waiting for user input: should I proceed?"},
            },
            "blocked_on_user",
            True,
        ),
        (
            {
                "event": "ActionRequired",
                "pane_snapshot": {"visible_text": "Approval required. Reply /approve or /deny."},
            },
            "permission_prompt",
            True,
        ),
        (
            {
                "event": "ActionRequired",
                "pane_snapshot": {"visible_text": "Running tests with pytest -q ..."},
                "command_running": True,
            },
            "tool_running",
            False,
        ),
        (
            {
                "event": "ActionRequired",
                "pane_snapshot": {"visible_text": "All tests passed. Process exited with code 0."},
                "completed": True,
            },
            "completed_idle",
            False,
        ),
        (
            {
                "event": "ActionRequired",
                "pane_snapshot": {"visible_text": "can't find pane %42"},
                "pane_alive": False,
            },
            "orphaned",
            False,
        ),
        (
            {
                "event": "ActionRequired",
                "pane_snapshot": {"visible_text": "Hermes is thinking quietly."},
            },
            "unknown",
            False,
        ),
    ],
)
def test_clawhip_action_required_classifier_bounds_fixture_states(
    payload,
    expected_state,
    should_alert,
):
    classification = classify_work_session_action_required(payload)

    assert classification.state == expected_state
    assert classification.reason
    assert classification.required_owner_action
    assert classification.should_alert is should_alert


def test_clawhip_session_start_requires_explicit_card_and_lane(tmp_path):
    registrar = _FakeTmuxWatchRegistrar()
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
    )

    missing_card = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-missing-card",
            "repo_path": "/home/ubuntu/repos/dailychingu",
            "worktree_path": "/home/ubuntu/repos/dailychingu/.worktrees/ch-239",
        }
    )
    assert missing_card["status"] == "rejected"
    assert missing_card["reason"] == "missing_card_or_lane_linkage"

    branch_only = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-branch-only",
            "repo_path": "/home/ubuntu/repos/dailychingu",
            "worktree_path": "/home/ubuntu/repos/dailychingu/.worktrees/ch-239",
            "linear_card_id": "CH-239",
            "branch": "yuuka/ch-239-clawhip-work-session-registry",
        }
    )
    assert branch_only["status"] == "rejected"
    assert branch_only["reason"] == "missing_card_or_lane_linkage"
    assert registry.list_sessions() == []
    assert registrar.calls == []


def test_clawhip_session_start_does_not_auto_watch_without_trusted_tmux_metadata(tmp_path):
    registrar = _FakeTmuxWatchRegistrar()
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
    )

    no_tmux = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-no-tmux",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-241",
            "linear_card_id": "CH-241",
            "lane_id": "lane-241",
        }
    )
    assert no_tmux["status"] == "accepted"

    no_pane = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-no-pane",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-241",
            "linear_card_id": "CH-241",
            "lane_id": "lane-241",
            "tmux_session": "ch241",
            "watch_status": "active",
        }
    )
    assert no_pane["status"] == "accepted"

    assert registrar.calls == []
    assert registry.get_session("codex", "sess-no-tmux").watch_status == "unwatched"
    assert registry.get_session("codex", "sess-no-pane").watch_status == "unwatched"


def test_clawhip_session_start_does_not_auto_watch_without_owner_and_trusted_marker(tmp_path):
    registrar = _FakeTmuxWatchRegistrar()
    work_state_store = _seed_verified_delegated_work_record(
        tmp_path,
        tmux_session="ch241",
        repo_path="/repo",
        worktree_path="/repo/.worktrees/ch-241",
    )
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
        work_state_store=work_state_store,
    )

    missing_owner = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-missing-owner",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-241",
            "linear_card_id": "CH-241",
            "lane_id": "lane-241",
            "tmux_session": "ch241",
            "tmux_pane": "%42",
            "trusted_auto_watch": True,
        }
    )
    assert missing_owner["status"] == "accepted"

    missing_marker = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-missing-marker",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-241",
            "linear_card_id": "CH-241",
            "lane_id": "lane-241",
            "tmux_session": "ch241",
            "tmux_pane": "%42",
            "owner": "hermes",
        }
    )
    assert missing_marker["status"] == "accepted"

    assert registrar.calls == []
    assert registry.get_session("codex", "sess-missing-owner").watch_status == "unwatched"
    assert registry.get_session("codex", "sess-missing-marker").watch_status == "unwatched"


def test_clawhip_spoofed_disposable_event_does_not_auto_watch_without_verified_work_record(tmp_path):
    registrar = _FakeTmuxWatchRegistrar()
    work_state_store = WorkStateStore(tmp_path / "work_state.json")
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
        work_state_store=work_state_store,
    )

    spoofed = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "spoof-disposable",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-241",
            "linear_card_id": "CH-241",
            "lane_id": "lane-241",
            "tmux_session": "disposable-tmux",
            "tmux_pane": "%99",
            "owner": "hermes",
            "trusted_auto_watch": True,
        }
    )

    assert spoofed["status"] == "accepted"
    assert registrar.calls == []
    assert registry.get_session("codex", "spoof-disposable").watch_status == "unwatched"


def test_clawhip_native_events_update_registry_without_overwriting_upstream_keys(tmp_path):
    registrar = _FakeTmuxWatchRegistrar()
    work_state_store = _seed_verified_delegated_work_record(tmp_path)
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
        work_state_store=work_state_store,
    )
    started = "2026-04-27T20:00:00+00:00"
    prompted = "2026-04-27T20:03:00+00:00"

    start = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-239",
            "directory": "/home/ubuntu/repos/dailychingu",
            "repo_path": "/home/ubuntu/repos/dailychingu",
            "worktree_path": "/home/ubuntu/repos/dailychingu/.worktrees/ch-239",
            "repo_name": "dailychingu",
            "project": "Brain OS",
            "branch": "yuuka/ch-239-clawhip-work-session-registry",
            "linear_card_id": "CH-239",
            "lane_id": "yuuka/ch-239-clawhip-work-session-registry",
            "tmux_session": "ch239-smoke",
            "tmux_pane": "%42",
            "owner": "hermes",
            "trusted_auto_watch": True,
            "work_id": "wk-delegated-ch-239",
            "timestamp": started,
        }
    )

    assert start["status"] == "accepted"
    assert start["record"].provider_session_id == "sess-239"
    assert start["record"].lifecycle_state == "active"
    assert start["record"].watch_status == "active"
    assert len(registrar.calls) == 1
    assert registrar.calls[0]["session"] == "ch239-smoke"
    assert registrar.calls[0]["keywords"] == ["CH-239", "yuuka/ch-239-clawhip-work-session-registry"]
    assert registrar.calls[0]["stale_minutes"] == 10
    assert registrar.calls[0]["source"] == "hermes-work-session-registry"
    assert registrar.calls[0]["owner"] == "hermes"
    assert registrar.calls[0]["registration_source"] == "hermes-work-session-registry"
    assert registrar.calls[0]["active_wrapper_monitor"] is False
    assert registrar.calls[0]["routing"]["source"] == "hermes-work-session-registry"
    assert registrar.calls[0]["routing"]["owner"] == "hermes"
    assert registrar.calls[0]["routing"]["repo_path"] == "/home/ubuntu/repos/dailychingu"
    assert registrar.calls[0]["routing"]["worktree_path"] == "/home/ubuntu/repos/dailychingu/.worktrees/ch-239"
    assert registrar.calls[0]["routing"]["linear_card_id"] == "CH-239"
    assert registrar.calls[0]["routing"]["lane_id"] == "yuuka/ch-239-clawhip-work-session-registry"
    assert registrar.calls[0]["routing"]["cleanup_condition"] == "stop_or_resolved_or_owner_close"
    assert registrar.calls[0]["routing"]["tmux_pane"] == "%42"
    assert registrar.calls[0]["metadata"] == registrar.calls[0]["routing"]

    prompt = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "UserPromptSubmit",
            "session_id": "sess-239",
            "repo_path": "/tmp/wrong-repo-should-not-overwrite",
            "worktree_path": "/tmp/wrong-worktree-should-not-overwrite",
            "linear_card_id": "CH-999",
            "lane_id": "wrong-lane",
            "timestamp": prompted,
        }
    )

    assert prompt["status"] == "accepted"
    record = registry.get_session("codex", "sess-239")
    assert record is not None
    assert record.lifecycle_state == "waiting_user"
    assert record.linear_card_id == "CH-239"
    assert record.lane_id == "yuuka/ch-239-clawhip-work-session-registry"
    assert record.repo_path == "/home/ubuntu/repos/dailychingu"
    assert record.worktree_path == "/home/ubuntu/repos/dailychingu/.worktrees/ch-239"
    assert record.last_event == "UserPromptSubmit"
    assert record.started_at == datetime.fromisoformat(started)
    assert record.last_event_at == datetime.fromisoformat(prompted)
    assert record.native_event_metadata["event"] == "UserPromptSubmit"
    assert record.native_event_metadata["repo_path"] == "/tmp/wrong-repo-should-not-overwrite"
    assert record.native_event_metadata["linear_card_id"] == "CH-999"
    assert record.watch_registration_source == "hermes-work-session-registry"
    assert record.watch_owner == "hermes"
    assert record.watch_repo_path == "/home/ubuntu/repos/dailychingu"
    assert record.watch_worktree_path == "/home/ubuntu/repos/dailychingu/.worktrees/ch-239"
    assert record.watch_linear_card_id == "CH-239"
    assert record.watch_stale_minutes == 10
    assert record.watch_cleanup_condition == "stop_or_resolved_or_owner_close"


def test_clawhip_action_required_alert_requires_registry_match_and_classifier_reason(tmp_path):
    registrar = _FakeTmuxWatchRegistrar()
    work_state_store = _seed_verified_delegated_work_record(tmp_path)
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
        work_state_store=work_state_store,
    )
    registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-semantic-alert",
            "repo_path": "/home/ubuntu/repos/dailychingu",
            "worktree_path": "/home/ubuntu/repos/dailychingu/.worktrees/ch-239",
            "linear_card_id": "CH-239",
            "lane_id": "yuuka/ch-239-clawhip-work-session-registry",
            "tmux_session": "ch239-smoke",
            "tmux_pane": "%42",
            "owner": "hermes",
            "trusted_auto_watch": True,
            "work_id": "wk-delegated-ch-239",
        }
    )

    action_required = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "ActionRequired",
            "session_id": "sess-semantic-alert",
            "repo_path": "/home/ubuntu/repos/dailychingu",
            "worktree_path": "/home/ubuntu/repos/dailychingu/.worktrees/ch-239",
            "tmux_session": "ch239-smoke",
            "tmux_pane": "%42",
            "pane_snapshot": {
                "visible_text": "Waiting for user input: should I proceed with the migration?"
            },
            "work_id": "wk-delegated-ch-239",
            "timestamp": "2026-04-27T20:05:00+00:00",
        }
    )

    assert action_required["status"] == "accepted"
    assert action_required["classification"]["state"] == "blocked_on_user"
    assert action_required["classification"]["reason"]
    alert = action_required["semantic_alert"]
    assert alert["work_id"] == "wk-delegated-ch-239"
    assert alert["state"] == "blocked"
    assert alert["usable_outcome"] == "blocked"
    assert alert["classifier_state"] == "blocked_on_user"
    assert alert["classifier_reason"]
    assert alert["required_owner_action"]
    assert "Classifier blocked_on_user" in alert["next_action"]
    assert alert["proof"].startswith("clawhip:blocked_on_user:")


def test_clawhip_action_required_without_verified_work_record_does_not_alert(tmp_path):
    registry = WorkSessionRegistry(tmp_path / "work_sessions.json")
    registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-no-work-record",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-242",
            "linear_card_id": "CH-242",
            "lane_id": "yuuka/ch-242-semantic-classifier",
            "tmux_session": "ch242",
            "tmux_pane": "%42",
        }
    )

    action_required = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "ActionRequired",
            "session_id": "sess-no-work-record",
            "pane_snapshot": {"visible_text": "Approval required. Reply /approve or /deny."},
        }
    )

    assert action_required["status"] == "accepted"
    assert action_required["classification"]["state"] == "permission_prompt"
    assert "semantic_alert" not in action_required


@pytest.mark.parametrize(
    ("payload", "expected_lifecycle"),
    [
        (
            {
                "pane_snapshot": {
                    "visible_text": "All tests passed. Process exited with code 0."
                },
                "completed": True,
            },
            "resolved",
        ),
        (
            {
                "pane_snapshot": {"visible_text": "can't find pane %42"},
                "pane_alive": False,
            },
            "orphaned",
        ),
    ],
)
def test_clawhip_completed_idle_and_orphaned_panes_suppress_repeated_wakes(
    tmp_path,
    payload,
    expected_lifecycle,
):
    registrar = _FakeTmuxWatchRegistrar()
    cleanup = _FakeTmuxWatchCleanup()
    work_state_store = _seed_verified_delegated_work_record(
        tmp_path,
        tmux_session="ch242",
        repo_path="/repo",
        worktree_path="/repo/.worktrees/ch-242",
    )
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
        tmux_watch_cleanup=cleanup,
        work_state_store=work_state_store,
    )
    registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-terminal-semantic",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-242",
            "linear_card_id": "CH-242",
            "lane_id": "yuuka/ch-242-semantic-classifier",
            "tmux_session": "ch242",
            "tmux_pane": "%42",
            "owner": "hermes",
            "trusted_auto_watch": True,
            "timestamp": "2026-04-27T20:00:00+00:00",
        }
    )
    assert len(registrar.calls) == 1

    terminal = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "ActionRequired",
            "session_id": "sess-terminal-semantic",
            "timestamp": "2026-04-27T20:04:00+00:00",
            **payload,
        }
    )
    repeated = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "ActionRequired",
            "session_id": "sess-terminal-semantic",
            "pane_snapshot": {"visible_text": "No output for 12 minutes"},
            "idle_seconds": 720,
            "stale_minutes": 10,
            "timestamp": "2026-04-27T20:06:00+00:00",
        }
    )

    assert terminal["status"] == "accepted"
    assert "semantic_alert" not in terminal
    assert repeated["status"] == "accepted"
    assert "semantic_alert" not in repeated
    record = registry.get_session("codex", "sess-terminal-semantic")
    assert record.lifecycle_state == expected_lifecycle
    assert record.watch_status == "inactive"
    assert len(cleanup.calls) == 1


@pytest.mark.asyncio
async def test_webhook_clawhip_native_ingress_writes_work_session_registry(tmp_path):
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "routes": {
                    "clawhip-native": {
                        "secret": _INSECURE_NO_AUTH,
                        "clawhip_native_ingress": True,
                    }
                }
            },
        )
    )
    runner = _RegistryRunner(tmp_path / "work_sessions.json")
    adapter.gateway_runner = runner

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        resp = await cli.post(
            "/webhooks/clawhip-native",
            json={
                "provider": "codex",
                "event_name": "SessionStart",
                "session_id": "sess-webhook-239",
                "repo_path": "/repo",
                "worktree_path": "/repo/.worktrees/ch-239",
                "linear_card_id": "CH-239",
                "lane_id": "lane-239",
                "ingress_route": "spoofed-route",
                "delivery_id": "spoofed-delivery",
            },
            headers={"X-Request-ID": "clawhip-native-001"},
        )
        assert resp.status == 202
        data = await resp.json()

    assert data["status"] == "accepted"
    assert data["verdict"] == "accepted"
    assert data["registry_status"] == "accepted"
    record = runner.work_session_registry.get_session("codex", "sess-webhook-239")
    assert record is not None
    assert record.linear_card_id == "CH-239"
    assert record.lane_id == "lane-239"
    assert record.ingress_route == "clawhip-native"
    assert record.first_delivery_id == "clawhip-native-001"
    assert record.latest_delivery_id == "clawhip-native-001"
    assert record.lifecycle_state == "active"


@pytest.mark.asyncio
async def test_webhook_clawhip_native_ingress_emits_semantic_alert_with_reason(tmp_path):
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "routes": {
                    "clawhip-native": {
                        "secret": _INSECURE_NO_AUTH,
                        "clawhip_native_ingress": True,
                    }
                }
            },
        )
    )
    runner = _AlertingRegistryRunner(tmp_path)
    adapter.gateway_runner = runner
    runner.work_session_registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-webhook-alert",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-242",
            "linear_card_id": "CH-242",
            "lane_id": "yuuka/ch-242-semantic-classifier",
            "tmux_session": "ch242-webhook",
            "tmux_pane": "%42",
            "owner": "hermes",
            "trusted_auto_watch": True,
            "work_id": "wk-delegated-ch-239",
        }
    )

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        resp = await cli.post(
            "/webhooks/clawhip-native",
            json={
                "provider": "codex",
                "event": "ActionRequired",
                "session_id": "sess-webhook-alert",
                "repo_path": "/repo",
                "worktree_path": "/repo/.worktrees/ch-242",
                "tmux_session": "ch242-webhook",
                "tmux_pane": "%42",
                "pane_snapshot": {
                    "visible_text": "Approval required. Reply /approve or /deny."
                },
                "work_id": "wk-delegated-ch-239",
            },
            headers={"X-Request-ID": "clawhip-alert-001"},
        )
        assert resp.status == 202
        data = await resp.json()

    assert data["classification"]["state"] == "permission_prompt"
    assert data["semantic_alert"]["reason"]
    assert data["semantic_alert"]["required_owner_action"]
    assert data["delegated_alert"]["status"] == "accepted"
    assert len(runner.delegated_packets) == 1
    packet = runner.delegated_packets[0]["payload"]
    assert packet["classifier_state"] == "permission_prompt"
    assert packet["classifier_reason"]
    assert packet["required_owner_action"]
    assert "Classifier permission_prompt" in packet["next_action"]


@pytest.mark.asyncio
async def test_webhook_clawhip_native_ingress_rejects_non_object_payload(tmp_path):
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "routes": {
                    "clawhip-native": {
                        "secret": _INSECURE_NO_AUTH,
                        "clawhip_native_ingress": True,
                    }
                }
            },
        )
    )
    adapter.gateway_runner = _RegistryRunner(tmp_path / "work_sessions.json")

    async with TestClient(TestServer(_create_app(adapter))) as cli:
        resp = await cli.post(
            "/webhooks/clawhip-native",
            json=["not", "an", "object"],
            headers={"X-Request-ID": "clawhip-native-array"},
        )
        assert resp.status == 422
        data = await resp.json()

    assert data["status"] == "reject"
    assert data["verdict"] == "reject"
    assert data["registry_status"] == "rejected"
    assert data["reason"] == "missing_provider_session_or_event"


@pytest.mark.parametrize(
    ("event_name", "expected_lifecycle"),
    [
        ("Stop", "stopped"),
        ("Resolved", "resolved"),
        ("OwnerClose", "resolved"),
    ],
)
def test_clawhip_terminal_events_clean_watch_state(tmp_path, event_name, expected_lifecycle):
    registrar = _FakeTmuxWatchRegistrar()
    cleanup = _FakeTmuxWatchCleanup()
    work_state_store = _seed_verified_delegated_work_record(
        tmp_path,
        tmux_session="task-watch",
        repo_path="/repo",
        worktree_path="/repo/.worktrees/task",
    )
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
        tmux_watch_cleanup=cleanup,
        work_state_store=work_state_store,
    )
    registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-terminal",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/task",
            "linear_card_id": "CH-239",
            "lane_id": "lane-239",
            "tmux_session": "task-watch",
            "tmux_pane": "%42",
            "owner": "hermes",
            "trusted_auto_watch": True,
            "timestamp": "2026-04-27T20:00:00+00:00",
        }
    )
    assert len(registrar.calls) == 1

    terminal = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": event_name,
            "session_id": "sess-terminal",
            "timestamp": "2026-04-27T20:07:00+00:00",
        }
    )

    assert terminal["status"] == "accepted"
    record = registry.get_session("codex", "sess-terminal")
    assert record is not None
    assert record.lifecycle_state == expected_lifecycle
    assert record.watch_status == "inactive"
    assert record.watch_cleanup_error is None
    assert record.stopped_at == datetime(2026, 4, 27, 20, 7, tzinfo=timezone.utc)
    assert len(cleanup.calls) == 1
    assert cleanup.calls[0]["session"] == "task-watch"
    assert cleanup.calls[0]["routing"]["source"] == "hermes-work-session-registry"
    assert cleanup.calls[0]["routing"]["owner"] == "hermes"
    assert cleanup.calls[0]["routing"]["tmux_pane"] == "%42"
    assert cleanup.calls[0]["routing"]["lifecycle_state"] == expected_lifecycle


def test_clawhip_cleanup_failure_records_error_and_deactivates_local_watch(tmp_path):
    registrar = _FakeTmuxWatchRegistrar()
    cleanup = _FakeTmuxWatchCleanup(ok=False)
    work_state_store = _seed_verified_delegated_work_record(
        tmp_path,
        tmux_session="task-watch",
        repo_path="/repo",
        worktree_path="/repo/.worktrees/task",
    )
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
        tmux_watch_cleanup=cleanup,
        work_state_store=work_state_store,
    )
    registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-cleanup-failure",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/task",
            "linear_card_id": "CH-239",
            "lane_id": "lane-239",
            "tmux_session": "task-watch",
            "tmux_pane": "%42",
            "owner": "hermes",
            "trusted_auto_watch": True,
            "timestamp": "2026-04-27T20:00:00+00:00",
        }
    )

    stopped = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "Stop",
            "session_id": "sess-cleanup-failure",
            "timestamp": "2026-04-27T20:07:00+00:00",
        }
    )

    assert stopped["status"] == "accepted"
    record = registry.get_session("codex", "sess-cleanup-failure")
    assert record.watch_status == "inactive"
    assert record.watch_cleanup_error == "{'ok': False}"
    assert len(cleanup.calls) == 1


class _FakeClawhipDeliver:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, deliver_request):
        self.calls.append(dict(deliver_request))
        return dict(self.result)


def _trusted_deliver_registry(tmp_path, *, deliver_result=None, tmux_session="deliver-tmux"):
    registrar = _FakeTmuxWatchRegistrar()
    deliver = _FakeClawhipDeliver(deliver_result or {
        "ok": True,
        "prompt_submit_marker_before": "marker-1",
        "prompt_submit_marker_after": "marker-2",
        "pane_evidence": {"event": "pane-output-changed"},
    })
    work_state_store = _seed_verified_delegated_work_record(
        tmp_path,
        tmux_session=tmux_session,
        repo_path="/repo",
        worktree_path="/repo/.worktrees/ch-243",
    )
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        tmux_watch_registrar=registrar,
        deliver_executor=deliver,
        work_state_store=work_state_store,
    )
    start = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-deliver",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-243",
            "repo_name": "hermes-agent",
            "linear_card_id": "CH-243",
            "lane_id": "yuuka/ch-243-deliver-policy",
            "tmux_session": tmux_session,
            "tmux_pane": "%7",
            "owner": "hermes",
            "trusted_auto_watch": True,
            "work_id": "wk-delegated-ch-239",
            "timestamp": "2026-04-27T20:00:00+00:00",
        }
    )
    assert start["status"] == "accepted"
    assert start["record"].watch_status == "active"
    return registry, registrar, deliver


def test_default_clawhip_deliver_executor_uses_current_clawhip_cli_contract(monkeypatch, tmp_path):
    calls = []
    worktree = tmp_path / "repo" / ".worktrees" / "ch-243"
    marker = worktree / ".clawhip" / "state" / "prompt-submit.json"
    marker.parent.mkdir(parents=True)
    marker.write_text('{"prompt":"before"}')

    class _Completed:
        def __init__(self, *, returncode=0, stdout=b"", stderr=b""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command, **kwargs):
        calls.append({"command": command, "kwargs": kwargs})
        if command[:3] == ["tmux", "capture-pane", "-p"]:
            return _Completed(stdout=f"pane-{len(calls)}".encode())
        marker.write_text('{"prompt":"after"}')
        return _Completed(stdout="Delivered prompt to codex session 'deliver-tmux' via codex", stderr="")

    monkeypatch.setattr("gateway.work_state.subprocess.run", fake_run)

    result = _default_clawhip_deliver_executor(
        {
            "provider": "codex",
            "provider_session_id": "sess-deliver",
            "tmux_session": "deliver-tmux",
            "tmux_pane": "%7",
            "prompt": "bounded prompt",
            "worktree_path": str(worktree),
        }
    )

    assert result["ok"] is True
    assert result["stdout"] == "Delivered prompt to codex session 'deliver-tmux' via codex"
    assert result["prompt_submit_marker_before"] != result["prompt_submit_marker_after"]
    assert result["pane_evidence"] == {"event": "pane-output-changed", "target": "%7"}
    assert calls[1] == {
        "command": ["clawhip", "deliver", "--session", "deliver-tmux", "--prompt", "bounded prompt"],
        "kwargs": {
            "capture_output": True,
            "text": True,
            "timeout": 10,
            "cwd": str(worktree),
        },
    }


def test_default_clawhip_deliver_executor_requires_target_session():
    result = _default_clawhip_deliver_executor({"prompt": "bounded prompt"})

    assert result == {"ok": False, "error": "clawhip_deliver_missing_tmux_session"}


def test_clawhip_deliver_policy_allows_active_trusted_session_and_bounds_prompt(tmp_path):
    registry, registrar, deliver = _trusted_deliver_registry(tmp_path)

    result = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "ActionRequired",
            "session_id": "sess-deliver",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-243",
            "tmux_session": "deliver-tmux",
            "tmux_pane": "%7",
            "pane_snapshot": {"visible_text": "No output for 12 minutes"},
            "idle_seconds": 720,
            "stale_minutes": 10,
            "work_id": "wk-delegated-ch-239",
            "timestamp": "2026-04-27T20:03:00+00:00",
        }
    )

    assert result["status"] == "accepted"
    assert result["deliver_policy"]["status"] == "succeeded"
    assert result["deliver_policy"]["reason"] == "prompt_submit_marker_and_followup_evidence"
    assert "semantic_alert" not in result
    assert len(deliver.calls) == 1
    request = deliver.calls[0]
    assert request["provider"] == "codex"
    assert request["provider_session_id"] == "sess-deliver"
    assert request["linear_card_id"] == "CH-243"
    assert request["lane_id"] == "yuuka/ch-243-deliver-policy"
    assert request["repo_path"] == "/repo"
    assert request["worktree_path"] == "/repo/.worktrees/ch-243"
    assert request["tmux_session"] == "deliver-tmux"
    assert request["tmux_pane"] == "%7"
    prompt = request["prompt"]
    assert "Card: CH-243" in prompt
    assert "Lane: yuuka/ch-243-deliver-policy" in prompt
    assert "Repo: hermes-agent" in prompt
    assert "Do not" in prompt and "unrelated session" in prompt
    assert len(prompt) <= 1200
    record = registry.get_session("codex", "sess-deliver")
    assert record.lifecycle_state == "active"
    assert record.deliver_status == "succeeded"
    assert record.deliver_attempts == 1
    assert record.deliver_evidence["marker_changed"] is True
    assert record.deliver_evidence["followup_evidence"] is True
    assert len(registrar.calls) == 1


@pytest.mark.parametrize(
    ("event_payload", "expected_reason"),
    [
        ({"event": "Resolved"}, "work_session_not_active"),
        ({"event": "Stop"}, "work_session_not_active"),
        (
            {
                "event": "ActionRequired",
                "pane_snapshot": {"visible_text": "All tests passed. Process exited with code 0."},
                "completed": True,
            },
            "classification_not_actionable",
        ),
    ],
)
def test_clawhip_deliver_policy_refuses_completed_resolved_and_stopped_sessions(
    tmp_path,
    event_payload,
    expected_reason,
):
    registry, _registrar, deliver = _trusted_deliver_registry(tmp_path)
    if event_payload["event"] in {"Resolved", "Stop"}:
        registry.ingest_clawhip_native_event(
            {
                "provider": "codex",
                "session_id": "sess-deliver",
                "timestamp": "2026-04-27T20:01:00+00:00",
                **event_payload,
            }
        )
        action_payload = {
            "event": "ActionRequired",
            "pane_snapshot": {"visible_text": "No output for 12 minutes"},
            "idle_seconds": 720,
            "stale_minutes": 10,
        }
    else:
        action_payload = event_payload

    result = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "session_id": "sess-deliver",
            "timestamp": "2026-04-27T20:02:00+00:00",
            **action_payload,
        }
    )

    assert result["status"] == "accepted"
    assert result["deliver_policy"]["status"] == "refused"
    assert result["deliver_policy"]["reason"] == expected_reason
    assert deliver.calls == []


def test_clawhip_deliver_policy_refuses_unknown_and_non_hooked_sessions(tmp_path):
    deliver = _FakeClawhipDeliver({"ok": True})
    registry = WorkSessionRegistry(
        tmp_path / "work_sessions.json",
        deliver_executor=deliver,
        work_state_store=WorkStateStore(tmp_path / "work_state.json"),
    )
    registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-unhooked",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/ch-243",
            "linear_card_id": "CH-243",
            "lane_id": "yuuka/ch-243-deliver-policy",
            "tmux_session": "plain-shell",
            "tmux_pane": "%3",
        }
    )

    result = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "ActionRequired",
            "session_id": "sess-unhooked",
            "pane_snapshot": {"visible_text": "No output for 12 minutes"},
            "idle_seconds": 720,
            "stale_minutes": 10,
        }
    )
    missing = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "ActionRequired",
            "session_id": "unknown-session",
            "pane_snapshot": {"visible_text": "No output for 12 minutes"},
            "idle_seconds": 720,
            "stale_minutes": 10,
        }
    )

    assert result["deliver_policy"]["status"] == "refused"
    assert result["deliver_policy"]["reason"] == "work_session_not_trusted_hooked"
    assert missing["status"] == "rejected"
    assert missing["reason"] == "missing_card_or_lane_linkage"
    assert deliver.calls == []


@pytest.mark.parametrize(
    "deliver_result",
    [
        {"ok": True, "prompt_submit_marker_before": "same", "prompt_submit_marker_after": "same", "pane_evidence": {"event": "changed"}},
        {"ok": True, "prompt_submit_marker_before": "before", "prompt_submit_marker_after": "after"},
        {"ok": True, "prompt_submit_marker_before": "before", "prompt_submit_marker_after": "after", "event_name": "ActionRequired"},
    ],
)
def test_clawhip_deliver_success_requires_marker_change_and_followup_evidence(tmp_path, deliver_result):
    registry, _registrar, deliver = _trusted_deliver_registry(
        tmp_path,
        deliver_result=deliver_result,
    )

    first = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "ActionRequired",
            "session_id": "sess-deliver",
            "pane_snapshot": {"visible_text": "No output for 12 minutes"},
            "idle_seconds": 720,
            "stale_minutes": 10,
            "work_id": "wk-delegated-ch-239",
            "timestamp": "2026-04-27T20:03:00+00:00",
        }
    )
    repeated = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "ActionRequired",
            "session_id": "sess-deliver",
            "pane_snapshot": {"visible_text": "No output for 14 minutes"},
            "idle_seconds": 840,
            "stale_minutes": 10,
            "work_id": "wk-delegated-ch-239",
            "timestamp": "2026-04-27T20:05:00+00:00",
        }
    )

    assert first["deliver_policy"]["status"] == "failed"
    assert "semantic_alert" in first
    record = registry.get_session("codex", "sess-deliver")
    assert record.lifecycle_state == "failed"
    assert record.deliver_status == "failed"
    assert record.deliver_attempts == 1
    assert len(deliver.calls) == 1
    assert repeated["deliver_policy"]["status"] == "refused"
    assert repeated["deliver_policy"]["reason"] == "work_session_not_active"
    assert len(deliver.calls) == 1
