from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_control_surface as kcs
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_projection_adapters as projections


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _surface(*, tenant: str = "brain-os", now: int = 1_700_000_000):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Ship projection for jane@example.com token=supersecret",
            assignee="owner@example.com",
            tenant=tenant,
            public_id="CH-423",
            priority=5,
        )
        kb.block_task(
            conn,
            task_id,
            reason="Need review from +1 415 555 1234 with api_key=abc123",
        )
        review_id = kb.create_task(
            conn,
            title="Review ready projection",
            tenant=tenant,
            public_id="CH-424",
            review_phase="review_ready",
            closeout_evidence={
                "github": {
                    "pr_url": "https://github.com/acme/repo/pull/1?token=secret",
                    "checks_url": "https://github.com/acme/repo/actions/runs/1",
                }
            },
        )
        kb.complete_task(conn, review_id, result="closed")
        return kcs.build_control_surface(conn, tenant=tenant, now=now)


def test_discord_projection_is_authority_labeled_secret_safe_and_idempotent(kanban_home):
    surface = _surface()

    first = projections.build_discord_summary_projection(surface, channel_ref="discord:#ops")
    retry = projections.build_discord_summary_projection(
        surface,
        channel_ref="discord:#ops",
        existing=[{"id": "message-1", "body": first.body}],
    )

    assert first.action == "create"
    assert retry.action == "noop"
    assert retry.existing_ref == "message-1"
    assert "authority: projection_only" in first.body
    assert "supersecret" not in first.body
    assert "jane@example.com" not in first.body
    assert "owner@example.com" not in first.body
    assert "415 555 1234" not in first.body
    assert "raw log" not in first.body.lower()


def test_retry_updates_existing_projection_key_instead_of_creating_duplicate(kanban_home):
    old_surface = _surface(now=1_700_000_000)
    old = projections.build_github_pr_evidence_projection(old_surface, pr_ref="acme/repo#1")

    with kb.connect() as conn:
        kb.create_task(
            conn,
            title="New failed task",
            tenant="brain-os",
            public_id="CH-425",
            review_phase="worker_done",
        )
        new_surface = kcs.build_control_surface(conn, tenant="brain-os", now=1_700_000_100)

    new = projections.build_github_pr_evidence_projection(
        new_surface,
        pr_ref="acme/repo#1",
        existing=[{"id": "comment-1", "body": old.body}],
    )

    assert new.key == old.key
    assert new.checksum != old.checksum
    assert new.action == "update"
    assert new.existing_ref == "comment-1"
    assert new.body.count("hermes-kanban-projection") == 1
    assert "token=secret" not in new.body
    assert "token=[REDACTED]" in new.body


def test_wiki_projection_block_upsert_replaces_instead_of_appending(kanban_home):
    surface = _surface(now=1_700_000_000)
    first = projections.build_wiki_log_projection(surface, log_ref="ops-log")
    text = projections.upsert_projection_block("# Existing\n", first)

    with kb.connect() as conn:
        kb.create_task(conn, title="Second item", tenant="brain-os", public_id="CH-426")
        changed = kcs.build_control_surface(conn, tenant="brain-os", now=1_700_000_500)

    second = projections.build_wiki_log_projection(changed, log_ref="ops-log")
    updated = projections.upsert_projection_block(text, second)

    assert updated.count('hermes-kanban-projection:start key="') == 1
    assert first.checksum not in updated
    assert second.checksum in updated
    assert "Second item" in updated


def test_linear_projection_can_be_disabled_while_historical_reference_resolves(kanban_home):
    surface = _surface(tenant="brain-os")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Historical Linear card",
            tenant="brain-os",
            public_id="CH-999",
            idempotency_key="linear:CH-999",
        )
        kb.archive_task(conn, task_id)

        projection = projections.build_linear_compatibility_projection(
            surface,
            issue_identifier="CH-999",
            conn=conn,
            enabled=True,
            retired_domains=["brain-os"],
        )

    assert projection.action == "skipped"
    assert projection.body == ""
    assert projection.metadata["disabled"] is True
    historical = projection.metadata["historical_reference"]
    assert historical["status"] == "resolved"
    assert historical["matches"][0]["task_id"] == task_id
    assert historical["matches"][0]["status"] == "archived"


def test_enabled_linear_projection_is_idempotent_and_authority_labeled(kanban_home):
    surface = _surface()
    first = projections.build_linear_compatibility_projection(
        surface,
        issue_identifier="CH-423",
    )
    retry = projections.build_linear_compatibility_projection(
        surface,
        issue_identifier="CH-423",
        existing=[{"id": "linear-comment-1", "body": first.body}],
    )

    assert first.action == "create"
    assert retry.action == "noop"
    assert retry.existing_ref == "linear-comment-1"
    assert "compatibility-only" in first.body
    assert "authority: projection_only" in first.body
