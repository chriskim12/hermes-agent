from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource
from gateway.work_state import WorkRecord, WorkStateStore


class FakeSessionStore:
    def __init__(self, entries):
        self.entries = entries

    def list_sessions(self):
        return list(self.entries)


class FakeAdapter:
    def __init__(self, success: bool = True):
        self.success = success
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata or {},
            }
        )
        return SendResult(success=self.success, error=None if self.success else "send failed")


def _runner(tmp_path, entries, adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._injected_work_state_store = WorkStateStore(tmp_path / "work_state.json")
    runner.session_store = FakeSessionStore(entries)
    runner.adapters = {Platform.DISCORD: adapter}
    return runner


def _owner_entry(session_id="owner-session-1", thread_id="thread-1"):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="parent-channel-1",
        chat_type="thread",
        thread_id=thread_id,
        user_id="owner-user",
        user_name="Chris",
    )
    return SessionEntry(
        session_key="discord:parent-channel-1:thread-1",
        session_id=session_id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        origin=source,
        platform=Platform.DISCORD,
        chat_type="thread",
    )


def _record(work_id="wk-owner-relay", owner_session_id="owner-session-1", repo_path="/repo/app"):
    return WorkRecord(
        work_id=work_id,
        title="Bounded owner relay proof",
        objective="Prove bounded owner-thread relay",
        owner="hermes",
        executor="omx",
        mode="delegated",
        owner_session_id=owner_session_id,
        executor_session_id="omx-owner-relay",
        repo_path=repo_path,
        worktree_path=f"{repo_path}/.worktrees/ch361",
        state="blocked",
        started_at=datetime.now(timezone.utc),
        last_progress_at=datetime.now(timezone.utc),
        next_action="Review the blocked OMX lane and decide whether to continue.",
        proof="clawhip:action-required:ch361",
    )


def _packet(repo_path="/repo/app"):
    return {
        "owner": "hermes",
        "executor": "omx",
        "normalized_event": "action_required",
        "state": "blocked",
        "repo_path": repo_path,
        "next_action": "Continue the owner lane from the blocked OMX signal.",
        "proof": "clawhip:action-required:ch361",
    }


@pytest.mark.asyncio
async def test_owner_ingress_relay_sends_bounded_note_to_resolved_thread(tmp_path):
    adapter = FakeAdapter()
    runner = _runner(tmp_path, [_owner_entry()], adapter)
    runner._injected_work_state_store.upsert(_record())

    verdict = await runner.handle_owner_ingress_packet(_packet())

    assert verdict["status"] == "single_match"
    assert verdict["relayed"] is True
    assert verdict["relay_reason"] == "owner_thread_relayed"
    assert verdict["relay_target"]["session_id"] == "owner-session-1"
    assert adapter.sent == [
        {
            "chat_id": "parent-channel-1",
            "content": adapter.sent[0]["content"],
            "reply_to": None,
            "metadata": {
                "platform": "discord",
                "chat_id": "parent-channel-1",
                "chat_name": None,
                "chat_type": "thread",
                "user_id": "owner-user",
                "user_name": "Chris",
                "thread_id": "thread-1",
                "chat_topic": None,
            },
        }
    ]
    assert "[SYSTEM: Owner ingress signal for wk-owner-relay]" in adapter.sent[0]["content"]
    assert "Next action: Continue the owner lane" in adapter.sent[0]["content"]
    assert "Proof: clawhip:action-required:ch361" in adapter.sent[0]["content"]


@pytest.mark.asyncio
async def test_owner_ingress_missing_match_does_not_send_to_current_or_generic_channel(tmp_path):
    adapter = FakeAdapter()
    runner = _runner(tmp_path, [_owner_entry()], adapter)

    verdict = await runner.handle_owner_ingress_packet(_packet())

    assert verdict["status"] == "missing"
    assert verdict["relayed"] is False
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_owner_ingress_ambiguous_match_rejects_without_thread_fallback(tmp_path):
    adapter = FakeAdapter()
    runner = _runner(tmp_path, [_owner_entry()], adapter)
    store = runner._injected_work_state_store
    store.upsert(_record(work_id="wk-a", repo_path="/repo/app"))
    store.upsert(_record(work_id="wk-b", repo_path="/repo/app"))

    verdict = await runner.handle_owner_ingress_packet(_packet())

    assert verdict["status"] == "ambiguous"
    assert verdict["relayed"] is False
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_owner_ingress_requires_resolved_owner_session_metadata(tmp_path):
    adapter = FakeAdapter()
    runner = _runner(tmp_path, [_owner_entry(session_id="different-session")], adapter)
    runner._injected_work_state_store.upsert(_record(owner_session_id="missing-owner-session"))

    verdict = await runner.handle_owner_ingress_packet(_packet())

    assert verdict["status"] == "single_match"
    assert verdict["relayed"] is False
    assert verdict["relay_reason"] == "owner_session_not_found"
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_owner_ingress_keeps_repo_channel_delivery_out_of_thread_relay(tmp_path):
    adapter = FakeAdapter()
    runner = _runner(tmp_path, [_owner_entry(thread_id="owner-thread-42")], adapter)
    runner._injected_work_state_store.upsert(_record())

    await runner.handle_owner_ingress_packet(
        {
            **_packet(),
            "webhook_session_id": "generic-webhook-session",
            "repo_channel_id": "repo-channel-999",
        }
    )

    assert len(adapter.sent) == 1
    assert adapter.sent[0]["chat_id"] == "parent-channel-1"
    assert adapter.sent[0]["metadata"]["thread_id"] == "owner-thread-42"
    assert adapter.sent[0]["metadata"].get("webhook_session_id") is None
    assert adapter.sent[0]["metadata"].get("repo_channel_id") is None
