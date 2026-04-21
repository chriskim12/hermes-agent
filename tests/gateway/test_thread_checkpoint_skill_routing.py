from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_promotes_discord_thread_checkpoint_to_state_recovery_skill():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="thread",
        thread_id="thread-1",
        user_name="Chris",
    )
    event = MessageEvent(
        text="체크포인트",
        message_type=MessageType.TEXT,
        source=source,
    )

    with patch(
        "agent.skill_commands.build_skill_invocation_message",
        return_value="[state recovery skill payload]",
    ) as build_skill_message:
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result == "[state recovery skill payload]"
    build_skill_message.assert_called_once()
    assert build_skill_message.call_args.args[:2] == (
        "/discord-thread-state-recovery",
        "체크포인트",
    )
    runtime_note = build_skill_message.call_args.kwargs.get("runtime_note", "")
    assert "checkpoint" in runtime_note.lower()
    assert "linear" in runtime_note.lower()


@pytest.mark.asyncio
async def test_prepare_inbound_message_text_leaves_non_thread_checkpoint_as_plain_text():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    runner._model = "test-model"
    runner._base_url = ""
    runner._has_setup_skill = lambda: False

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="group",
        user_name="Chris",
    )
    event = MessageEvent(
        text="체크포인트",
        message_type=MessageType.TEXT,
        source=source,
    )

    with patch(
        "agent.skill_commands.build_skill_invocation_message",
        side_effect=AssertionError("natural checkpoint routing should stay thread-scoped"),
    ):
        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result == "체크포인트"
