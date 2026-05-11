# BO-005 Linear legacy compatibility skill boundary

Authority: Kanban `BO-005` (`t_3ab01b0b`). Linear is compatibility/reference only for this work item.

## Patched skill

The patched runtime skill is:

- skill name: `linear-task-execution-closeout`
- file: `~/.hermes/skills/autonomous-ai-agents/linear-task-execution-closeout/SKILL.md`
- version verified during BO-005: `1.0.1`
- patch authorization: Chris approved continuing after the pinned-skill blocker; the skill was unpinned through `hermes curator unpin linear-task-execution-closeout` before using `skill_manage`

## Compatibility rule added

`linear-task-execution-closeout` is now explicitly a **Linear legacy compatibility helper**, not the default authority for new work.

Before using the skill, the agent must classify live work authority:

1. Kanban-authority work loads `kanban-native-work-execution` and keeps Linear/`CH-*` only as `legacy_ref` or projection.
2. Existing Linear legacy-authority work may still use `linear-task-execution-closeout` for Linear state/comment closeout.
3. Ambiguous authority fails closed before mutation.

The patched skill also updates its required loop and closeout checklist so Linear card state updates are only required when Linear is the actual legacy authority. For Kanban-authority work, Kanban closeout evidence is the required authority surface.

## Guard preserved

The patch does **not** delete Linear legacy support. It narrows that support so it cannot silently become the authority for Kanban-native work.

Projection surfaces remain bounded:

- Linear comments/states are compatibility/projection unless the item is explicitly a Linear legacy-authority task.
- GitHub PRs and CI are evidence.
- Discord/wiki/dashboard summaries are projections.
- Kanban task rows, comments/events, and closeout evidence remain the authority for Kanban work.

## BO-005 validation performed

- `hermes curator unpin linear-task-execution-closeout` after Chris approval.
- `skill_manage` patch of `linear-task-execution-closeout` from `1.0.0` to `1.0.1`.
- Added Kanban-first compatibility boundary to the skill.
- Updated the skill's required loop and closeout checklist to avoid Linear authority promotion.
- Loaded the patched skill with `skill_view(name="linear-task-execution-closeout")` and verified readiness.
- No Linear card was created or promoted to authority.
- No gateway restart/reload, secrets/env/BWS mutation, merge, or production action was performed.
