from datetime import datetime, timezone

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.work_state import WorkSessionRegistry


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


class _RegistryRunner:
    def __init__(self, path):
        self.work_session_registry = WorkSessionRegistry(path)



def test_clawhip_session_start_requires_explicit_card_and_lane(tmp_path):
    registry = WorkSessionRegistry(tmp_path / "work_sessions.json")

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


def test_clawhip_native_events_update_registry_without_overwriting_upstream_keys(tmp_path):
    registry = WorkSessionRegistry(tmp_path / "work_sessions.json")
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
            "timestamp": started,
        }
    )

    assert start["status"] == "accepted"
    assert start["record"].provider_session_id == "sess-239"
    assert start["record"].lifecycle_state == "active"
    assert start["record"].watch_status == "unwatched"

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
                "event": "SessionStart",
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


def test_clawhip_stop_marks_session_stopped_and_cleans_watch_state(tmp_path):
    registry = WorkSessionRegistry(tmp_path / "work_sessions.json")
    registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "SessionStart",
            "session_id": "sess-stop",
            "repo_path": "/repo",
            "worktree_path": "/repo/.worktrees/task",
            "linear_card_id": "CH-239",
            "lane_id": "lane-239",
            "tmux_session": "task-watch",
            "watch_status": "active",
            "timestamp": "2026-04-27T20:00:00+00:00",
        }
    )

    stopped = registry.ingest_clawhip_native_event(
        {
            "provider": "codex",
            "event": "Stop",
            "session_id": "sess-stop",
            "timestamp": "2026-04-27T20:07:00+00:00",
        }
    )

    assert stopped["status"] == "accepted"
    record = registry.get_session("codex", "sess-stop")
    assert record is not None
    assert record.lifecycle_state == "stopped"
    assert record.watch_status == "inactive"
    assert record.stopped_at == datetime(2026, 4, 27, 20, 7, tzinfo=timezone.utc)
