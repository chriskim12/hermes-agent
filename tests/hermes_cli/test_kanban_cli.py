"""Tests for the kanban CLI surface (hermes_cli.kanban)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_control_surface as kcs
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Workspace flag parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("scratch",              ("scratch", None)),
        ("worktree",              ("worktree", None)),
        ("dir:/tmp/work",         ("dir", "/tmp/work")),
    ],
)
def test_parse_workspace_flag_valid(value, expected):
    assert kc._parse_workspace_flag(value) == expected


def test_parse_workspace_flag_expands_user():
    kind, path = kc._parse_workspace_flag("dir:~/vault")
    assert kind == "dir"
    assert path.endswith("/vault")
    assert not path.startswith("~")


@pytest.mark.parametrize("bad", ["cloud", "dir:", "", "worktree:/x"])
def test_parse_workspace_flag_rejects(bad):
    if not bad:
        # Empty -> defaults; not an error.
        assert kc._parse_workspace_flag(bad) == ("scratch", None)
        return
    with pytest.raises(argparse.ArgumentTypeError):
        kc._parse_workspace_flag(bad)


# ---------------------------------------------------------------------------
# run_slash smoke tests (end-to-end via the same entry both CLI and gateway use)
# ---------------------------------------------------------------------------

def test_run_slash_no_args_shows_usage(kanban_home):
    out = kc.run_slash("")
    assert "kanban" in out.lower()
    assert "create" in out.lower() or "subcommand" in out.lower() or "action" in out.lower()


def test_run_slash_create_and_list(kanban_home):
    out = kc.run_slash("create 'ship feature' --assignee alice")
    assert "Created" in out
    out = kc.run_slash("list")
    assert "ship feature" in out
    assert "alice" in out


def test_run_slash_create_with_parent_and_cascade(kanban_home):
    # Parent then child via --parent
    out1 = kc.run_slash("create 'parent' --assignee alice")
    # Extract the "t_xxxx" id from "Created t_xxxx (ready, ...)"
    import re
    m = re.search(r"(t_[a-f0-9]+)", out1)
    assert m
    p = m.group(1)
    out2 = kc.run_slash(f"create 'child' --assignee bob --parent {p}")
    assert "todo" in out2  # child starts as todo

    # Complete parent; list should promote child to ready
    kc.run_slash(f"complete {p}")
    # Explicit filter: child should now be ready (was todo before complete).
    ready_list = kc.run_slash("list --status ready")
    assert "child" in ready_list


def test_run_slash_show_includes_comments(kanban_home):
    out = kc.run_slash("create 'x'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'source is paywalled'")
    show = kc.run_slash(f"show {tid}")
    assert "source is paywalled" in show


def test_run_slash_block_unblock_cycle(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    # Claim first so block() finds it running
    kc.run_slash(f"claim {tid}")
    assert "Blocked" in kc.run_slash(f"block {tid} 'need decision'")
    assert "Unblocked" in kc.run_slash(f"unblock {tid}")


def test_run_slash_json_output(kanban_home):
    out = kc.run_slash("create 'jsontask' --assignee alice --json")
    payload = json.loads(out)
    assert payload["title"] == "jsontask"
    assert payload["assignee"] == "alice"
    assert payload["status"] == "ready"


def test_run_slash_dispatch_dry_run_counts(kanban_home):
    kc.run_slash("create 'a' --assignee alice")
    kc.run_slash("create 'b' --assignee bob")
    out = kc.run_slash("dispatch --dry-run")
    assert "Spawned:" in out


def test_run_slash_context_output_format(kanban_home):
    out = kc.run_slash("create 'tech spec' --assignee alice --body 'write an RFC'")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    kc.run_slash(f"comment {tid} 'remember to include performance section'")
    ctx = kc.run_slash(f"context {tid}")
    assert "tech spec" in ctx
    assert "write an RFC" in ctx
    assert "performance section" in ctx


def test_run_slash_tenant_filter(kanban_home):
    kc.run_slash("create 'biz-a task' --tenant biz-a --assignee alice")
    kc.run_slash("create 'biz-b task' --tenant biz-b --assignee alice")
    a = kc.run_slash("list --tenant biz-a")
    b = kc.run_slash("list --tenant biz-b")
    assert "biz-a task" in a and "biz-b task" not in a
    assert "biz-b task" in b and "biz-a task" not in b


def _seed_control_surface_tasks():
    with kb.connect() as conn:
        active = kb.create_task(
            conn,
            title="active implementation",
            assignee="yuuka",
            tenant="brain-os",
            public_id="CH-500",
            priority=10,
        )
        kb.claim_task(conn, active, claimer="test-active")

        blocked = kb.create_task(
            conn,
            title="blocked by missing review token=supersecret",
            assignee="yuuka",
            tenant="brain-os",
            public_id="CH-501",
        )
        kb.block_task(conn, blocked, reason="need policy decision")

        stale = kb.create_task(
            conn,
            title="stale worker",
            assignee="omx",
            tenant="brain-os",
            public_id="CH-502",
        )
        kb.claim_task(conn, stale, ttl_seconds=-1, claimer="expired")
        with kb.write_txn(conn):
            row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (stale,)).fetchone()
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE id=?",
                (json.dumps({"state": "stale", "next_action": "recover worker"}), int(row["current_run_id"])),
            )

        failed = kb.create_task(
            conn,
            title="failed worker",
            assignee="omx",
            tenant="brain-os",
            public_id="CH-503",
        )
        kb.claim_task(conn, failed, claimer="failed")
        with kb.write_txn(conn):
            row = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (failed,)).fetchone()
            run_id = int(row["current_run_id"])
            conn.execute(
                "UPDATE task_runs SET status='failed', outcome='gave_up', ended_at=?, "
                "summary='executor failed', error='boom' WHERE id=?",
                (1_700_000_100, run_id),
            )
            conn.execute(
                "UPDATE tasks SET status='blocked', current_run_id=NULL, claim_lock=NULL, "
                "claim_expires=NULL, last_spawn_error='worker failed token=supersecret' WHERE id=?",
                (failed,),
            )

        worker_done = kb.create_task(
            conn,
            title="worker done slice",
            assignee="yuuka",
            tenant="brain-os",
            public_id="CH-504",
            review_phase="worker_done",
        )
        review_ready = kb.create_task(
            conn,
            title="review ready slice",
            assignee="yuuka",
            tenant="brain-os",
            public_id="CH-505",
            review_phase="review_ready",
            closeout_evidence={
                "github": {
                    "pr_url": "https://github.com/chriskim12/hermes-agent/pull/505",
                    "checks_url": "https://github.com/chriskim12/hermes-agent/actions/runs/505",
                }
            },
        )
        closed = kb.create_task(
            conn,
            title="closed slice",
            assignee="yuuka",
            tenant="brain-os",
            public_id="CH-506",
            review_phase="closed",
        )
        kb.complete_task(conn, closed, result="closed with approval")
    return {
        "active": active,
        "blocked": blocked,
        "stale": stale,
        "failed": failed,
        "worker_done": worker_done,
        "review_ready": review_ready,
        "closed": closed,
    }


def test_control_surface_buckets_are_authority_labeled_and_secret_safe(kanban_home):
    ids = _seed_control_surface_tasks()
    with kb.connect() as conn:
        surface = kcs.build_control_surface(conn, tenant="brain-os", now=1_700_001_000)
    payload = surface.to_dict()

    for bucket in kcs.BUCKETS:
        assert payload["queues"][bucket], bucket
    assert ids["active"] in {e["task_id"] for e in payload["queues"]["active"]}
    assert ids["stale"] in {e["task_id"] for e in payload["queues"]["stale"]}
    assert ids["failed"] in {e["task_id"] for e in payload["queues"]["failed"]}
    assert "kanban.tasks" in payload["queues"]["active"][0]["authorities"]
    assert any(
        "github.evidence_link" in entry["authorities"]
        for entry in payload["queues"]["review_ready"]
    )
    text = kcs.format_control_surface(surface)
    assert "authority: projection_only" in text
    assert "token=[REDACTED]" in text
    assert "supersecret" not in text


def test_run_slash_control_surface_json_and_alias(kanban_home):
    _seed_control_surface_tasks()
    out = kc.run_slash("ledger --tenant brain-os --json")
    payload = json.loads(out)
    assert payload["authority"].startswith("projection_only")
    assert payload["counts"]["review_ready"] >= 1
    assert payload["counts"]["closed"] >= 1
    forbidden = "tasks" + "." + "metadata"
    assert forbidden not in json.dumps(payload)


def test_run_slash_usage_error_returns_message(kanban_home):
    # Missing required argument for create
    out = kc.run_slash("create")
    assert "usage" in out.lower() or "error" in out.lower()


def test_run_slash_assign_reassigns(kanban_home):
    out = kc.run_slash("create 'x' --assignee alice")
    import re
    tid = re.search(r"(t_[a-f0-9]+)", out).group(1)
    assert "Assigned" in kc.run_slash(f"assign {tid} bob")
    show = kc.run_slash(f"show {tid}")
    assert "bob" in show


def test_run_slash_link_unlink(kanban_home):
    a = kc.run_slash("create 'a'")
    b = kc.run_slash("create 'b'")
    import re
    ta = re.search(r"(t_[a-f0-9]+)", a).group(1)
    tb = re.search(r"(t_[a-f0-9]+)", b).group(1)
    assert "Linked" in kc.run_slash(f"link {ta} {tb}")
    # After link, b is todo
    show = kc.run_slash(f"show {tb}")
    assert "todo" in show
    assert "Unlinked" in kc.run_slash(f"unlink {ta} {tb}")


# ---------------------------------------------------------------------------
# Integration with the COMMAND_REGISTRY
# ---------------------------------------------------------------------------

def test_kanban_is_resolvable():
    from hermes_cli.commands import resolve_command

    cmd = resolve_command("kanban")
    assert cmd is not None
    assert cmd.name == "kanban"


def test_kanban_bypasses_active_session_guard():
    from hermes_cli.commands import should_bypass_active_session

    assert should_bypass_active_session("kanban")


def test_kanban_in_autocomplete_table():
    from hermes_cli.commands import COMMANDS, SUBCOMMANDS

    assert "/kanban" in COMMANDS
    subs = SUBCOMMANDS.get("/kanban") or []
    assert "create" in subs
    assert "dispatch" in subs


def test_kanban_not_gateway_only():
    # kanban is available in BOTH CLI and gateway surfaces.
    from hermes_cli.commands import COMMAND_REGISTRY

    cmd = next(c for c in COMMAND_REGISTRY if c.name == "kanban")
    assert not cmd.cli_only
    assert not cmd.gateway_only
