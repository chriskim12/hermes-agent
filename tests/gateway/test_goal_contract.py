import pytest

from gateway.goal_contract import (
    GoalContractError,
    build_goal_contract_from_linear,
    generate_goal_contract,
)


class _FakeLinearGoalClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def fetch_goal_issue(self, identifier: str):
        self.calls.append(identifier)
        return self.payloads.get(identifier)


def _issue(identifier="CH-401", *, state_name="Execution Ready", state_type="unstarted", description=None, parent=None, children=None, comments=None):
    return {
        "status": "ok",
        "identifier": identifier,
        "title": f"{identifier} title",
        "url": f"https://linear.app/chriskim12/issue/{identifier.lower()}",
        "description": description
        if description is not None
        else "## Context\nLive card context\n\n## Done when\n- Implementation complete\n\n## Verification\n- Focused tests pass",
        "state": {"name": state_name, "type": state_type},
        "parent": parent,
        "children": {"nodes": children or []},
        "comments": {"nodes": comments or []},
        "team": {"key": "CH", "name": "Chris"},
    }


def test_single_card_contract_requires_live_lookup_and_core_fields():
    client = _FakeLinearGoalClient({"CH-401": _issue("CH-401")})

    contract = build_goal_contract_from_linear("CH-401", mode="single-card", linear_client=client)

    assert client.calls == ["CH-401"]
    assert contract["status"] == "ok"
    assert contract["target"]["identifier"] == "CH-401"
    assert contract["mode"] == "single-card"
    prompt = contract["prompt"]
    assert prompt.startswith("/goal ")
    for required in [
        "Linear is the SSOT",
        "Target issue: CH-401",
        "Live preflight",
        "Scope",
        "Non-goals",
        "Forbidden side effects",
        "Verification",
        "Closeout rules",
        "Stop conditions",
    ]:
        assert required in prompt
    assert "Do not use stale chat memory or stored /goal_seed as canonical truth" in prompt


def test_contract_includes_parent_children_and_recent_comments_without_using_seed_as_truth():
    issue = _issue(
        "CH-401",
        parent={"identifier": "CH-173", "title": "parent", "state": {"name": "Backlog", "type": "backlog"}},
        children=[_issue("CH-402"), _issue("CH-403", state_name="Done", state_type="completed")],
        comments=[{"body": "/goal_seed stale old prompt", "createdAt": "2026-05-04T00:00:00Z"}],
    )
    contract = generate_goal_contract(issue, mode="single-card")
    prompt = contract["prompt"]

    assert "Parent: CH-173 — parent [Backlog/backlog]" in prompt
    assert "Children snapshot:" in prompt
    assert "CH-402" in prompt
    assert "CH-403" in prompt
    assert "Recent comments snapshot" in prompt
    assert "/goal_seed" in prompt
    assert "Do not use stale chat memory or stored /goal_seed as canonical truth" in prompt


def test_supported_modes_have_explicit_mode_policy():
    issue = _issue("CH-401")

    for mode in ["single-card", "parent-auto-pilot", "shaping", "cleanup", "verification"]:
        contract = generate_goal_contract(issue, mode=mode)
        assert contract["mode"] == mode
        assert f"Mode: {mode}" in contract["prompt"]
        assert "Mode policy:" in contract["prompt"]


@pytest.mark.parametrize(
    "payload, reason",
    [
        (None, "linear_lookup_missing"),
        ({"status": "unavailable", "reason": "LINEAR_API_KEY_missing"}, "LINEAR_API_KEY_missing"),
        ({"status": "ok", "identifier": "CH-401", "title": "missing state"}, "missing_state"),
        ({"status": "ok", "identifier": "CH-401", "state": {"name": "Execution Ready", "type": "unstarted"}}, "missing_title"),
        ({"status": "ok", "title": "missing identifier", "state": {"name": "Execution Ready", "type": "unstarted"}}, "missing_identifier"),
    ],
)
def test_missing_or_ambiguous_live_card_fails_closed(payload, reason):
    client = _FakeLinearGoalClient({"CH-401": payload})

    with pytest.raises(GoalContractError) as exc:
        build_goal_contract_from_linear("CH-401", mode="single-card", linear_client=client)

    assert exc.value.reason == reason


def test_invalid_mode_fails_closed():
    with pytest.raises(GoalContractError) as exc:
        generate_goal_contract(_issue("CH-401"), mode="freeform")

    assert exc.value.reason == "unsupported_mode"


def test_identifier_mismatch_fails_closed():
    client = _FakeLinearGoalClient({"CH-401": _issue("CH-402")})

    with pytest.raises(GoalContractError) as exc:
        build_goal_contract_from_linear("CH-401", mode="single-card", linear_client=client)

    assert exc.value.reason == "identifier_mismatch"


def test_generated_prompt_redacts_extended_bearer_token_charset():
    token = "abc" + "/def" + "+ghi" + "=~jkl.more_token-value"
    issue = _issue(
        "CH-401",
        description="token sample: " + "Bearer " + token + " should not leak",
    )

    prompt = generate_goal_contract(issue, mode="single-card")["prompt"]

    assert "Bearer " not in prompt
    assert token not in prompt
    assert "[REDACTED]" in prompt


def test_generated_prompt_has_no_secret_placeholders_or_bearer_values():
    env_assignment = "LINEAR_API" + "_KEY=" + "example-secret-value"
    issue = _issue(
        "CH-401",
        description="Use " + env_assignment + " but never expose value. Bearer should not appear.",
    )

    prompt = generate_goal_contract(issue, mode="single-card")["prompt"]

    assert "Bearer " not in prompt
    assert env_assignment not in prompt
    assert "[REDACTED]" in prompt
