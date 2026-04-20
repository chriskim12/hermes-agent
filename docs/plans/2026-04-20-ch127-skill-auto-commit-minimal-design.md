# CH-127 session-authored skill auto-commit — minimal design contract

> For Hermes: this is the narrow design/spec slice for CH-128. Lock the contract first, then implement against it.

**Goal:** Keep `~/.hermes` git-legible by auto-committing only the skill paths authored by the current Hermes session, without relocating skills or sweeping unrelated dirtiness into the commit.

**Architecture:** Skill writes stay in the existing canonical path (`~/.hermes/skills`). Successful `skill_manage` calls emit explicit touched paths. The active `AIAgent` instance accumulates those paths across the session, then performs one fail-closed git checkpoint at the actual session boundary (`shutdown_memory_provider()` / cached-agent finalization) if auto-commit mode is enabled.

**Tech Stack:** Python, existing Hermes config loader, `skill_manage` tool results, `AIAgent`, git CLI via `subprocess`.

---

## 1. Scope

This slice defines the smallest contract needed before implementation.

### In scope
- how successful `skill_manage` writes expose exact touched paths
- where session-scoped path accumulation lives
- when the batched auto-commit is attempted
- what git states force a fail-closed skip
- how to guarantee unrelated root dirtiness is not pulled into the commit

### Explicit non-goals
- moving skills out of `~/.hermes/skills`
- changing skill discovery/loading order
- auto-committing non-skill files
- per-edit commit spam
- best-effort commits during crash recovery or forced process death

---

## 2. Chosen touchpoints

### 2.1 Write path truth
The write truth for skill mutations is `tools/skill_manager_tool.py`.

Reason:
- it already knows which action succeeded
- it already knows the exact target file or skill directory being mutated
- it is the narrowest place to surface canonical touched paths without inferring from git diff

### 2.2 Session-scoped accumulation
The session-owned accumulator lives on `AIAgent`.

Reason:
- gateway cached agents persist across messages for one session
- CLI keeps one agent alive across the interactive session
- this preserves a real session-batched boundary instead of committing per tool call

### 2.3 Actual commit boundary
The first implementation target is **session end**, not per-turn.

Trigger boundary:
- `AIAgent.shutdown_memory_provider()`

Reason:
- code already documents this as an actual session boundary
- gateway reset/expiry/finalize and CLI exit already route through it
- it is narrow enough to avoid per-edit noise while still being explicit

---

## 3. Result contract from `skill_manage`

Every successful mutating `skill_manage` action must return a `touched_paths` field.

### Required shape
```json
{
  "success": true,
  "message": "...",
  "touched_paths": ["/absolute/path/one", "/absolute/path/two"]
}
```

### Action-specific rules
- `create` -> newly written `SKILL.md`
- `edit` -> target `SKILL.md`
- `patch` -> patched file path (`SKILL.md` or supporting file)
- `write_file` -> written supporting file path
- `remove_file` -> removed supporting file path
- `delete` -> deleted skill directory path (directory-level path is acceptable for `git add -A -- <dir>` semantics)

### Path rules
- absolute paths only
- must resolve under `get_hermes_home() / "skills"`
- duplicates collapsed before accumulation

---

## 4. Session accumulation contract

`AIAgent` keeps a session-local set of skill paths touched by successful `skill_manage` calls.

### Required properties
- set semantics (dedupe automatically)
- persisted only in memory on the live agent instance
- reset after a successful commit attempt or explicit skip finalization
- no accumulation from non-skill tools
- the implementation must capture **pre-tool dirty state** for predicted skill targets before the write runs, so a touched path that was already dirty/staged before this session becomes a fail-closed skip rather than a misleading auto-commit target

### Data shape
```python
self._session_skill_touched_paths: set[str]
self._last_skill_autocommit_result: dict | None
```

---

## 5. Auto-commit config contract

Config lives under the existing `skills` section.

### Minimal config
```yaml
skills:
  external_dirs: []
  auto_commit:
    mode: off
```

### Supported initial modes
- `off` — default
- `session_end` — attempt one batched commit at actual session finalization

No additional modes are required for v1.

---

## 6. Fail-closed git guard contract

Auto-commit must skip instead of guessing when git state is unsafe.

### Hard skip conditions
- `~/.hermes` is not a git repo
- repo root cannot be resolved
- session touched path escapes repo root or `skills/`
- any predicted touched skill path was already dirty/staged before the current session's write hit it
- `index.lock` exists
- merge / rebase / cherry-pick / revert operation in progress
- unresolved conflicts exist
- pre-existing staged changes exist outside the touched-path allowlist
- stage result would introduce paths outside the touched-path allowlist

### Soft no-op conditions
- mode is `off`
- no touched paths were accumulated
- allowed touched paths resolve to no staged changes by session end

### Required behavior
On skip/no-op, record a structured result in memory/logs and do **not** create a commit.

---

## 7. Anti-sweep guarantee

The implementation must never commit the whole dirty tree.

### Required staging rule
Only stage the accumulated allowlist:
```bash
git add -A -- <touched path 1> <touched path 2> ...
```

### Required validation
Compare staged paths before and after staging:
- if pre-existing staged paths outside allowlist exist -> skip
- if newly staged paths outside allowlist appear -> skip and unstage the newly added batch before returning

This is the core protection against swallowing unrelated `~/.hermes` dirtiness.

---

## 8. Commit message contract

The initial message should be stable and boring.

### Default message
```text
chore(skills): checkpoint session-authored skill updates
```

Optional suffixes like touched skill count are acceptable, but not required for v1.

---

## 9. Minimal implementation plan

### Step 1
Add `touched_paths` to successful `skill_manage` mutation results in `tools/skill_manager_tool.py`.

### Step 2
Add `skills.auto_commit.mode` default config in `hermes_cli/config.py`.

### Step 3
Add session path accumulation + finalization hook in `run_agent.py`.

### Step 4
Implement a narrow git helper module for:
- repo root resolution
- allowlist normalization
- unsafe-state checks
- path-scoped staging
- commit / skip result reporting

### Step 5
Add tests proving:
- touched paths are returned correctly
- unrelated dirtiness is not committed
- unsafe git states skip fail-closed
- session-end commit path works

---

## 10. Verification slice for CH-127

CH-127 implementation is acceptable only if all of these pass:

1. a session that mutates skills produces exactly one commit at session end
2. commit includes only touched skill paths
3. unrelated dirty files remain uncommitted
4. unsafe git state yields skip/no commit
5. skill loading still resolves from `~/.hermes/skills` unchanged

---

## 11. Why this boundary is the right minimum

This solves the actual pain without changing the skill model:
- skills stay where they already load from
- the repo stops re-dirtying indefinitely from uncommitted session-authored skill drift
- preserve/reconcile work is no longer mixed with every new skill write
- v1 stays small enough to test honestly
