import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore, build_session_key


class _StopAfterRunAgent(RuntimeError):
    pass


def _make_source(chat_id: str = "123", user_id: str = "u1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        user_id=user_id,
        chat_type="dm",
    )


def _make_store(tmp_path):
    config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=config)
    return config, store


def test_mark_interrupted_session_preserves_session_and_sets_resume_flag(tmp_path):
    _config, store = _make_store(tmp_path)
    source = _make_source()

    entry1 = store.get_or_create_session(source)
    original_session_id = entry1.session_id

    marked = store.mark_interrupted_sessions([entry1.session_key], reason="restart")

    assert marked == 1

    entry2 = store.get_or_create_session(source)

    assert entry2.session_id == original_session_id
    assert entry2.was_auto_reset is False
    assert entry2.was_interrupted is True
    assert entry2.interrupted_reason == "restart"
    assert entry2.interrupted is False


def test_recover_interrupted_sessions_from_checkpoint_marks_exact_sessions_only(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    _config, store = _make_store(tmp_path)

    source_a = _make_source(chat_id="a", user_id="ua")
    source_b = _make_source(chat_id="b", user_id="ub")
    entry_a = store.get_or_create_session(source_a)
    _entry_b = store.get_or_create_session(source_b)

    checkpoint_path = tmp_path / ".running_sessions.json"
    checkpoint_path.write_text(
        json.dumps({"session_keys": [entry_a.session_key]}),
        encoding="utf-8",
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = store

    recovered = GatewayRunner._recover_interrupted_sessions_from_checkpoint(runner)

    assert recovered == 1
    assert not checkpoint_path.exists()

    with store._lock:
        store._ensure_loaded_locked()
        assert store._entries[entry_a.session_key].interrupted is True
        other_keys = [k for k in store._entries if k != entry_a.session_key]
        assert len(other_keys) == 1
        assert store._entries[other_keys[0]].interrupted is False


@pytest.mark.asyncio
async def test_handle_message_with_interrupted_session_injects_resume_context_note(tmp_path):
    config, store = _make_store(tmp_path)
    source = _make_source()
    session_key = build_session_key(source)

    entry = store.get_or_create_session(source)
    store.rewrite_transcript(
        entry.session_id,
        [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ],
    )
    store.mark_interrupted_sessions([session_key], reason="restart")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = config
    runner.session_store = store
    runner.adapters = {Platform.TELEGRAM: MagicMock()}
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner._set_session_env = lambda _context: ()
    runner._prepare_inbound_message_text = AsyncMock(return_value="continue")

    captured = {}

    async def fake_run_agent(**kwargs):
        captured.update(kwargs)
        raise _StopAfterRunAgent()

    runner._run_agent = fake_run_agent

    event = MessageEvent(
        text="continue",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    result = await GatewayRunner._handle_message_with_agent(runner, event, source, session_key)

    assert "encountered an error" in result.lower()

    assert "previous turn was interrupted" in captured["context_prompt"]
    assert "Resume the existing conversation context" in captured["context_prompt"]

    resumed = store.get_or_create_session(source)
    assert resumed.session_id == entry.session_id
