# Tool Result Storage and Summary Thresholds

Source files: `tools/budget_config.py`, `tools/tool_result_storage.py`, `run_agent.py`

This page is the implementation note for how Hermes handles tool output that would otherwise be too large to keep inline in the agent context.

## Goals

Hermes must never silently discard evidence from tool output. Large results can be summarized inline for the model, but the full result must remain accessible by file path so a human or follow-up tool call can inspect it exactly.

The storage path is the escape hatch, not a replacement for evidence.

## The three layers

### 1) Per-tool inline cap

Each tool may truncate its own output before returning it. This is the earliest and cheapest defense.

### 2) Per-result persistence threshold

After a tool returns, `maybe_persist_tool_result()` compares the result length to the tool's resolved threshold.

Current defaults:

| Setting | Value | Meaning |
|---|---:|---|
| `DEFAULT_RESULT_SIZE_CHARS` | `100_000` | Default per-tool result threshold |
| `DEFAULT_TURN_BUDGET_CHARS` | `200_000` | Max total tool-result chars allowed in one assistant turn |
| `DEFAULT_PREVIEW_SIZE_CHARS` | `1_500` | Preview size kept inline after persistence |

Threshold resolution order:

1. pinned thresholds
2. explicit tool overrides in `BudgetConfig`
3. tool registry value for that tool
4. default result size

If a result exceeds the resolved per-tool threshold, Hermes must:

- write the full output to a temp-backed file named `{tool_use_id}.txt`
- store it under `/tmp/hermes-results` by default, or the backend temp dir's `hermes-results/` subdir when available
- replace the in-context content with a persisted-output block

### 3) Per-turn aggregate budget

After all tool results for a single assistant turn are collected, Hermes checks the total size of that turn's tool messages.

If the total exceeds `DEFAULT_TURN_BUDGET_CHARS` (`200_000`), Hermes must spill the largest non-persisted results to disk first until the turn is back under budget.

Already-persisted results are not re-spilled.

## Required in-context metadata

When a result is persisted or summarized, the model-facing message must include enough metadata to locate and interpret the full output:

- tool name
- tool_use_id or equivalent stable identifier
- total size of the original output
- human-readable size label when useful (`KB` / `MB`)
- saved file path
- a preview snippet of the retained output
- a clear instruction to use `read_file` with `offset` and `limit` for the full evidence

For command-style tools, the summary should also preserve:

- exit code / returncode
- the relevant stdout/stderr snippet that explains the result

The exact shape can vary by tool, but these fields must be recoverable from the inline result without guessing.

## Inline vs. summarized vs. saved-path behavior

### Small output: inline

If the result is at or below the per-tool threshold, return it as-is.

### Medium output: summarized + saved path

If the result is above the per-tool threshold and persistence succeeds, return a `<persisted-output>` block containing:

- a short explanation that the result was too large
- the saved path
- a preview snippet
- a reminder to use `read_file`

This is not a lossy replacement. The full artifact must remain on disk.

### Fallback: explicit truncation only when persistence fails

If the agent cannot write the file to sandbox storage, Hermes may fall back to inline truncation, but the truncation must be explicit.

The message must say that the full output could not be saved and must still include the preview. Silent truncation is not allowed.

## Non-negotiable rule

The full tool output must always remain accessible by path/read_file when persistence succeeds.

That means:

- no hidden deletion of the original evidence
- no summary-only replacement when a file path can be written
- no silent clipping of large output
- no dependence on the model remembering the preview

If the file exists, the file is the source of truth.

## Practical rules for implementers

- Use a backend-aware temp directory when available.
- Write the file through the environment execution path so the output is reachable from the active backend.
- Keep the inline preview short enough to fit comfortably in context.
- When the turn budget spills a result, prefer the largest non-persisted outputs first.
- Preserve the full path in the message exactly as written so humans can copy it into `read_file`.

## Acceptance checklist for this policy

- [ ] Small outputs stay inline
- [ ] Oversized outputs are persisted to `/tmp/hermes-results` or the backend temp equivalent
- [ ] The inline replacement includes path + preview + access instructions
- [ ] Mid-sized outputs can be spilled to disk at the turn level
- [ ] No silent truncation path exists
- [ ] Full evidence remains reachable by path/read_file
