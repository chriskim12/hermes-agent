---
name: sdlc-review
description: Review Kanban worker_done candidates before review_ready.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [kanban, review, verifier, autopilot]
---

# SDLC Review

Use this skill when a Hermes Kanban task is in the native `review` column and you are spawned as the reviewer agent.

## Mission

You are not the implementation worker. Independently verify the worker's `worker_done_candidate` before the task can become `review_ready` for human review.

## Required behavior

1. Read the live Kanban card, latest worker run, comments, closeout evidence, and `worker_done_candidate` from the task context.
2. Check each Done criterion against artifact-backed evidence.
3. Verify tests/checks, cleanup/residue proof, authority boundaries, and PR/no-code review package evidence when applicable.
4. Do not merge, deploy, restart, mutate production, or claim human approval.
5. If anything is missing, return `FAIL` with criterion-level remediation so the worker can retry.
6. If the task is unsafe or blocked by authority, return `BLOCKED`.
7. Return `PASS` only when the result is ready to be promoted toward `review_ready`.

## Completion contract

When finished, call Kanban completion with structured metadata using this schema:

```json
{
  "schema": "kanban_reviewer_result.v1",
  "verdict": "PASS | FAIL | BLOCKED | REFINEMENT_REQUIRED",
  "criterion_results": [
    {
      "criterion_id": "...",
      "verdict": "PASS | FAIL | BLOCKED",
      "evidence": "artifact-backed proof or exact gap",
      "artifact_refs": ["path-or-command-or-event-ref"]
    }
  ],
  "remediation_instructions": ["required when verdict is not PASS"]
}
```

The Kanban DB will bind your reviewer run to the latest worker candidate, reject self-approval, requeue the worker on fixable `FAIL`, and block the task on exhausted attempts or authority blockers.
