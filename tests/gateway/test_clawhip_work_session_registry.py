from datetime import datetime, timezone

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.work_state import WorkRecord, WorkSessionRegistry, WorkStateStore


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


class _RegistryRunner:
    def __init__(self, path):
        self.work_session_registry = WorkSessionRegistry(path)


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
