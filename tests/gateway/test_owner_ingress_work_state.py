import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from gateway.work_state import WorkRecord, WorkStateStore


class _CaptureAdapter:
    def __init__(self):
        self.events = []
        self.sent = []

    async def handle_message(self, event):
        self.events.append(event)

    async def send(self, chat_id, content, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return None


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _make_source(chat_id: str = "chat-1", user_id: str = "user-1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id=user_id,
        user_name=user_id,
        chat_type="dm",
    )


def _make_discord_thread_source(
    thread_id: str = "1493539933749776415",
    user_id: str = "user-1",
    *,
    chat_id: str | None = None,
    include_thread_id: bool = True,
) -> SessionSource:
    resolved_chat_id = chat_id or thread_id
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=resolved_chat_id,
        chat_name="guild / #ops / thread",
        chat_type="thread",
        user_id=user_id,
        user_name=user_id,
        thread_id=thread_id if include_thread_id else None,
    )


def _make_runner(tmp_path):
    config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="***"),
            Platform.WEBHOOK: PlatformConfig(enabled=True, extra={}),
        }
    )
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    work_state_store = WorkStateStore(tmp_path / "work_state.json")
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner.work_state_store = work_state_store
    runner.adapters = {Platform.TELEGRAM: _CaptureAdapter()}
    return runner, store, work_state_store


@pytest.mark.asyncio
async def test_owner_ingress_route_injects_internal_event_for_single_live_work(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)

    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-direct-1",
            title="resume interrupted gateway turn",
            objective="resume the blocked direct work after restart",
            owner="hermes",
            executor="hermes",
            mode="direct",
            owner_session_id=entry.session_key,
            state="blocked",
            started_at=now,
            last_progress_at=now,
            next_action="Resume the interrupted owner turn",
            proof="gateway_restart_checkpoint",
        )
    )

    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "0.0.0.0",
                "port": 0,
                "routes": {
                    "owner-ingress": {
                        "secret": _INSECURE_NO_AUTH,
                        "owner_ingress": True,
                    }
                },
            },
        )
    )
    adapter.gateway_runner = runner

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/webhooks/owner-ingress",
            json={
                "work_id": "wk-direct-1",
                "owner": "hermes",
                "owner_session_id": entry.session_key,
                "state": "blocked",
                "next_action": "Resume the interrupted owner turn",
                "proof": "gateway_restart_checkpoint",
            },
            headers={"X-Request-ID": "owner-ingress-001"},
        )
        assert resp.status == 202
        data = await resp.json()
        assert data["status"] == "accepted"
        assert data["verdict"] == "accepted"
        assert data["resolution"] == "single_match"
        assert data["target_session_key"] == entry.session_key

    await asyncio.sleep(0.05)

    capture_adapter = runner.adapters[Platform.TELEGRAM]
    assert len(capture_adapter.events) == 1
    event = capture_adapter.events[0]
    assert event.internal is True
    assert event.source.platform == Platform.TELEGRAM
    assert event.source.chat_id == source.chat_id
    assert event.source.user_id == source.user_id
    assert "wk-direct-1" in event.text
    assert "Resume the interrupted owner turn" in event.text
    assert "gateway_restart_checkpoint" in event.text


@pytest.mark.asyncio
async def test_owner_ingress_route_rejects_ambiguous_live_work_matches(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source_a = _make_source(chat_id="chat-a", user_id="user-a")
    source_b = _make_source(chat_id="chat-b", user_id="user-b")
    entry_a = store.get_or_create_session(source_a)
    entry_b = store.get_or_create_session(source_b)

    now = datetime.now(timezone.utc)
    for entry in (entry_a, entry_b):
        work_state_store.upsert(
            WorkRecord(
                work_id="wk-ambiguous",
                title="ambiguous work",
                objective="prove 2+ matches refuse wake",
                owner="hermes",
                executor="hermes",
                mode="direct",
                owner_session_id=entry.session_key,
                state="blocked",
                started_at=now,
                last_progress_at=now,
                next_action="Do not broad-wake",
                proof="test-fixture",
            )
        )

    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "0.0.0.0",
                "port": 0,
                "routes": {
                    "owner-ingress": {
                        "secret": _INSECURE_NO_AUTH,
                        "owner_ingress": True,
                    }
                },
            },
        )
    )
    adapter.gateway_runner = runner

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/webhooks/owner-ingress",
            json={
                "work_id": "wk-ambiguous",
                "owner": "hermes",
                "state": "blocked",
                "next_action": "Do not broad-wake",
                "proof": "test-fixture",
            },
            headers={"X-Request-ID": "owner-ingress-ambiguous"},
        )
        assert resp.status == 409
        data = await resp.json()
        assert data["verdict"] == "reject"
        assert data["reason"] == "ambiguous_owner_resolution"
        assert data["resolution"] == "ambiguous"

    await asyncio.sleep(0.05)
    capture_adapter = runner.adapters[Platform.TELEGRAM]
    assert capture_adapter.events == []


def test_recover_interrupted_sessions_marks_running_direct_work_blocked(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)

    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-restart-1",
            title="restart interrupted work",
            objective="derive blocked signal from gateway restart checkpoint",
            owner="hermes",
            executor="hermes",
            mode="direct",
            owner_session_id=entry.session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Continue the interrupted turn",
        )
    )

    checkpoint_path = tmp_path / ".running_sessions.json"
    checkpoint_path.write_text(
        json.dumps({"session_keys": [entry.session_key]}),
        encoding="utf-8",
    )

    recovered = GatewayRunner._recover_interrupted_sessions_from_checkpoint(runner)
    assert recovered == 1

    signal = work_state_store.derive_direct_signal(
        "wk-restart-1",
        now=now + timedelta(minutes=1),
        stale_after_seconds=3600,
    )
    assert signal is not None
    assert signal["state"] == "blocked"
    assert signal["owner_session_id"] == entry.session_key
    assert signal["proof"] == "gateway_restart_checkpoint"
    assert "Continue the interrupted turn" in signal["next_action"]


def test_derive_direct_signal_promotes_stale_from_last_progress_age(tmp_path):
    work_state_store = WorkStateStore(tmp_path / "work_state.json")
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-stale-1",
            title="stale work",
            objective="derive stale from work-state age",
            owner="hermes",
            executor="hermes",
            mode="direct",
            owner_session_id="agent:main:telegram:dm:chat-1",
            state="running",
            started_at=now - timedelta(minutes=20),
            last_progress_at=now - timedelta(minutes=20),
            next_action="Check the stuck direct work",
        )
    )

    signal = work_state_store.derive_direct_signal(
        "wk-stale-1",
        now=now,
        stale_after_seconds=300,
    )

    assert signal is not None
    assert signal["state"] == "stale"
    assert signal["work_id"] == "wk-stale-1"
    assert signal["owner"] == "hermes"
    assert signal["owner_session_id"] == "agent:main:telegram:dm:chat-1"
    assert signal["next_action"] == "Check the stuck direct work"
    assert "last_progress_at" in signal["proof"]


def _make_message_runner(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    runner.hooks = type("Hooks", (), {"emit": AsyncMock()})()
    runner._set_session_env = lambda _context: ()
    runner._prepare_inbound_message_text = AsyncMock(return_value="investigate direct work")
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    return runner, store, work_state_store


@pytest.mark.asyncio
async def test_handle_message_with_agent_tracks_direct_work_record_success(tmp_path):
    runner, store, work_state_store = _make_message_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)

    async def fake_run_agent(**_kwargs):
        records = work_state_store.list_records()
        assert len(records) == 1
        assert records[0].state == "running"
        return {
            "final_response": "done",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "api_calls": 1,
        }

    runner._run_agent = fake_run_agent

    event = gateway_run.MessageEvent(
        text="investigate direct work",
        message_type=gateway_run.MessageType.TEXT,
        source=source,
        message_id="msg-success",
    )

    result = await GatewayRunner._handle_message_with_agent(
        runner,
        event,
        source,
        session_entry.session_key,
    )

    assert result == "done"
    records = work_state_store.list_records()
    assert len(records) == 1
    assert records[0].owner_session_id == session_entry.session_key
    assert records[0].state == "finished"
    assert records[0].proof == "agent_completed"


@pytest.mark.asyncio
async def test_handle_message_with_agent_tracks_direct_work_record_failure(tmp_path):
    runner, store, work_state_store = _make_message_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)

    async def fake_run_agent(**_kwargs):
        records = work_state_store.list_records()
        assert len(records) == 1
        assert records[0].state == "running"
        raise RuntimeError("boom")

    runner._run_agent = fake_run_agent

    event = gateway_run.MessageEvent(
        text="investigate direct work",
        message_type=gateway_run.MessageType.TEXT,
        source=source,
        message_id="msg-fail",
    )

    result = await GatewayRunner._handle_message_with_agent(
        runner,
        event,
        source,
        session_entry.session_key,
    )

    assert "encountered an error" in result.lower()
    records = work_state_store.list_records()
    assert len(records) == 1
    assert records[0].owner_session_id == session_entry.session_key
    assert records[0].state == "failed"
    assert records[0].proof == "agent_failed"


@pytest.mark.asyncio
async def test_handle_message_with_agent_reuses_targeted_internal_direct_work_record(tmp_path):
    runner, store, work_state_store = _make_message_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-direct-resume-1",
            title="resume blocked direct work",
            objective="resume the exact blocked direct work record rather than creating a duplicate",
            owner="hermes",
            executor="hermes",
            mode="direct",
            owner_session_id=session_entry.session_key,
            state="blocked",
            started_at=now,
            last_progress_at=now,
            next_action="Resume the interrupted owner turn",
            proof="gateway_restart_checkpoint",
        )
    )

    async def fake_run_agent(**_kwargs):
        records = work_state_store.list_records()
        assert len(records) == 1
        assert records[0].work_id == "wk-direct-resume-1"
        assert records[0].state == "running"
        return {
            "final_response": "resumed",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "api_calls": 1,
        }

    runner._run_agent = fake_run_agent

    event = gateway_run.MessageEvent(
        text="[System note: resume only this targeted work record]",
        message_type=gateway_run.MessageType.TEXT,
        source=source,
        message_id="owner-ingress:direct-resume",
        raw_message={"work_id": "wk-direct-resume-1", "owner": "hermes"},
        internal=True,
    )

    result = await GatewayRunner._handle_message_with_agent(
        runner,
        event,
        source,
        session_entry.session_key,
    )

    assert result == "resumed"
    records = work_state_store.list_records()
    assert len(records) == 1
    assert records[0].work_id == "wk-direct-resume-1"
    assert records[0].state == "finished"
    assert records[0].proof == "agent_completed"


@pytest.mark.asyncio
async def test_handle_message_with_agent_does_not_create_duplicate_direct_record_for_internal_delegated_followup(tmp_path):
    runner, store, work_state_store = _make_message_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-blocked-1",
            title="delegated blocked work",
            objective="keep delegated ledger state stable while the owner handles the follow-up",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="blocked",
            started_at=now,
            last_progress_at=now,
            next_action="Inspect the blocked delegated run",
            executor_session_id="proc-delegated-1",
            proof="clawhip:session.blocked",
        )
    )

    async def fake_run_agent(**_kwargs):
        records = work_state_store.list_records()
        assert len(records) == 1
        assert records[0].work_id == "wk-delegated-blocked-1"
        assert records[0].mode == "delegated"
        assert records[0].state == "blocked"
        return {
            "final_response": "owner handled",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
            "api_calls": 1,
        }

    runner._run_agent = fake_run_agent

    event = gateway_run.MessageEvent(
        text="[System note: delegated owner follow-up]",
        message_type=gateway_run.MessageType.TEXT,
        source=source,
        message_id="owner-ingress:delegated-followup",
        raw_message={"work_id": "wk-delegated-blocked-1", "owner": "hermes"},
        internal=True,
    )

    result = await GatewayRunner._handle_message_with_agent(
        runner,
        event,
        source,
        session_entry.session_key,
    )

    assert result == "owner handled"
    records = work_state_store.list_records()
    assert len(records) == 1
    assert records[0].work_id == "wk-delegated-blocked-1"
    assert records[0].mode == "delegated"
    assert records[0].state == "blocked"
    assert records[0].proof == "clawhip:session.blocked"


def test_mark_work_record_delegated_handoff_writes_executor_correlation_fields(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)

    work_id = GatewayRunner._begin_direct_work_record(
        runner,
        session_id=session_entry.session_id,
        session_key=session_entry.session_key,
        message_text="delegate this task to OMX",
        platform="telegram",
        event_message_id="msg-delegated",
    )

    updated = GatewayRunner._mark_work_record_delegated(
        runner,
        work_id,
        session_entry.session_key,
        executor_session_id="omx-session-123",
        tmux_session="omx-hermes-test",
        repo_path="/repo/demo",
        worktree_path="/repo/demo",
        next_action="Resume the OMX-owned delegated run",
        proof="delegated_handoff:test",
    )

    assert updated is True
    records = work_state_store.list_records()
    assert len(records) == 1
    record = records[0]
    assert record.work_id == work_id
    assert record.owner == "hermes"
    assert record.executor == "omx"
    assert record.mode == "delegated"
    assert record.owner_session_id == session_entry.session_key
    assert record.executor_session_id == "omx-session-123"
    assert record.tmux_session == "omx-hermes-test"
    assert record.repo_path == "/repo/demo"
    assert record.worktree_path == "/repo/demo"
    assert record.next_action == "Resume the OMX-owned delegated run"
    assert record.proof == "delegated_handoff:test"


@pytest.mark.asyncio
async def test_handle_delegated_ingress_packet_resolves_single_match_and_injects_owner_followup(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-1",
            title="delegated work",
            objective="correlate OMX signal back to one owner session",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Wait for delegated executor signal",
            executor_session_id="omx-session-123",
            tmux_session="omx-hermes-test",
            repo_path="/repo/demo",
            worktree_path="/repo/demo",
            proof="delegated_handoff:test",
        )
    )

    result = await GatewayRunner.handle_delegated_ingress_packet(
        runner,
        {
            "owner": "hermes",
            "state": "blocked",
            "normalized_event": "blocked",
            "executor": "omx",
            "executor_session_id": "omx-session-123",
            "tmux_session": "omx-hermes-test",
            "repo_path": "/repo/demo",
            "worktree_path": "/repo/demo",
            "next_action": "Wake the owner and inspect the blocked OMX run",
            "proof": "clawhip:session.blocked",
        },
        route_name="delegated-ingress",
        delivery_id="delegated-001",
    )

    assert result["status"] == "accepted"
    assert result["verdict"] == "accepted"
    assert result["resolution"] == "single_match"
    assert result["work_id"] == "wk-delegated-1"
    assert result["target_session_key"] == session_entry.session_key

    capture_adapter = runner.adapters[Platform.TELEGRAM]
    assert len(capture_adapter.events) == 1
    event = capture_adapter.events[0]
    assert event.internal is True
    assert "wk-delegated-1" in event.text
    assert "Wake the owner and inspect the blocked OMX run" in event.text


@pytest.mark.asyncio
async def test_handle_delegated_ingress_packet_preserves_discord_thread_source_for_owner_followup(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    runner.adapters[Platform.DISCORD] = _CaptureAdapter()
    source = _make_discord_thread_source(thread_id="thread-42", user_id="discord-user")
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-discord-thread-1",
            title="delegated discord thread work",
            objective="relay actionable alert back into the owning Discord thread",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Wait for delegated executor signal",
            executor_session_id="omx-session-discord-1",
            tmux_session="omx-discord-thread-test",
            repo_path="/repo/demo",
            worktree_path="/repo/demo",
            proof="delegated_handoff:test",
        )
    )

    result = await GatewayRunner.handle_delegated_ingress_packet(
        runner,
        {
            "owner": "hermes",
            "state": "blocked",
            "normalized_event": "blocked",
            "executor": "omx",
            "executor_session_id": "omx-session-discord-1",
            "tmux_session": "omx-discord-thread-test",
            "repo_path": "/repo/demo",
            "worktree_path": "/repo/demo",
            "next_action": "Relay the actionable alert into the owning Discord thread",
            "proof": "clawhip:session.blocked",
        },
        route_name="delegated-ingress",
        delivery_id="delegated-discord-thread-001",
    )

    assert result["status"] == "accepted"
    assert result["resolution"] == "single_match"

    capture_adapter = runner.adapters[Platform.DISCORD]
    assert len(capture_adapter.events) == 1
    event = capture_adapter.events[0]
    assert event.internal is True
    assert event.source.platform == Platform.DISCORD
    assert event.source.chat_type == "thread"
    assert event.source.chat_id == "thread-42"
    assert event.source.thread_id == "thread-42"
    assert "wk-delegated-discord-thread-1" in event.text
    assert "Relay the actionable alert into the owning Discord thread" in event.text


@pytest.mark.asyncio
async def test_handle_delegated_ingress_packet_rejects_missing_discord_thread_source(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    runner.adapters[Platform.DISCORD] = _CaptureAdapter()
    source = _make_discord_thread_source(
        thread_id="thread-missing",
        user_id="discord-user",
        chat_id="parent-channel-1",
        include_thread_id=False,
    )
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-discord-thread-missing",
            title="delegated discord thread work missing thread id",
            objective="refuse wrong-thread contamination when thread metadata cannot be restored",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Wait for delegated executor signal",
            executor_session_id="omx-session-discord-missing",
            tmux_session="omx-discord-thread-missing",
            repo_path="/repo/demo",
            worktree_path="/repo/demo",
            proof="delegated_handoff:test",
        )
    )

    result = await GatewayRunner.handle_delegated_ingress_packet(
        runner,
        {
            "owner": "hermes",
            "state": "blocked",
            "normalized_event": "blocked",
            "executor": "omx",
            "executor_session_id": "omx-session-discord-missing",
            "tmux_session": "omx-discord-thread-missing",
            "repo_path": "/repo/demo",
            "worktree_path": "/repo/demo",
            "next_action": "Do not leak this alert into the parent channel",
            "proof": "clawhip:session.blocked",
        },
        route_name="delegated-ingress",
        delivery_id="delegated-discord-thread-missing",
    )

    assert result["status"] == "reject"
    assert result["reason"] == "missing_owner_thread_source"
    assert result["resolution"] == "missing"

    capture_adapter = runner.adapters[Platform.DISCORD]
    assert capture_adapter.events == []


@pytest.mark.asyncio
async def test_handle_delegated_ingress_packet_retry_needed_prefers_executor_followup_without_owner_wake(
    tmp_path,
    monkeypatch,
):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-retry-1",
            title="delegated retry work",
            objective="retry-needed should stay executor-first when a bounded executor target exists",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Wait for delegated executor signal",
            executor_session_id="proc-retry-1",
            tmux_session="omx-retry-test",
            repo_path="/repo/demo",
            worktree_path="/repo/demo",
            proof="delegated_handoff:test",
        )
    )

    dispatched = {}

    def _fake_dispatch(self, record, *, next_action, proof, route_name=""):
        dispatched["work_id"] = record.work_id
        dispatched["next_action"] = next_action
        dispatched["proof"] = proof
        dispatched["route_name"] = route_name
        return {
            "status": "accepted",
            "route": "process_stdin",
            "target": record.executor_session_id,
        }

    monkeypatch.setattr(
        GatewayRunner,
        "_dispatch_delegated_retry_followup",
        _fake_dispatch,
        raising=False,
    )

    result = await GatewayRunner.handle_delegated_ingress_packet(
        runner,
        {
            "owner": "hermes",
            "state": "retry_needed",
            "normalized_event": "retry-needed",
            "executor": "omx",
            "executor_session_id": "proc-retry-1",
            "tmux_session": "omx-retry-test",
            "repo_path": "/repo/demo",
            "worktree_path": "/repo/demo",
            "next_action": "Retry the OMX lane exactly once using the current session context",
            "proof": "clawhip:retry-needed",
        },
        route_name="delegated-ingress",
        delivery_id="delegated-retry-001",
    )

    assert result["status"] == "accepted"
    assert result["verdict"] == "accepted"
    assert result["resolution"] == "single_match"
    assert result["reaction"] == "executor_first"
    assert result["work_id"] == "wk-delegated-retry-1"
    assert result["dispatch_route"] == "process_stdin"
    assert result["target_executor_id"] == "proc-retry-1"
    assert dispatched == {
        "work_id": "wk-delegated-retry-1",
        "next_action": "Retry the OMX lane exactly once using the current session context",
        "proof": "clawhip:retry-needed",
        "route_name": "delegated-ingress",
    }

    capture_adapter = runner.adapters[Platform.TELEGRAM]
    assert capture_adapter.events == []

    record = work_state_store.resolve_delegated_signal_candidate(
        work_id="wk-delegated-retry-1",
        live_only=False,
    )["record"]
    assert record.state == "running"
    assert record.next_action == "Wait for delegated executor retry outcome"
    assert record.proof == "retry_dispatch:process_stdin|source=clawhip:retry-needed"


@pytest.mark.asyncio
async def test_handle_delegated_ingress_packet_retry_needed_falls_back_to_owner_wake_when_executor_followup_unavailable(
    tmp_path,
    monkeypatch,
):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-retry-2",
            title="delegated retry fallback work",
            objective="owner wake should happen only after executor-first follow-up is unavailable",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Wait for delegated executor signal",
            executor_session_id="proc-retry-missing",
            tmux_session="omx-retry-missing",
            repo_path="/repo/demo",
            worktree_path="/repo/demo",
            proof="delegated_handoff:test",
        )
    )

    def _fake_dispatch(self, record, *, next_action, proof, route_name=""):
        return {
            "status": "unavailable",
            "reason": "no_live_executor_surface",
        }

    monkeypatch.setattr(
        GatewayRunner,
        "_dispatch_delegated_retry_followup",
        _fake_dispatch,
        raising=False,
    )

    result = await GatewayRunner.handle_delegated_ingress_packet(
        runner,
        {
            "owner": "hermes",
            "state": "retry_needed",
            "normalized_event": "retry-needed",
            "executor": "omx",
            "executor_session_id": "proc-retry-missing",
            "tmux_session": "omx-retry-missing",
            "repo_path": "/repo/demo",
            "worktree_path": "/repo/demo",
            "next_action": "Retry the OMX lane exactly once using the current session context",
            "proof": "clawhip:retry-needed",
        },
        route_name="delegated-ingress",
        delivery_id="delegated-retry-002",
    )

    assert result["status"] == "accepted"
    assert result["verdict"] == "accepted"
    assert result["resolution"] == "single_match"
    assert result["reaction"] == "owner_fallback"
    assert result["work_id"] == "wk-delegated-retry-2"

    capture_adapter = runner.adapters[Platform.TELEGRAM]
    assert len(capture_adapter.events) == 1
    event = capture_adapter.events[0]
    assert event.internal is True
    assert "wk-delegated-retry-2" in event.text
    assert "Retry the OMX lane exactly once using the current session context" in event.text

    record = work_state_store.resolve_delegated_signal_candidate(
        work_id="wk-delegated-retry-2",
        live_only=False,
    )["record"]
    assert record.state == "retry_needed"
    assert record.next_action == "Retry the OMX lane exactly once using the current session context"
    assert record.proof == "clawhip:retry-needed"


@pytest.mark.asyncio
async def test_handle_delegated_ingress_packet_rejects_missing_match(tmp_path):
    runner, store, _work_state_store = _make_runner(tmp_path)
    source = _make_source()
    store.get_or_create_session(source)

    result = await GatewayRunner.handle_delegated_ingress_packet(
        runner,
        {
            "owner": "hermes",
            "state": "blocked",
            "normalized_event": "blocked",
            "executor": "omx",
            "executor_session_id": "omx-session-missing",
            "tmux_session": "omx-missing",
            "repo_path": "/repo/demo",
            "worktree_path": "/repo/demo",
            "next_action": "Do not broad-wake anyone",
            "proof": "clawhip:session.blocked",
        },
        route_name="delegated-ingress",
        delivery_id="delegated-missing",
    )

    assert result["status"] == "reject"
    assert result["verdict"] == "reject"
    assert result["resolution"] == "missing"


@pytest.mark.asyncio
async def test_handle_delegated_ingress_packet_rejects_ambiguous_match(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source_a = _make_source(chat_id="chat-a", user_id="user-a")
    source_b = _make_source(chat_id="chat-b", user_id="user-b")
    entry_a = store.get_or_create_session(source_a)
    entry_b = store.get_or_create_session(source_b)
    now = datetime.now(timezone.utc)

    for index, entry in enumerate((entry_a, entry_b), start=1):
        work_state_store.upsert(
            WorkRecord(
                work_id=f"wk-delegated-{index}",
                title="delegated work",
                objective="prove delegated ambiguity rejects owner wake",
                owner="hermes",
                executor="omx",
                mode="delegated",
                owner_session_id=entry.session_key,
                state="blocked",
                started_at=now,
                last_progress_at=now,
                next_action="Do not broad-wake delegated work",
                executor_session_id="omx-session-shared",
                tmux_session="omx-shared",
                repo_path="/repo/demo",
                worktree_path="/repo/demo",
                proof="delegated_handoff:test",
            )
        )

    result = await GatewayRunner.handle_delegated_ingress_packet(
        runner,
        {
            "owner": "hermes",
            "state": "blocked",
            "normalized_event": "blocked",
            "executor": "omx",
            "executor_session_id": "omx-session-shared",
            "tmux_session": "omx-shared",
            "repo_path": "/repo/demo",
            "worktree_path": "/repo/demo",
            "next_action": "Do not broad-wake delegated work",
            "proof": "clawhip:session.blocked",
        },
        route_name="delegated-ingress",
        delivery_id="delegated-ambiguous",
    )

    assert result["status"] == "reject"
    assert result["verdict"] == "reject"
    assert result["resolution"] == "ambiguous"

    capture_adapter = runner.adapters[Platform.TELEGRAM]
    assert capture_adapter.events == []


def test_resolve_delegated_signal_candidate_refreshes_external_store_updates(tmp_path):
    path = tmp_path / "work_state.json"
    reader = WorkStateStore(path)
    assert reader.list_records() == []

    writer = WorkStateStore(path)
    now = datetime.now(timezone.utc)
    writer.upsert(
        WorkRecord(
            work_id="wk-delegated-refresh-1",
            title="delegated refresh",
            objective="reader store should see delegated records written by another store instance",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id="agent:main:telegram:dm:chat-1",
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Resolve delegated record after external write",
            executor_session_id="proc-refresh-1",
            tmux_session=None,
            repo_path="/repo/demo",
            worktree_path="/repo/demo",
            proof="delegated_handoff:test",
        )
    )

    resolution = reader.resolve_delegated_signal_candidate(
        work_id="wk-delegated-refresh-1",
        live_only=False,
    )
    assert resolution["status"] == "single_match"
    assert resolution["record"].executor_session_id == "proc-refresh-1"


def test_update_delegated_work_for_process_marks_finished_on_success(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-finish-1",
            title="delegated finish",
            objective="mark delegated work finished when OMX executor process exits cleanly",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Wait for OMX completion",
            executor_session_id="proc-finish-1",
            tmux_session="omx-finish",
            repo_path="/repo/demo",
            worktree_path="/repo/demo",
            proof="delegated_handoff:test",
        )
    )

    updated = GatewayRunner._update_delegated_work_for_process(
        runner,
        executor_session_id="proc-finish-1",
        exit_code=0,
    )

    assert updated is True
    record = work_state_store.resolve_delegated_signal_candidate(
        work_id="wk-delegated-finish-1",
        live_only=False,
    )["record"]
    assert record.state == "finished"
    assert record.proof == "background_process_exit:0"
    assert record.next_action == "Inspect the completed OMX run"


def test_update_delegated_work_for_process_marks_failed_on_nonzero_exit(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-finish-2",
            title="delegated fail",
            objective="mark delegated work failed when OMX executor process exits nonzero",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Wait for OMX completion",
            executor_session_id="proc-finish-2",
            tmux_session="omx-fail",
            repo_path="/repo/demo",
            worktree_path="/repo/demo",
            proof="delegated_handoff:test",
        )
    )

    updated = GatewayRunner._update_delegated_work_for_process(
        runner,
        executor_session_id="proc-finish-2",
        exit_code=23,
    )

    assert updated is True
    record = work_state_store.resolve_delegated_signal_candidate(
        work_id="wk-delegated-finish-2",
        live_only=False,
    )["record"]
    assert record.state == "failed"
    assert record.proof == "background_process_exit:23"
    assert record.next_action == "Inspect the failed OMX run"


def test_finish_direct_work_record_does_not_overwrite_delegated_record_state_or_proof(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-owner-followup-1",
            title="delegated blocked work",
            objective="owner follow-up turn should not overwrite delegated executor state",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="blocked",
            started_at=now,
            last_progress_at=now,
            next_action="Resume the delegated OMX work after blocked signal proof",
            executor_session_id="proc-blocked-1",
            repo_path="/repo/demo",
            worktree_path="/repo/demo",
            proof="clawhip:session.blocked",
        )
    )

    GatewayRunner._finish_direct_work_record(
        runner,
        "wk-delegated-owner-followup-1",
        session_entry.session_key,
        failed=False,
    )

    record = work_state_store.resolve_delegated_signal_candidate(
        work_id="wk-delegated-owner-followup-1",
        live_only=False,
    )["record"]
    assert record.state == "blocked"
    assert record.proof == "clawhip:session.blocked"
    assert record.next_action == "Resume the delegated OMX work after blocked signal proof"


@pytest.mark.asyncio
async def test_delegated_ingress_webhook_route_calls_gateway_runner(tmp_path):
    runner, store, work_state_store = _make_runner(tmp_path)
    source = _make_source()
    session_entry = store.get_or_create_session(source)
    now = datetime.now(timezone.utc)
    work_state_store.upsert(
        WorkRecord(
            work_id="wk-delegated-webhook-1",
            title="delegated webhook work",
            objective="verify delegated ingress webhook route uses gateway runner correlation",
            owner="hermes",
            executor="omx",
            mode="delegated",
            owner_session_id=session_entry.session_key,
            state="running",
            started_at=now,
            last_progress_at=now,
            next_action="Wait for delegated webhook signal",
            executor_session_id="omx-session-webhook",
            tmux_session="omx-webhook",
            repo_path="/repo/webhook",
            worktree_path="/repo/webhook",
            proof="delegated_handoff:test",
        )
    )

    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "0.0.0.0",
                "port": 0,
                "routes": {
                    "delegated-ingress": {
                        "secret": _INSECURE_NO_AUTH,
                        "delegated_ingress": True,
                    }
                },
            },
        )
    )
    adapter.gateway_runner = runner

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/webhooks/delegated-ingress",
            json={
                "owner": "hermes",
                "state": "blocked",
                "normalized_event": "blocked",
                "executor": "omx",
                "executor_session_id": "omx-session-webhook",
                "tmux_session": "omx-webhook",
                "repo_path": "/repo/webhook",
                "worktree_path": "/repo/webhook",
                "next_action": "Wake the owner from delegated ingress webhook",
                "proof": "clawhip:session.blocked",
            },
            headers={"X-Request-ID": "delegated-ingress-001"},
        )
        assert resp.status == 202
        data = await resp.json()
        assert data["status"] == "accepted"
        assert data["verdict"] == "accepted"
        assert data["resolution"] == "single_match"
        assert data["work_id"] == "wk-delegated-webhook-1"

    await asyncio.sleep(0.05)
    capture_adapter = runner.adapters[Platform.TELEGRAM]
    assert len(capture_adapter.events) == 1
    assert "wk-delegated-webhook-1" in capture_adapter.events[0].text
