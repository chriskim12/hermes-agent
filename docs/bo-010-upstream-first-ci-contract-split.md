# BO-010 upstream-first CI contract split

BO-010 supersedes the local-copy multi-board PR (#32) with an upstream-first reconciliation PR (#33). After conflict resolution and focused verification, GitHub CI showed the broad `test` workflow failing on a stale local-fork expectation set rather than on the board/upstream reconciliation slice itself.

## Decision

Chris approved the upstream-first path: accept upstream as the base truth first, keep the local delta minimal, and split stale local-only test expectations out of the blocking PR gate instead of reintroducing all legacy fork surfaces into the upstream-first code.

This does **not** delete the stale tests. The blocking `test` job excludes the known local-fork drift files, and this document records the excluded bucket so it can be reconciled deliberately in a follow-up CI-contract/runtime-surface migration card instead of being treated as upstream-sync evidence.

## Failure bucket captured before the split

The failing full test job on PR #33 head `86d1b1d3db057b4da0ca3a1fd2f602e630281048` reported 128 failed / 11 errors. Representative drift classes:

- CLI TUI/output-history helpers expected by fork-local tests but absent after upstream-first reconciliation, such as `cli._configure_output_history`, `_replay_output_history`, `_preserve_ctrl_enter_newline`, and `_confirm_destructive_slash`.
- Gateway owner/delegated ingress, autopilot, generated TTS/voice, and work-state helper methods expected by fork-local tests but absent or reshaped in upstream reality.
- Legacy `CH-*` Linear/autopilot expectations conflicting with Kanban-first `BO-*` routing.
- Local update/service/config exact-output expectations whose asserted strings or helper seams no longer match upstream.

## Governance

- Kanban remains the authority for BO-010.
- GitHub checks are evidence/review surface, not authority.
- PR #32 remains superseded and must not be merged as-is.
- PR #33 merge remains a separate approval gate.
- No gateway restart/reload, secrets/env/BWS mutation, or project data migration is included in this split.
- The legacy-local-fork test bucket should be reconciled in a follow-up CI-contract/runtime-surface migration card, not silently forgotten.
