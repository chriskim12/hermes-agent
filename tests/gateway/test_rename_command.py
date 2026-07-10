"""Tests for the gateway /rename command."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource


def _make_runner(adapter):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    return runner


def _make_event(text="/rename Better Thread Title", *, thread_id="999", chat_id="111"):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type="thread" if thread_id else "group",
        user_id="12345",
        user_name="testuser",
        thread_id=thread_id,
    )
    return MessageEvent(text=text, source=source)


@pytest.mark.asyncio
async def test_rename_command_calls_adapter_with_thread_id():
    adapter = SimpleNamespace(rename_chat=AsyncMock(return_value=SendResult(success=True)))
    runner = _make_runner(adapter)

    result = await runner._handle_rename_command(_make_event())

    adapter.rename_chat.assert_awaited_once_with(
        "111",
        "Better Thread Title",
        metadata={"thread_id": "999"},
    )
    assert "Better Thread Title" in result


@pytest.mark.asyncio
async def test_rename_command_rejects_missing_name():
    adapter = SimpleNamespace(rename_chat=AsyncMock())
    runner = _make_runner(adapter)

    result = await runner._handle_rename_command(_make_event(text="/rename   "))

    adapter.rename_chat.assert_not_called()
    assert "Usage: /rename" in result


@pytest.mark.asyncio
async def test_rename_command_surfaces_adapter_failure():
    adapter = SimpleNamespace(
        rename_chat=AsyncMock(return_value=SendResult(success=False, error="Missing Permissions"))
    )
    runner = _make_runner(adapter)

    result = await runner._handle_rename_command(_make_event())

    assert "Missing Permissions" in result


@pytest.mark.asyncio
async def test_rename_command_handles_adapter_without_capability():
    runner = _make_runner(SimpleNamespace())

    result = await runner._handle_rename_command(_make_event())

    assert "does not support" in result


@pytest.mark.asyncio
async def test_rename_command_contains_unexpected_adapter_exception():
    adapter = SimpleNamespace(rename_chat=AsyncMock(side_effect=RuntimeError("boom")))
    runner = _make_runner(adapter)

    result = await runner._handle_rename_command(_make_event())

    assert "unexpected error" in result


@pytest.mark.asyncio
async def test_rename_command_rejects_invalid_names_before_adapter_call():
    adapter = SimpleNamespace(rename_chat=AsyncMock())
    runner = _make_runner(adapter)

    empty = await runner._handle_rename_command(_make_event(text="/rename \x00\x7f"))
    too_long = await runner._handle_rename_command(_make_event(text=f"/rename {'x' * 101}"))

    assert "empty" in empty
    assert "too long" in too_long
    adapter.rename_chat.assert_not_called()


def test_rename_is_gateway_only_command():
    from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command

    command = resolve_command("rename")
    assert command is not None
    assert command.gateway_only is True
    assert command.args_hint == "<name>"
    assert "rename" in GATEWAY_KNOWN_COMMANDS
