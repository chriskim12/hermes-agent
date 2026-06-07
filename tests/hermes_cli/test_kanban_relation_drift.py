from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_relation_drift as krd


@pytest.fixture
def seeded_relation_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kb.connect() as conn:
        for task_id, title, status, assignee in [
            ("p_dep", "Dependency only parent", "running", "alice"),
            ("p_hier", "Hierarchy only parent", "done", "bob"),
            ("p_mix", "Mixed topology parent", "ready", "carol"),
            ("p_done_dep", "Done dependency parent", "done", "alice"),
            ("orphan", "No children", "todo", "dave"),
            ("c_dep_1", "Dependency child 1", "todo", None),
            ("c_dep_2", "Dependency child 2", "ready", None),
            ("c_hier_1", "Hierarchy child 1", "todo", None),
            ("c_hier_2", "Hierarchy child 2", "done", None),
            ("c_mix_dep", "Mixed dependency child", "todo", None),
            ("c_mix_hier", "Mixed hierarchy child", "done", None),
            ("c_done_dep", "Done dependency child", "done", None),
        ]:
            conn.execute(
                "INSERT INTO tasks(id, title, status, assignee, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, title, status, assignee, 1700000000),
            )
        conn.executemany(
            "INSERT INTO task_links(parent_id, child_id, relation_type) VALUES (?, ?, ?)",
            [
                ("p_dep", "c_dep_1", "dependency"),
                ("p_dep", "c_dep_2", "dependency"),
                ("p_hier", "c_hier_1", "hierarchy"),
                ("p_hier", "c_hier_2", "hierarchy"),
                ("p_mix", "c_mix_dep", "dependency"),
                ("p_mix", "c_mix_hier", "hierarchy"),
                ("p_done_dep", "c_done_dep", "dependency"),
            ],
        )
    return home


def test_relation_drift_classifies_dependency_hierarchy_and_mixed(seeded_relation_db):
    report = krd.run_audit()

    dep = {entry.parent_id: entry for entry in report.dependency_only}
    hier = {entry.parent_id: entry for entry in report.hierarchy_only}
    mixed = {entry.parent_id: entry for entry in report.mixed}

    assert dep["p_dep"].classification == "DEPENDENCY_ONLY"
    assert krd.RC_ACTIVE_PARENT_GATING in dep["p_dep"].reason_codes
    assert dep["p_done_dep"].classification == "DEPENDENCY_ONLY"
    assert krd.RC_IDLE_GATING in dep["p_done_dep"].reason_codes
    assert hier["p_hier"].classification == "HIERARCHY_ONLY"
    assert mixed["p_mix"].classification == "MIXED"
    assert krd.RC_ACTIVE_PARENT_GATING in mixed["p_mix"].reason_codes
    assert "orphan" not in dep | hier | mixed


def test_relation_drift_json_roundtrips_and_preserves_counts(seeded_relation_db):
    with kb.connect() as conn:
        before = conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]

    report = krd.run_audit()
    payload = json.loads(krd.format_report_json(report))

    assert payload["summary"]["dependency_only_count"] == 2
    assert payload["summary"]["hierarchy_only_count"] == 1
    assert payload["summary"]["mixed_count"] == 1
    assert payload["summary"]["active_gating_parents"] == 2
    assert payload["total_links"] == 7

    with kb.connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]
    assert before == after