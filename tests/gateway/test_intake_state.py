"""Tests for gateway intake interview session state."""

from datetime import datetime, timedelta

from gateway.config import GatewayConfig, Platform
from gateway.session import SessionSource, SessionStore


def _discord_source(*, user_id="u1", channel="c1", thread="t1", guild="g1"):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=channel,
        chat_type="channel",
        user_id=user_id,
        user_name="Chris",
        thread_id=thread,
        guild_id=guild,
    )


def test_intake_can_continue_across_messages_in_same_discord_context(tmp_path):
    store = SessionStore(tmp_path, GatewayConfig())
    source = _discord_source()

    started = store.start_intake(
        source,
        project="bo",
        goal="Build a deterministic Discord intake flow",
        tenant="kanban",
        draft={"acceptance_criteria": []},
    )

    active = store.get_active_intake(source)

    assert active is not None
    assert active.intake_id == started.intake_id
    assert active.project == "BO"
    assert active.tenant == "kanban"
    assert active.draft["goal"] == "Build a deterministic Discord intake flow"
    assert active.draft["acceptance_criteria"] == []


def test_unrelated_discord_chat_does_not_attach_to_active_intake(tmp_path):
    store = SessionStore(tmp_path, GatewayConfig())
    store.start_intake(
        _discord_source(user_id="u1", channel="c1", thread="t1"),
        project="dc",
        goal="Draft DailyChingu billing remediation criteria",
    )

    same_channel_other_thread = _discord_source(user_id="u1", channel="c1", thread="t2")
    same_thread_other_user = _discord_source(user_id="u2", channel="c1", thread="t1")
    other_channel = _discord_source(user_id="u1", channel="c2", thread="t1")

    assert store.get_active_intake(same_channel_other_thread) is None
    assert store.get_active_intake(same_thread_other_user) is None
    assert store.get_active_intake(other_channel) is None


def test_intake_state_persists_and_expires_deterministically(tmp_path):
    store = SessionStore(tmp_path, GatewayConfig())
    source = _discord_source()
    started = store.start_intake(
        source,
        project="ws",
        goal="Collect WhyStarve popup change acceptance criteria",
        ttl_minutes=30,
    )

    reloaded = SessionStore(tmp_path, GatewayConfig())
    assert reloaded.get_active_intake(source).intake_id == started.intake_id

    key = reloaded._intake_state_key(source, project="WS", tenant=None)
    reloaded._intake_states[key].expires_at = datetime.now() - timedelta(minutes=1)
    reloaded._save_intake_states()

    expired_reload = SessionStore(tmp_path, GatewayConfig())
    assert expired_reload.get_active_intake(source) is None


def test_multiple_active_intakes_require_explicit_project(tmp_path):
    store = SessionStore(tmp_path, GatewayConfig())
    source = _discord_source()
    bo = store.start_intake(source, project="bo", goal="Brain OS admission")
    rs = store.start_intake(source, project="rs", goal="Risu admission")

    assert store.get_active_intake(source) is None
    assert store.get_active_intake(source, project="bo").intake_id == bo.intake_id
    assert store.get_active_intake(source, project="rs").intake_id == rs.intake_id


def test_reset_session_clears_intakes_for_that_context(tmp_path):
    store = SessionStore(tmp_path, GatewayConfig())
    source = _discord_source()
    entry = store.get_or_create_session(source)
    store.start_intake(source, project="bo", goal="Initial goal")

    store.reset_session(entry.session_key)

    assert store.get_active_intake(source, project="bo") is None


def test_update_and_clear_active_intake(tmp_path):
    store = SessionStore(tmp_path, GatewayConfig())
    source = _discord_source()
    started = store.start_intake(source, project="bo", goal="Initial goal")

    updated = store.update_intake_draft(
        source,
        intake_id=started.intake_id,
        draft_updates={"open_questions": ["Which acceptance criteria matter?"]},
        last_prompt="What does done mean?",
    )

    assert updated is not None
    assert updated.draft["open_questions"] == ["Which acceptance criteria matter?"]
    assert updated.last_prompt == "What does done mean?"

    assert store.clear_intake(source, intake_id=started.intake_id) is True
    assert store.get_active_intake(source) is None
