# BO-006 Kanban-first routing language migration

Authority: Kanban `BO-006` (`t_e972e1ba`). Linear/`CH-*` is legacy compatibility/reference only for this work item.

## Scope

BO-006 updates human/operator-facing routing and closeout defaults so GitHub, OMX, and AUTOPILOT surfaces cannot silently become task authority for new work.

Patched runtime skills:

- `omx-card-execution-routing` `1.2.2` -> `1.2.3`
- `github-pr-workflow` `1.1.0` -> `1.1.1`
- `autopilot-pr-closeout-protocol` `1.0.0` -> `1.0.1`

## Routing verdict

```text
Routing verdict: Hermes direct
Reason: BO-006 is a bounded language/guard migration across GitHub, OMX, and AUTOPILOT skills so Kanban authority is checked before routing or PR closeout.
Boundary: review=Chris, push/PR=allowed, merge=separate approval unless explicitly approved, gateway restart/reload=forbidden, secrets/env/BWS mutation=forbidden unless explicitly approved
Next gate: patch routing/closeout skill language, verify loadability/static checks, open PR, and persist Kanban closeout evidence
```

## Guard outcomes

- Kanban is now named as default authority for new work in `omx-card-execution-routing`.
- OMX session state, GitHub PRs/checks, Discord/wiki/dashboard summaries, and Linear comments are documented as evidence/projection unless explicitly declared authority for a legacy lane.
- Projection mismatch is fail-closed: reconcile Kanban first rather than promoting a projection surface.
- `github-pr-workflow` now instructs agents to persist PR URL, head SHA, CI rollup, mergeability, and cleanup proof back to Kanban for Kanban-authority work.
- `autopilot-pr-closeout-protocol` now describes Kanban review-ready closeout as the default and keeps Linear Done mutation as legacy compatibility only when Linear is authority.

## Non-actions

- No merge performed.
- No gateway restart/reload performed.
- No secrets/env/BWS mutation performed.
- No production/customer/billing action performed.
- Existing Linear/CH references remain compatibility evidence only; they were not promoted to authority.

## Verification target

This repo commit is an evidence ledger for the external skill patches. The actual skill patch evidence is the loaded skill content plus the Kanban closeout record for `BO-006`.
