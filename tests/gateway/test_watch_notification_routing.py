import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _FakeRegistry:
    def __init__(self, session=None):
        self._session = session

    def get(self, session_id):
        return self._session


def _build_runner(monkeypatch, tmp_path) -> GatewayRunner:
    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text("", encoding="utf-8")

    runner = GatewayRunner(GatewayConfig())
    runner.adapters[Platform.DISCORD] = SimpleNamespace(handle_message=AsyncMock())
    runner.adapters[Platform.TELEGRAM] = SimpleNamespace(handle_message=AsyncMock())
    return runner


def _fallback_event() -> MessageEvent:
    return MessageEvent(
        text="user message",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-current",
            thread_id="thread-current",
            user_id="user-current",
            user_name="current-user",
        ),
        internal=False,
    )


@pytest.mark.asyncio
async def test_watch_notification_routes_to_process_owner_thread(monkeypatch, tmp_path):
    import tools.process_registry as pr_module

    monkeypatch.setattr(
        pr_module,
        "process_registry",
        _FakeRegistry(
            SimpleNamespace(
                watcher_platform="discord",
                watcher_chat_id="chat-owner",
                watcher_thread_id="thread-owner",
                watcher_user_id="user-owner",
                watcher_user_name="owner-user",
            )
        ),
    )

    runner = _build_runner(monkeypatch, tmp_path)

    await runner._inject_watch_notification(
        "[SYSTEM: Background process proc_owner matched watch pattern \"ERROR\"]",
        {"session_id": "proc_owner", "type": "watch_match"},
        _fallback_event(),
    )

    discord_adapter = runner.adapters[Platform.DISCORD]
    telegram_adapter = runner.adapters[Platform.TELEGRAM]

    assert discord_adapter.handle_message.await_count == 1
    assert telegram_adapter.handle_message.await_count == 0

    event = discord_adapter.handle_message.await_args.args[0]
    assert event.source.platform == Platform.DISCORD
    assert event.source.chat_id == "chat-owner"
    assert event.source.thread_id == "thread-owner"
    assert event.source.user_id == "user-owner"
    assert event.source.user_name == "owner-user"


@pytest.mark.asyncio
async def test_watch_notification_skips_when_owner_missing(monkeypatch, tmp_path):
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(None))

    runner = _build_runner(monkeypatch, tmp_path)
    fallback_event = _fallback_event()

    await runner._inject_watch_notification(
        "[SYSTEM: Background process proc_missing matched watch pattern \"ERROR\"]",
        {"session_id": "proc_missing", "type": "watch_match"},
        fallback_event,
    )

    discord_adapter = runner.adapters[Platform.DISCORD]
    telegram_adapter = runner.adapters[Platform.TELEGRAM]

    assert discord_adapter.handle_message.await_count == 0
    assert telegram_adapter.handle_message.await_count == 0
