"""Regression coverage for packaged Kanban closeout imports.

The source checkout can hide missing modules through stale ``__pycache__`` files,
while a wheel/Nix package starts clean.  Keep this import path explicit so
``hermes --help`` cannot fail after packaging.
"""

from __future__ import annotations


def test_kanban_closeout_imports_without_stale_pycache_dependency():
    from hermes_cli import kanban_closeout
    from hermes_cli import kanban_drift_audit

    payload = {
        "result_class": "projection_authority_claim",
        "blocking": True,
        "findings": [
            {
                "result_class": "projection_authority_claim",
                "blocks_flip_closeout": True,
            }
        ],
    }

    blockers = kanban_drift_audit.closeout_blocks_from_audit(payload)
    assert "drift_audit_blocking" in blockers
    assert "drift_audit_projection_authority_claim" in blockers
    assert kanban_closeout.CLOSEOUT_EVIDENCE_SCHEMA == "kanban_closeout_evidence.v1"
