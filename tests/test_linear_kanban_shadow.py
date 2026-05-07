import json

from hermes_cli import kanban_db as kb
from hermes_cli import linear_kanban_shadow as bridge


def _issue(**overrides):
    data = {
        "identifier": "CH-409",
        "title": "Implement Linear→Kanban shadow task bridge with idempotency guard",
        "url": "https://linear.app/chriskim12/issue/CH-409/example",
        "issue_id": "lin-uuid-409",
        "state": "In Progress",
        "project": "Brain OS",
        "team": "Chris",
        "assignee": "Yuuka",
        "parent_identifier": "CH-406",
        "updated_at": "2026-05-07T12:00:00.000Z",
        "description": "Shadow bridge slice.",
    }
    data.update(overrides)
    return bridge.LinearIssueSnapshot(**data)


def test_shadow_issue_creates_triage_task_with_linear_idempotency(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    kb.init_db()
    issue = _issue()

    with kb.connect() as conn:
        result = bridge.shadow_issue_to_kanban(conn, issue)
        task = kb.get_task(conn, result["task_id"])

    assert result["created"] is True
    assert result["idempotency_key"] == "linear:CH-409"
    assert result["tenant"] == "brain-os"
    assert result["status"] == "triage"
    assert task is not None
    assert task.assignee is None
    assert task.workspace_kind == "scratch"
    assert task.title.startswith("CH-409 — Implement")
    assert "```json source_payload" in task.body
    payload_text = task.body.split("```json source_payload\n", 1)[1].split("\n```", 1)[0]
    payload = json.loads(payload_text)
    assert payload["identifier"] == "CH-409"
    assert payload["issue_id"] == "lin-uuid-409"
    assert payload["shadow_policy"]["idempotency_key"] == "linear:CH-409"
    assert payload["shadow_policy"]["execution"] == "shadow_only_no_dispatch"


def test_shadow_issue_reuses_existing_non_archived_task(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    kb.init_db()
    issue = _issue()

    with kb.connect() as conn:
        first = bridge.shadow_issue_to_kanban(conn, issue)
        second = bridge.shadow_issue_to_kanban(conn, issue)
        rows = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ?",
            ("linear:CH-409",),
        ).fetchall()

    assert first["created"] is True
    assert second["created"] is False
    assert second["task_id"] == first["task_id"]
    assert len(rows) == 1


def test_shadow_issue_dry_run_does_not_write(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    kb.init_db()
    issue = _issue(project="DailyChingu")

    with kb.connect() as conn:
        result = bridge.shadow_issue_to_kanban(conn, issue, dry_run=True)
        count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]

    assert result["dry_run"] is True
    assert result["task_id"] is None
    assert result["tenant"] == "dailychingu"
    assert count == 0


def test_shadow_issue_blocks_unmapped_project_without_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    kb.init_db()
    issue = _issue(project="Unmapped Project")

    with kb.connect() as conn:
        try:
            bridge.shadow_issue_to_kanban(conn, issue)
        except ValueError as exc:
            assert "unmapped Linear project" in str(exc)
        else:
            raise AssertionError("expected ValueError")


def test_shadow_issue_allows_explicit_tenant_override(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    kb.init_db()
    issue = _issue(project="Unmapped Project")

    with kb.connect() as conn:
        result = bridge.shadow_issue_to_kanban(conn, issue, tenant="hermes", dry_run=True)

    assert result["tenant"] == "hermes"


def test_linear_issue_snapshot_from_graphql_validates_required_fields():
    snapshot = bridge.LinearIssueSnapshot.from_graphql(
        {
            "id": "lin-uuid-409",
            "identifier": "CH-409",
            "title": "Bridge",
            "url": "https://linear.app/example",
            "state": {"name": "Execution Ready"},
            "project": {"name": "Brain OS"},
            "team": {"name": "Chris"},
            "assignee": {"name": "Yuuka"},
            "parent": {"identifier": "CH-406"},
            "updatedAt": "2026-05-07T12:00:00.000Z",
        }
    )

    assert snapshot.identifier == "CH-409"
    assert snapshot.issue_id == "lin-uuid-409"
    assert snapshot.state == "Execution Ready"
    assert snapshot.project == "Brain OS"
    assert snapshot.parent_identifier == "CH-406"


def test_linear_idempotency_key_rejects_invalid_identifier():
    try:
        bridge.linear_idempotency_key("not a card")
    except ValueError as exc:
        assert "invalid Linear identifier" in str(exc)
    else:
        raise AssertionError("expected ValueError")
