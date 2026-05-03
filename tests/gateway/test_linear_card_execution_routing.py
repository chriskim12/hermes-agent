"""Regression tests for Discord thread Linear card execution ingress routing."""

from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


def _make_source(*, chat_type: str = "thread", thread_id: str | None = "thread-1") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=thread_id or "channel-1",
        chat_name="Guild / #ops / CH thread" if thread_id else "Guild / #ops",
        chat_type=chat_type,
        user_id="user-1",
        user_name="Alice",
        thread_id=thread_id,
    )


def _make_skill(skills_dir, name: str, body: str = "Handle Linear task routing.") -> None:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""\
---
name: {name}
description: Test {name}.
---

# {name}

{body}
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_discord_thread_linear_execution_cue_preloads_router_before_sender_prefix(tmp_path):
    from agent.skill_commands import scan_skill_commands

    runner = _make_runner()
    source = _make_source()
    event = MessageEvent(text="CH-123 진행", source=source)

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        _make_skill(tmp_path, "linear-task-operator")
        scan_skill_commands()

        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "linear-task-operator" in result
    assert "Linear card execution ingress for CH-123" in result
    assert "live Linear card preflight" in result
    assert "omx-card-execution-routing" in result
    assert not result.startswith("[Alice]")


@pytest.mark.asyncio
async def test_discord_thread_simple_linear_mention_does_not_route(tmp_path):
    from agent.skill_commands import scan_skill_commands

    runner = _make_runner()
    source = _make_source()
    event = MessageEvent(text="CH-123은 incident anchor였어", source=source)

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        _make_skill(tmp_path, "linear-task-operator")
        scan_skill_commands()

        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result == "[Alice] CH-123은 incident anchor였어"


@pytest.mark.asyncio
async def test_discord_thread_linear_execution_cue_fails_closed_when_router_missing(tmp_path):
    from agent.skill_commands import scan_skill_commands

    runner = _make_runner()
    source = _make_source()
    event = MessageEvent(text="CH-123 계속", source=source)

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        scan_skill_commands()

        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result is not None
    assert "Fail-closed result: blocked" in result
    assert "Router skill unavailable" in result
    assert "Linear card execution ingress for CH-123" in result
    assert "ordinary conversation" in result
    assert not result.startswith("[Alice]")


@pytest.mark.asyncio
async def test_discord_thread_status_cue_does_not_route(tmp_path):
    from agent.skill_commands import scan_skill_commands

    runner = _make_runner()
    source = _make_source()
    event = MessageEvent(text="CH-123 진행상황", source=source)

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        _make_skill(tmp_path, "linear-task-operator")
        scan_skill_commands()

        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result == "[Alice] CH-123 진행상황"


@pytest.mark.asyncio
async def test_discord_non_thread_linear_execution_cue_does_not_route(tmp_path):
    from agent.skill_commands import scan_skill_commands

    runner = _make_runner()
    source = _make_source(chat_type="group", thread_id=None)
    event = MessageEvent(text="CH-123 진행해", source=source)

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        _make_skill(tmp_path, "linear-task-operator")
        scan_skill_commands()

        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result == "CH-123 진행해"


@pytest.mark.asyncio
async def test_discord_thread_checkpoint_followup_still_uses_sender_prefix(tmp_path):
    from agent.skill_commands import scan_skill_commands

    runner = _make_runner()
    source = _make_source()
    event = MessageEvent(text="checkpoint follow-up: keep going", source=source)

    with patch("tools.skills_tool.SKILLS_DIR", tmp_path):
        _make_skill(tmp_path, "linear-task-operator")
        scan_skill_commands()

        result = await runner._prepare_inbound_message_text(
            event=event,
            source=source,
            history=[],
        )

    assert result == "[Alice] checkpoint follow-up: keep going"
