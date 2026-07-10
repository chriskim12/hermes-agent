"""Tests for DiscordAdapter.rename_chat."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.mark.asyncio
async def test_rename_chat_uses_thread_id_metadata():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    channel = SimpleNamespace(edit=AsyncMock())
    client = MagicMock()
    client.get_channel.return_value = channel
    adapter._client = client

    result = await adapter.rename_chat("111", "New Thread Name", metadata={"thread_id": "999"})

    assert result.success is True
    client.get_channel.assert_called_once_with(999)
    channel.edit.assert_awaited_once_with(name="New Thread Name")
    assert result.raw_response == {"channel_id": "999", "name": "New Thread Name"}


@pytest.mark.asyncio
async def test_rename_chat_fetches_when_not_cached():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    channel = SimpleNamespace(edit=AsyncMock())
    client = MagicMock()
    client.get_channel.return_value = None
    client.fetch_channel = AsyncMock(return_value=channel)
    adapter._client = client

    result = await adapter.rename_chat("111", "New Channel Name")

    assert result.success is True
    client.fetch_channel.assert_awaited_once_with(111)
    channel.edit.assert_awaited_once_with(name="New Channel Name")


@pytest.mark.asyncio
async def test_rename_chat_surfaces_permission_error():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    channel = SimpleNamespace(edit=AsyncMock(side_effect=RuntimeError("Missing Permissions")))
    client = MagicMock()
    client.get_channel.return_value = channel
    adapter._client = client

    result = await adapter.rename_chat("111", "Blocked")

    assert result.success is False
    assert "Missing Permissions" in (result.error or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "bad\nname", "x" * 101, None])
async def test_rename_chat_rejects_invalid_names_without_api_call(name):
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    client = MagicMock()
    adapter._client = client

    result = await adapter.rename_chat("111", name)

    assert result.success is False
    client.get_channel.assert_not_called()
