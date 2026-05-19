"""Tests for the admission-only /ouro-intake command surface."""

import json
import re
from types import SimpleNamespace

import pytest

from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from hermes_cli.commands import (
    ACTIVE_SESSION_BYPASS_COMMANDS,
    GATEWAY_KNOWN_COMMANDS,
    is_gateway_known_command,
    resolve_command,
    should_bypass_active_session,
)


def _seed_from_body(body: str) -> dict:
    match = re.search(r"```json seed_contract\n(.*?)\n```", body, re.S)
    assert match, body
    return json.loads(match.group(1))


@pytest.fixture()
def hermes_home(tmp_path):
    token = set_hermes_home_override(tmp_path)
    try:
        yield tmp_path
    finally:
        reset_hermes_home_override(token)


def test_ouro_intake_is_registered_as_gateway_known_command():
    cmd = resolve_command("ouro-intake")

    assert cmd is not None
    assert cmd.name == "ouro-intake"
    assert "ouro-intake" in GATEWAY_KNOWN_COMMANDS
    assert is_gateway_known_command("ouro-intake") is True
    assert should_bypass_active_session("ouro-intake") is True
    assert "ouro-intake" in ACTIVE_SESSION_BYPASS_COMMANDS
    assert resolve_command("ouro_intake").name == "ouro-intake"


def test_help_and_missing_goal_do_not_mutate(hermes_home):
    from gateway.ouro_intake import handle_ouro_intake_command

    help_result = handle_ouro_intake_command("")
    explicit_help = handle_ouro_intake_command("help")

    assert help_result.action == "help"
    assert explicit_help.action == "help"
    assert help_result.mutated is False
    assert help_result.dispatched is False
    assert "admission only" in help_result.message
    assert not (hermes_home / "kanban.db").exists()


def test_creates_blocked_seed_contract_without_dispatch(hermes_home):
    from gateway.ouro_intake import handle_ouro_intake_command
    from hermes_cli import kanban_db as kb

    result = handle_ouro_intake_command(
        'goal:"Design the Discord intake flow" project:bo tenant:kanban context:"seed only"',
        actor="tester",
    )

    assert result.action == "created"
    assert result.mutated is True
    assert result.dispatched is False
    assert result.public_id == "BO-001"
    assert result.task_id
    assert "no worker dispatched" in result.message.lower()

    with kb.connect() as conn:
        task = kb.get_task(conn, result.task_id)
        assert task is not None
        assert task.public_id == "BO-001"
        assert task.status == "blocked"
        assert task.assignee is None
        assert task.claim_lock is None
        assert task.worker_pid is None
        assert task.tenant == "kanban"
        assert task.routing_verdict["status"] == "proposed_only"
        assert task.routing_verdict["verdict"] == "blocked"
        assert task.admission_snapshot is not None
        assert task.body is not None
        assert task.admission_snapshot["executor_dispatch"] == "forbidden_during_admission"
        assert task.closeout_evidence["policy"] == "admission_only_no_execution"
        assert task.admission_snapshot["seed_review_mode"] == "decision_gate_only"
        assert "missing_acceptance_criteria" in task.admission_snapshot["ambiguity_flags"]
        seed = _seed_from_body(task.body)
        assert seed["authority"]["seed_contract_is_source_material_only"] is True
        assert seed["side_effect_boundary"]["executor_dispatch"] == "forbidden_during_admission"
        assert seed["initial_routing"]["status"] == "proposed_only"
        assert seed["seed_review"]["dispatch_allowed"] is False
        assert "What observable proof should make this accepted as Done?" in seed["open_questions"]
        runs = conn.execute(
            "SELECT COUNT(*) AS n FROM task_runs WHERE task_id = ?",
            (result.task_id,),
        ).fetchone()["n"]
        assert runs == 0
        comments = kb.list_comments(conn, result.task_id)
        assert any("Admission-only block" in c.body for c in comments)


def test_sensitive_prod_billing_env_goal_stays_decision_gated(hermes_home):
    from gateway.ouro_intake import handle_ouro_intake_command
    from hermes_cli import kanban_db as kb

    result = handle_ouro_intake_command(
        'goal:"Change production billing env for Paddle checkout" project:dc tenant:billing',
        actor="tester",
    )

    assert result.action == "created"
    assert result.task_id is not None
    with kb.connect() as conn:
        task = kb.get_task(conn, result.task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.assignee is None
        assert task.claim_lock is None
        assert task.worker_pid is None
        assert task.admission_snapshot is not None
        assert "sensitive_side_effect_domain" in task.admission_snapshot["ambiguity_flags"]
        seed = _seed_from_body(task.body or "")
        assert seed["seed_review"]["mode"] == "decision_gate_only"
        assert seed["side_effect_boundary"]["secret_or_env_mutation"] is False
        assert seed["side_effect_boundary"]["prod_or_customer_visible_change"] is False
        assert any("side effects" in q for q in seed["open_questions"])
        runs = conn.execute(
            "SELECT COUNT(*) AS n FROM task_runs WHERE task_id = ?",
            (result.task_id,),
        ).fetchone()["n"]
        assert runs == 0


@pytest.mark.asyncio
async def test_gateway_handler_routes_raw_args_to_controller(monkeypatch):
    from gateway.run import GatewayRunner
    from gateway.platforms.base import MessageEvent
    import gateway.ouro_intake as ouro_intake

    calls = []

    def fake_handle(raw_args, *, actor=None):
        calls.append((raw_args, actor))
        return SimpleNamespace(message="handled by ouro controller")

    monkeypatch.setattr(ouro_intake, "handle_ouro_intake_command", fake_handle)
    runner = object.__new__(GatewayRunner)
    event = MessageEvent(
        text="/ouro-intake goal:test project:bo",
        source=SimpleNamespace(user_name="tester", user_id="u1", chat_id="c1"),
        message_id="m1",
    )

    result = await runner._handle_ouro_intake_command(event)

    assert result == "handled by ouro controller"
    assert calls == [("goal:test project:bo", "tester")]


def test_cli_handler_routes_raw_args_to_controller(monkeypatch):
    import cli as cli_module
    from cli import HermesCLI
    import gateway.ouro_intake as ouro_intake

    calls = []
    printed = []

    def fake_handle(raw_args, *, actor=None):
        calls.append((raw_args, actor))
        return SimpleNamespace(message="cli handled")

    monkeypatch.setattr(ouro_intake, "handle_ouro_intake_command", fake_handle)
    monkeypatch.setattr(cli_module, "_cprint", lambda message: printed.append(message))

    should_continue = object.__new__(HermesCLI).process_command("/ouro-intake goal:test project:bo")

    assert should_continue is True
    assert calls == [("goal:test project:bo", "local-cli")]
    assert printed == ["cli handled"]
