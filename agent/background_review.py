"""Background memory/skill review — fork the agent to evaluate the turn.

After every turn, ``AIAgent.run_conversation`` may call
:func:`spawn_background_review` to fire off a daemon thread that replays
the conversation snapshot in a forked :class:`AIAgent` and asks itself
"should any skill/memory be saved or updated?".  Writes go straight to
the memory + skill stores.  Main conversation and prompt cache are never
touched.

The fork inherits the parent's live runtime (provider, model, base_url,
credentials, cached system prompt) so it hits the same prefix cache and
uses the same auth.  It runs with a tool whitelist limited to memory and
skill management tools; everything else is denied at runtime.

See the ``hermes-agent-dev`` skill (``references/self-improvement-loop.md``)
for invariants and PR review criteria.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough

logger = logging.getLogger(__name__)


# Review-prompt strings — used by ``spawn_background_review_thread`` to build
# the user-message that the forked review agent receives.  AIAgent exposes
# them as class attributes (``_MEMORY_REVIEW_PROMPT`` etc.) for back-compat;
# the actual text lives here so future edits are one-place.
_MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

_SKILL_REVIEW_PROMPT = (
    "Review the conversation above and update the skill library. Be "
    "ACTIVE — most sessions produce at least one skill update, even if "
    "small. A pass that does nothing is a missed learning opportunity, "
    "not a neutral outcome.\n\n"
    "Target shape of the library: CLASS-LEVEL skills, each with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries. This "
    "shapes HOW you update, not WHETHER you update.\n\n"
    "Signals to look for (any one of these warrants action):\n"
    "  • User corrected your style, tone, format, legibility, or "
    "verbosity. Frustration signals like 'stop doing X', 'this is too "
    "verbose', 'don't format like this', 'why are you explaining', "
    "'just give me the answer', 'you always do Y and I hate it', or an "
    "explicit 'remember this' are FIRST-CLASS skill signals, not just "
    "memory signals. Update the relevant skill(s) to embed the "
    "preference so the next session starts already knowing.\n"
    "  • User corrected your workflow, approach, or sequence of steps. "
    "Encode the correction as a pitfall or explicit step in the skill "
    "that governs that class of task.\n"
    "  • Non-trivial technique, fix, workaround, debugging path, or "
    "tool-usage pattern emerged that a future session would benefit "
    "from. Capture it.\n"
    "  • A skill that got loaded or consulted this session turned out "
    "to be wrong, missing a step, or outdated. Patch it NOW.\n\n"
    "Preference order — prefer the earliest action that fits, but do "
    "pick one when a signal above fired:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Look back through the "
    "conversation for skills the user loaded via /skill-name or you "
    "read via skill_view. If any of them covers the territory of the "
    "new learning, PATCH that one first. It is the skill that was in "
    "play, so it's the right one to extend.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (via skills_list + skill_view). "
    "If no loaded skill fits but an existing class-level skill does, "
    "patch it. Add a subsection, a pitfall, or broaden a trigger.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella. Skills can be "
    "packaged with three kinds of support files — use the right "
    "directory per kind:\n"
    "     • `references/<topic>.md` — session-specific detail (error "
    "transcripts, reproduction recipes, provider quirks) AND "
    "condensed knowledge banks: quoted research, API docs, external "
    "authoritative excerpts, or domain notes you found while working "
    "on the problem. Write it concise and for the value of the task, "
    "not as a full mirror of upstream docs.\n"
    "     • `templates/<name>.<ext>` — starter files meant to be "
    "copied and modified (boilerplate configs, scaffolding, a "
    "known-good example the agent can `reproduce with modifications`).\n"
    "     • `scripts/<name>.<ext>` — statically re-runnable actions "
    "the skill can invoke directly (verification scripts, fixture "
    "generators, deterministic probes, anything the agent should run "
    "rather than hand-type each time).\n"
    "     Add support files via skill_manage action=write_file with "
    "file_path starting 'references/', 'templates/', or 'scripts/'. "
    "The umbrella's SKILL.md should gain a one-line pointer to any "
    "new support file so future agents know it exists.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA SKILL when no existing "
    "skill covers the class. The name MUST be at the class level. "
    "The name MUST NOT be a specific PR number, error string, feature "
    "codename, library-alone name, or 'fix-X / debug-Y / audit-Z-today' "
    "session artifact. If the proposed name only makes sense for "
    "today's task, it's wrong — fall back to (1), (2), or (3).\n\n"
    "User-preference embedding (important): when the user expressed a "
    "style/format/workflow preference, the update belongs in the "
    "SKILL.md body, not just in memory. Memory captures 'who the user "
    "is and what the current situation and state of your operations "
    "are'; skills capture 'how to do this class of task for this "
    "user'. When they complain about how you handled a task, the "
    "skill that governs that task needs to carry the lesson.\n\n"
    "If you notice two existing skills that overlap, note it in your "
    "reply — the background curator handles consolidation at scale.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • Bundled skills (shipped with Hermes, e.g. 'hermes-agent').\n"
    "  • Hub-installed skills (installed via 'hermes skills install').\n"
    "Pinned skills (marked via 'hermes curator pin') CAN be improved — "
    "pin only blocks deletion/archive/consolidation by the curator, not "
    "content updates. Patch them when a pitfall or missing step turns up, "
    "same as any other agent-created skill.\n"
    "If the only skills that need updating are protected, say\n"
    "'Nothing to save.' and stop.\n\n"
    "Do NOT capture (these become persistent self-imposed constraints "
    "that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "'Nothing to save.' is a real option but should NOT be the "
    "default. If the session ran smoothly with no corrections and "
    "produced no new technique, just say 'Nothing to save.' and stop. "
    "Otherwise, act."
)

_COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and update two things:\n\n"
    "**Memory**: who the user is. Did the user reveal persona, "
    "desires, preferences, personal details, or expectations about "
    "how you should behave? Save facts about the user and durable "
    "preferences with the memory tool.\n\n"
    "**Skills**: how to do this class of task. Be ACTIVE — most "
    "sessions produce at least one skill update. A pass that does "
    "nothing is a missed learning opportunity, not a neutral outcome.\n\n"
    "Target shape of the skill library: CLASS-LEVEL skills with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries.\n\n"
    "Signals that warrant a skill update (any one is enough):\n"
    "  • User corrected your style, tone, format, legibility, "
    "verbosity, or approach. Frustration is a FIRST-CLASS skill "
    "signal, not just a memory signal. 'stop doing X', 'don't format "
    "like this', 'I hate when you Y' — embed the lesson in the skill "
    "that governs that task so the next session starts fixed.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path "
    "emerged.\n"
    "  • A skill that was loaded or consulted turned out wrong, "
    "missing, or outdated — patch it now.\n\n"
    "Preference order for skills — pick the earliest that fits:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were "
    "loaded via /skill-name or skill_view in the conversation. If one "
    "of them covers the learning, PATCH it first. It was in play; "
    "it's the right place.\n"
    "  2. UPDATE AN EXISTING UMBRELLA (skills_list + skill_view to "
    "find the right one). Patch it.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella via "
    "skill_manage action=write_file. Three kinds: "
    "`references/<topic>.md` for session-specific detail OR condensed "
    "knowledge banks (quoted research, API docs excerpts, domain "
    "notes) written concise and task-focused; `templates/<name>.<ext>` "
    "for starter files meant to be copied and modified; "
    "`scripts/<name>.<ext>` for statically re-runnable actions "
    "(verification, fixture generators, probes). Add a one-line "
    "pointer in SKILL.md so future agents find them.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing exists. "
    "Name at the class level — NOT a PR number, error string, "
    "codename, library-alone name, or 'fix-X / debug-Y' session "
    "artifact. If the name only fits today's task, fall back to (1), "
    "(2), or (3).\n\n"
    "User-preference embedding: when the user complains about how "
    "you handled a task, update the skill that governs that task — "
    "memory alone isn't enough. Memory says 'who the user is and "
    "what the current situation and state of your operations are'; "
    "skills say 'how to do this class of task for this user'. Both "
    "should carry user-preference lessons when relevant.\n\n"
    "If you notice overlapping existing skills, mention it — the "
    "background curator handles consolidation.\n\n"
    "Protected skills (DO NOT edit these):\n"
    "  • Bundled skills (shipped with Hermes, e.g. 'hermes-agent').\n"
    "  • Hub-installed skills (installed via 'hermes skills install').\n"
    "Pinned skills (marked via 'hermes curator pin') CAN be improved — "
    "pin only blocks deletion/archive/consolidation by the curator, not "
    "content updates. Patch them when a pitfall or missing step turns up, "
    "same as any other agent-created skill.\n"
    "If the only skills that need updating are protected, say\n"
    "'Nothing to save.' and stop.\n\n"
    "Do NOT capture as skills (these become persistent self-imposed "
    "constraints that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from execute_code'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "Act on whichever of the two dimensions has real signal. If "
    "genuinely nothing stands out on either, say 'Nothing to save.' "
    "and stop — but don't reach for that conclusion as a default."
)



_REVIEW_SECTION_ORDER = (
    "recent_delta",
    "final_response",
    "tool_summary",
    "artifact_changes",
    "history",
)

_REVIEW_ACKNOWLEDGEMENTS = {
    "ok",
    "okay",
    "thanks",
    "thank you",
    "got it",
    "sounds good",
    "yep",
    "yup",
    "done",
}


def _truncate_text(text: str, limit: Optional[int]) -> str:
    if not text or limit is None or limit < 0 or len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: max(0, limit - 1)] + "…"


def _stringify_review_content(content: Any, *, limit: int = 1200) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return _truncate_text(content, limit)
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type")
                if ptype in {"text", "output_text", "input_text"}:
                    text = part.get("text") or part.get("content") or part.get("value") or ""
                    if text:
                        parts.append(str(text))
                    continue
                if ptype in {"image", "image_url", "input_image"}:
                    parts.append(f"[{ptype}]")
                    continue
            parts.append(str(part))
        return _truncate_text("\n".join(parts), limit)
    if isinstance(content, dict):
        return _truncate_text(json.dumps(content, ensure_ascii=False), limit)
    return _truncate_text(str(content), limit)


def _compact_message(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict):
        return None
    role = msg.get("role")
    if role not in {"user", "assistant", "tool"}:
        return None
    compact: Dict[str, Any] = {"role": role}
    for key in ("name", "tool_call_id"):
        value = msg.get(key)
        if value not in (None, ""):
            compact[key] = value
    content = msg.get("content")
    if content not in (None, ""):
        compact["content"] = _stringify_review_content(content)
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        compact["tool_calls"] = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            function_name = function.get("name") if isinstance(function, dict) else None
            compact["tool_calls"].append(
                {
                    "id": tool_call.get("id"),
                    "name": function_name or tool_call.get("name"),
                }
            )
    function_call = msg.get("function_call")
    if isinstance(function_call, dict) and function_call:
        compact["function_call"] = {
            "name": function_call.get("name"),
            "arguments": _truncate_text(str(function_call.get("arguments") or ""), 300),
        }
    return compact


def _compact_summary_item(item: Any) -> Optional[Dict[str, Any]]:
    if isinstance(item, dict):
        compact: Dict[str, Any] = {}
        for key in ("tool", "name", "status", "summary", "message", "error", "tool_call_id", "target", "path", "kind", "source", "excerpt"):
            value = item.get(key)
            if value not in (None, ""):
                trunc_keys = {"summary", "message", "error", "excerpt"}
                compact[key] = _truncate_text(str(value), 600) if key in trunc_keys else value
        if "summary" not in compact:
            fallback = item.get("message") or item.get("error") or item.get("content") or item.get("result")
            if fallback not in (None, ""):
                compact["summary"] = _truncate_text(str(fallback), 600)
        return compact or None
    if item in (None, ""):
        return None
    return {"summary": _truncate_text(str(item), 600)}


def _tool_result_summary(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(msg, dict) or msg.get("role") != "tool":
        return None
    raw_content = msg.get("content")
    parsed: Dict[str, Any] = {}
    if isinstance(raw_content, str):
        try:
            candidate = json.loads(raw_content)
            if isinstance(candidate, dict):
                parsed = candidate
        except (TypeError, json.JSONDecodeError):
            parsed = {}
    elif isinstance(raw_content, dict):
        parsed = raw_content
    tool_name = msg.get("name") or parsed.get("tool") or parsed.get("name") or "tool"
    success = parsed.get("success")
    status = "ok" if success is True else "error" if success is False else "unknown"
    summary = parsed.get("message") or parsed.get("summary") or parsed.get("result")
    if not summary:
        if parsed.get("error"):
            summary = parsed.get("error")
        elif parsed.get("content"):
            summary = parsed.get("content")
        else:
            summary = _stringify_review_content(raw_content, limit=500)
    summary_text = _truncate_text(str(summary), 700)
    if not summary_text:
        return None
    return {
        "tool": tool_name,
        "status": status,
        "summary": summary_text,
        **({"tool_call_id": msg["tool_call_id"]} if msg.get("tool_call_id") else {}),
    }


def _artifact_changes_from_tool(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(msg, dict) or msg.get("role") != "tool":
        return []
    raw_content = msg.get("content")
    if isinstance(raw_content, str):
        try:
            parsed = json.loads(raw_content)
        except (TypeError, json.JSONDecodeError):
            parsed = {}
    elif isinstance(raw_content, dict):
        parsed = raw_content
    else:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    items: List[Dict[str, Any]] = []
    source = msg.get("name") or parsed.get("tool") or parsed.get("name") or "tool"

    def _append_paths(paths: Any, kind: str) -> None:
        if isinstance(paths, str):
            path_items = [paths]
        elif isinstance(paths, list):
            path_items = [p for p in paths if isinstance(p, str) and p]
        else:
            path_items = []
        for path in path_items:
            items.append({"kind": kind, "path": path, "source": source})

    _append_paths(parsed.get("files_modified") or parsed.get("changed_files") or parsed.get("files"), "modified")
    _append_paths(parsed.get("files_created") or parsed.get("created_files") or parsed.get("artifacts"), "created")
    _append_paths(parsed.get("files_deleted") or parsed.get("deleted_files"), "deleted")

    diff = parsed.get("diff") or parsed.get("patch") or parsed.get("combined_diff")
    if isinstance(diff, str) and diff.strip():
        excerpt = "\n".join(diff.splitlines()[:12])
        items.append({
            "kind": "diff",
            "source": source,
            "excerpt": _truncate_text(excerpt, 900),
        })
    return items


def _snapshot_stats(snapshot: Dict[str, Any]) -> Dict[str, int]:
    recent_and_history: List[Dict[str, Any]] = []
    for section in ("recent_delta", "history"):
        for msg in snapshot.get(section) or []:
            if isinstance(msg, dict):
                recent_and_history.append(msg)
    if snapshot.get("final_response"):
        final_msg = snapshot["final_response"]
        if isinstance(final_msg, dict):
            recent_and_history.append(final_msg)
    messages = estimate_messages_tokens_rough(recent_and_history)
    payload = json.dumps(
        {
            "tool_summary": snapshot.get("tool_summary") or [],
            "artifact_changes": snapshot.get("artifact_changes") or [],
            "truncation": snapshot.get("truncation") or {},
        },
        ensure_ascii=False,
    )
    chars = len(json.dumps(snapshot, ensure_ascii=False))
    tokens = messages + estimate_tokens_rough(payload)
    return {"messages": len(recent_and_history) + len(snapshot.get("tool_summary") or []) + len(snapshot.get("artifact_changes") or []), "chars": chars, "tokens": tokens}


def _mark_truncation(truncation: Dict[str, Any], section: str, limit_name: str, *, dropped: int = 0, truncated: bool = False) -> None:
    truncation["truncated"] = True
    if limit_name not in truncation["hit_limits"]:
        truncation["hit_limits"].append(limit_name)
    section_state = truncation["sections"].setdefault(section, {"truncated": False, "dropped": 0})
    section_state["truncated"] = section_state.get("truncated", False) or truncated
    if dropped:
        section_state["dropped"] = section_state.get("dropped", 0) + dropped


def build_review_snapshot(
    messages: Optional[Sequence[Dict[str, Any]]],
    *,
    final_response: Any = None,
    tool_summary: Optional[Sequence[Any]] = None,
    artifact_changes: Optional[Sequence[Any]] = None,
    max_messages: int = 32,
    max_chars: int = 12000,
    max_tokens: int = 3000,
) -> Dict[str, Any]:
    """Build a compact structured snapshot for background review."""
    source_messages = [m for m in (messages or []) if isinstance(m, dict)]

    last_user_idx = None
    last_assistant_idx = None
    for idx, msg in enumerate(source_messages):
        if msg.get("role") == "user":
            last_user_idx = idx
        if msg.get("role") == "assistant" and msg.get("content"):
            last_assistant_idx = idx

    if final_response in (None, ""):
        if last_assistant_idx is not None:
            final_response_entry = _compact_message(source_messages[last_assistant_idx])
        else:
            final_response_entry = None
    elif isinstance(final_response, dict):
        final_response_entry = _compact_message(final_response) or {"role": "assistant", "content": _stringify_review_content(final_response)}
    else:
        final_response_entry = {"role": "assistant", "content": _stringify_review_content(final_response, limit=4000)}

    recent_end = last_assistant_idx if last_assistant_idx is not None else len(source_messages)
    recent_start = last_user_idx if last_user_idx is not None else max(0, recent_end - 4)
    recent_delta = [m for m in (_compact_message(m) for m in source_messages[recent_start:recent_end]) if m and m.get("role") in ("user", "assistant")]
    if not recent_delta and final_response_entry:
        recent_delta = [m for m in (_compact_message(m) for m in source_messages[max(0, recent_end - 2):recent_end]) if m and m.get("role") in ("user", "assistant")]

    derived_tool_summary: List[Dict[str, Any]] = []
    seen_tool_ids = set()
    for msg in source_messages:
        if msg.get("role") != "tool":
            continue
        tool_id = msg.get("tool_call_id")
        if tool_id and tool_id in seen_tool_ids:
            continue
        summary = _tool_result_summary(msg)
        if summary:
            derived_tool_summary.append(summary)
            if tool_id:
                seen_tool_ids.add(tool_id)

    derived_artifact_changes: List[Dict[str, Any]] = []
    seen_artifact_keys = set()
    for msg in source_messages:
        for change in _artifact_changes_from_tool(msg):
            key = (change.get("kind"), change.get("path"), change.get("excerpt"), change.get("source"))
            if key in seen_artifact_keys:
                continue
            seen_artifact_keys.add(key)
            derived_artifact_changes.append(change)

    tool_summary_items = [item for item in (_compact_summary_item(x) for x in (tool_summary or derived_tool_summary)) if item]
    artifact_items = [item for item in (_compact_summary_item(x) for x in (artifact_changes or derived_artifact_changes)) if item]

    history: List[Dict[str, Any]] = []
    for msg in [m for m in (_compact_message(m) for m in source_messages[:recent_start]) if m]:
        if msg.get("role") == "tool":
            continue
        content = (msg.get("content") or "").strip().lower()
        if msg.get("role") == "assistant" and content in _REVIEW_ACKNOWLEDGEMENTS:
            continue
        history.append(msg)

    snapshot: Dict[str, Any] = {
        "recent_delta": recent_delta,
        "final_response": final_response_entry,
        "tool_summary": tool_summary_items,
        "artifact_changes": artifact_items,
        "history": history,
        "truncation": {
            "truncated": False,
            "reason": None,
            "hit_limits": [],
            "limits": {
                "max_messages": max_messages,
                "max_chars": max_chars,
                "max_tokens": max_tokens,
            },
            "used": {},
            "remaining": {},
            "sections": {},
        },
    }

    if not any(snapshot[section] for section in _REVIEW_SECTION_ORDER):
        snapshot["truncation"]["reason"] = "no_review_context"
        return snapshot

    # Preserve the highest-priority sections first, pruning the lowest-priority
    # material until all limits are satisfied.
    for _ in range(1000):
        stats = _snapshot_stats(snapshot)
        snapshot["truncation"]["used"] = stats
        snapshot["truncation"]["remaining"] = {
            "max_messages": max_messages - stats["messages"],
            "max_chars": max_chars - stats["chars"],
            "max_tokens": max_tokens - stats["tokens"],
        }
        if (
            stats["messages"] <= max_messages
            and stats["chars"] <= max_chars
            and stats["tokens"] <= max_tokens
        ):
            break

        if snapshot["history"]:
            snapshot["history"].pop()
            _mark_truncation(snapshot["truncation"], "history", "max_messages", dropped=1)
            continue
        if snapshot["artifact_changes"]:
            snapshot["artifact_changes"].pop()
            _mark_truncation(snapshot["truncation"], "artifact_changes", "max_messages", dropped=1)
            continue
        if snapshot["tool_summary"]:
            snapshot["tool_summary"].pop()
            _mark_truncation(snapshot["truncation"], "tool_summary", "max_messages", dropped=1)
            continue
        if snapshot["final_response"] and isinstance(snapshot["final_response"], dict):
            content = snapshot["final_response"].get("content") or ""
            if len(content) > 64:
                new_len = max(64, len(content) // 2)
                snapshot["final_response"]["content"] = _truncate_text(content, new_len)
                _mark_truncation(snapshot["truncation"], "final_response", "max_chars", truncated=True)
                continue
            if len(content) > 0:
                # Final response content is small (< 64 chars); dropping it
                # saves negligible space. Skip to recent_delta truncation
                # instead of uselessly discarding the last remaining section.
                pass
            else:
                snapshot["final_response"] = None
                _mark_truncation(snapshot["truncation"], "final_response", "max_messages", dropped=1)
                continue
        if snapshot["recent_delta"]:
            last = snapshot["recent_delta"][-1]
            content = last.get("content") or ""
            if len(content) > 64:
                new_len = max(64, len(content) // 2)
                last["content"] = _truncate_text(content, new_len)
                _mark_truncation(snapshot["truncation"], "recent_delta", "max_chars", truncated=True)
                continue
            if len(snapshot["recent_delta"]) > 1:
                snapshot["recent_delta"].pop(0)
                _mark_truncation(snapshot["truncation"], "recent_delta", "max_messages", dropped=1)
                continue
            break
        break

    stats = _snapshot_stats(snapshot)
    snapshot["truncation"]["used"] = stats
    snapshot["truncation"]["remaining"] = {
        "max_messages": max_messages - stats["messages"],
        "max_chars": max_chars - stats["chars"],
        "max_tokens": max_tokens - stats["tokens"],
    }
    if (
        stats["messages"] > max_messages
        or stats["chars"] > max_chars
        or stats["tokens"] > max_tokens
    ):
        _mark_truncation(snapshot["truncation"], "recent_delta", "max_tokens", truncated=True)
        snapshot["truncation"]["reason"] = snapshot["truncation"].get("reason") or "snapshot_too_large"

    return snapshot


def serialize_review_snapshot(snapshot: Dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def summarize_background_review_actions(
    review_messages: List[Dict],
    prior_snapshot: List[Dict],
) -> List[str]:
    """Build the human-facing action summary for a background review pass.

    Walks the review agent's session messages and collects "successful tool
    action" descriptions to surface to the user (e.g. "Memory updated").
    Tool messages already present in ``prior_snapshot`` are skipped so we
    don't re-surface stale results from the prior conversation that the
    review agent inherited via ``conversation_history`` (issue #14944).

    Matching is by ``tool_call_id`` when available, with a content-equality
    fallback for tool messages that lack one.
    """
    existing_tool_call_ids = set()
    existing_tool_contents = set()
    for prior in prior_snapshot or []:
        if not isinstance(prior, dict) or prior.get("role") != "tool":
            continue
        tcid = prior.get("tool_call_id")
        if tcid:
            existing_tool_call_ids.add(tcid)
        else:
            content = prior.get("content")
            if isinstance(content, str):
                existing_tool_contents.add(content)

    actions: List[str] = []
    for msg in review_messages or []:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        tcid = msg.get("tool_call_id")
        if tcid and tcid in existing_tool_call_ids:
            continue
        if not tcid:
            content_str = msg.get("content")
            if isinstance(content_str, str) and content_str in existing_tool_contents:
                continue
        try:
            data = json.loads(msg.get("content", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict) or not data.get("success"):
            continue
        message = data.get("message", "")
        target = data.get("target", "")
        if "created" in message.lower():
            actions.append(message)
        elif "updated" in message.lower():
            actions.append(message)
        elif "added" in message.lower() or (target and "add" in message.lower()):
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
            actions.append(f"{label} updated")
        elif "Entry added" in message:
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
            actions.append(f"{label} updated")
        elif "removed" in message.lower() or "replaced" in message.lower():
            label = "Memory" if target == "memory" else "User profile" if target == "user" else target
            actions.append(f"{label} updated")
    return actions


def build_memory_write_metadata(
    agent: Any,
    *,
    write_origin: Optional[str] = None,
    execution_context: Optional[str] = None,
    task_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build provenance metadata for external memory-provider mirrors."""
    metadata: Dict[str, Any] = {
        "write_origin": write_origin or getattr(agent, "_memory_write_origin", "assistant_tool"),
        "execution_context": (
            execution_context
            or getattr(agent, "_memory_write_context", "foreground")
        ),
        "session_id": agent.session_id or "",
        "parent_session_id": agent._parent_session_id or "",
        "platform": agent.platform or os.environ.get("HERMES_SESSION_SOURCE", "cli"),
        "tool_name": "memory",
    }
    if task_id:
        metadata["task_id"] = task_id
    if tool_call_id:
        metadata["tool_call_id"] = tool_call_id
    return {k: v for k, v in metadata.items() if v not in {None, ""}}


def _run_review_in_thread(
    agent: Any,
    review_snapshot: Dict[str, Any],
    prompt: str,
) -> None:
    """Worker function executed in the background-review daemon thread.

    Spawns a forked ``AIAgent`` inheriting the parent's runtime, runs the
    review prompt, and surfaces a compact action summary back to the user
    via ``agent._safe_print`` and ``agent.background_review_callback``.
    """
    # Local import to avoid a hard circular dep at module load.
    from run_agent import AIAgent
    from tools.terminal_tool import set_approval_callback as _set_approval_callback

    # Install a non-interactive approval callback on this worker
    # thread so any dangerous-command guard the review agent trips
    # resolves to "deny" instead of falling back to input() -- which
    # deadlocks against the parent's prompt_toolkit TUI (#15216).
    # Same pattern as _subagent_auto_deny in tools/delegate_tool.py.
    def _bg_review_auto_deny(command, description, **kwargs):
        logger.warning(
            "Background review auto-denied dangerous command: %s (%s)",
            command, description,
        )
        return "deny"
    try:
        _set_approval_callback(_bg_review_auto_deny)
    except Exception:
        pass

    review_agent = None
    review_messages: List[Dict] = []
    try:
        with open(os.devnull, "w", encoding="utf-8") as _devnull, \
             contextlib.redirect_stdout(_devnull), \
             contextlib.redirect_stderr(_devnull):
            # Inherit the parent agent's live runtime (provider, model,
            # base_url, api_key, api_mode) so the fork uses the exact
            # same credentials the main turn is using.  Without this,
            # AIAgent.__init__ re-runs auto-resolution from env vars,
            # which fails for OAuth-only providers, session-scoped
            # creds, or credential-pool setups where the resolver can't
            # reconstruct auth from scratch -- producing the spurious
            # "No LLM provider configured" warning at end of turn.
            _parent_runtime = agent._current_main_runtime()
            _parent_api_mode = _parent_runtime.get("api_mode") or None
            # The review fork needs to call agent-loop tools (memory,
            # skill_manage). Those tools require Hermes' own dispatch,
            # which the codex_app_server runtime bypasses entirely
            # (it runs the turn inside codex's subprocess). So when
            # the parent is on codex_app_server, downgrade the review
            # fork to codex_responses — same auth/credentials, but
            # talks to the OpenAI Responses API directly so Hermes
            # owns the loop and the agent-loop tools dispatch.
            if _parent_api_mode == "codex_app_server":
                _parent_api_mode = "codex_responses"
            # skip_memory=True keeps the review fork from
            # touching external memory plugins (honcho, mem0,
            # supermemory, etc.).  Without it, the fork's
            # __init__ rebuilds its own _memory_manager from
            # config, scoped to the parent's session_id, and
            # run_conversation() then leaks the harness prompt
            # into the user's real memory namespace via three
            # ingestion sites: on_turn_start (cadence + turn
            # message), prefetch_all (recall query), and
            # sync_all (harness prompt + review output recorded
            # as a (user, assistant) turn pair).  Built-in
            # MEMORY.md / USER.md state is re-bound from the
            # parent below so memory(action="add") writes from
            # the review still land on disk; the review just
            # has zero side effects on external providers.
            # Match parent's toolset config so ``tools[]`` is byte-identical
            # in the request body — Anthropic's cache key includes it.
            # (The runtime whitelist below still restricts dispatch.)
            review_agent = AIAgent(
                model=agent.model,
                max_iterations=16,
                quiet_mode=True,
                platform=agent.platform,
                provider=agent.provider,
                api_mode=_parent_api_mode,
                base_url=_parent_runtime.get("base_url") or None,
                api_key=_parent_runtime.get("api_key") or None,
                credential_pool=getattr(agent, "_credential_pool", None),
                parent_session_id=agent.session_id,
                enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                skip_memory=True,
            )
            review_agent._memory_write_origin = "background_review"
            review_agent._memory_write_context = "background_review"
            review_agent._memory_store = agent._memory_store
            review_agent._memory_enabled = agent._memory_enabled
            review_agent._user_profile_enabled = agent._user_profile_enabled
            review_agent._memory_nudge_interval = 0
            review_agent._skill_nudge_interval = 0
            # Suppress all status/warning emits from the fork so the
            # user only sees the final successful-action summary.
            # Without this, mid-review "Iteration budget exhausted",
            # rate-limit retries, compression warnings, and other
            # lifecycle messages bubble up through _emit_status ->
            # _vprint and leak past the stdout redirect (they go via
            # _print_fn/status_callback, which bypass sys.stdout).
            review_agent.suppress_status_output = True
            # Inherit the parent's cached system prompt verbatim so
            # the review fork's outbound HTTP request hits the same
            # Anthropic/OpenRouter prefix cache the parent warmed.
            # Without this, the fork rebuilds the system prompt from
            # scratch (fresh _hermes_now() timestamp, fresh
            # session_id, narrower toolset → different skills_prompt)
            # and the byte-exact prefix-cache key misses. See
            # issue #25322 and PR #17276 for the full analysis +
            # measured impact (~26% end-to-end cost reduction on
            # Sonnet 4.5).
            review_agent._cached_system_prompt = agent._cached_system_prompt
            # Defensive: pin session_start + session_id to the
            # parent's so any code path that re-renders parts of
            # the system prompt (compression, plugin hooks) still
            # produces byte-identical output. The cached-prompt
            # assignment above already short-circuits the normal
            # rebuild path, but these pins guarantee parity even
            # if a future code path bypasses the cache.
            review_agent.session_start = agent.session_start
            review_agent.session_id = agent.session_id

            from model_tools import get_tool_definitions
            from hermes_cli.plugins import (
                set_thread_tool_whitelist,
                clear_thread_tool_whitelist,
            )

            review_whitelist = {
                t["function"]["name"]
                for t in get_tool_definitions(
                    enabled_toolsets=["memory", "skills"],
                    quiet_mode=True,
                )
            }
            set_thread_tool_whitelist(
                review_whitelist,
                deny_msg_fmt=(
                    "Background review denied non-whitelisted tool: "
                    "{tool_name}. Only memory/skill tools are allowed."
                ),
            )
            try:
                review_agent.run_conversation(
                    user_message=(
                        prompt
                        + "\n\nYou can only call memory and skill "
                        "management tools. Other tools will be denied "
                        "at runtime — do not attempt them.\n\n"
                        + "Structured review snapshot:\n"
                        + serialize_review_snapshot(review_snapshot)
                    ),
                    conversation_history=[],
                )
            finally:
                clear_thread_tool_whitelist()

            # Tear down memory providers while stdout is still
            # redirected so background thread teardown (Honcho flush,
            # Hindsight sync, etc.) stays silent.  The finally block
            # below is a safety net for the exception path.
            try:
                review_agent.shutdown_memory_provider()
            except Exception:
                pass
            try:
                review_agent.close()
            except Exception:
                pass
            review_messages = list(getattr(review_agent, "_session_messages", []))
            review_agent = None

        # Scan the review agent's messages for successful tool actions
        # and surface a compact summary to the user. Tool messages
        # already present in messages_snapshot must be skipped, since
        # the review agent inherits that history and would otherwise
        # re-surface stale "created"/"updated" messages from the prior
        # conversation as if they just happened (issue #14944).
        actions = summarize_background_review_actions(
            review_messages,
            [],
        )

        if actions:
            summary = " · ".join(dict.fromkeys(actions))
            agent._safe_print(
                f"  💾 Self-improvement review: {summary}"
            )
            _bg_cb = agent.background_review_callback
            if _bg_cb:
                try:
                    _bg_cb(
                        f"💾 Self-improvement review: {summary}"
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.warning("Background memory/skill review failed: %s", e)
        agent._emit_auxiliary_failure("background review", e)
    finally:
        # Safety-net cleanup for the exception path.  Normal
        # completion already shut down inside redirect_stdout above.
        # Re-open devnull here so any teardown output (Honcho flush,
        # Hindsight sync, background thread joins) stays silent even
        # on the exception path where redirect_stdout already exited.
        if review_agent is not None:
            try:
                with open(os.devnull, "w", encoding="utf-8") as _fn, \
                     contextlib.redirect_stdout(_fn), \
                     contextlib.redirect_stderr(_fn):
                    try:
                        review_agent.shutdown_memory_provider()
                    except Exception:
                        pass
                    try:
                        review_agent.close()
                    except Exception:
                        pass
            except Exception:
                pass
        # Clear the approval callback on this bg-review thread so a
        # recycled thread-id doesn't inherit a stale reference.
        try:
            _set_approval_callback(None)
        except Exception:
            pass


def spawn_background_review_thread(
    agent: Any,
    messages_snapshot: Optional[Sequence[Dict[str, Any]]] = None,
    review_memory: bool = False,
    review_skills: bool = False,
    review_snapshot: Optional[Dict[str, Any]] = None,
):
    """Build the review thread target and prompt for a background review.

    Returns a ``(target, prompt)`` tuple.  The caller (``AIAgent._spawn_background_review``)
    owns the actual ``threading.Thread`` construction so test-level patches
    of ``run_agent.threading.Thread`` keep working.
    """
    # Pick the right prompt based on which triggers fired.  Allow per-agent
    # override (the prompts moved to module-level constants but old code paths
    # that set agent._MEMORY_REVIEW_PROMPT etc. directly keep working).
    if review_memory and review_skills:
        prompt = getattr(agent, "_COMBINED_REVIEW_PROMPT", _COMBINED_REVIEW_PROMPT)
    elif review_memory:
        prompt = getattr(agent, "_MEMORY_REVIEW_PROMPT", _MEMORY_REVIEW_PROMPT)
    else:
        prompt = getattr(agent, "_SKILL_REVIEW_PROMPT", _SKILL_REVIEW_PROMPT)

    snapshot_payload: Dict[str, Any]
    if review_snapshot is None:
        if isinstance(messages_snapshot, dict):
            snapshot_payload = messages_snapshot
        else:
            snapshot_payload = build_review_snapshot(messages_snapshot or [])
    else:
        snapshot_payload = review_snapshot

    def _target() -> None:
        _run_review_in_thread(agent, snapshot_payload, prompt)

    return _target, prompt


__all__ = [
    "_MEMORY_REVIEW_PROMPT",
    "_SKILL_REVIEW_PROMPT",
    "_COMBINED_REVIEW_PROMPT",
    "build_review_snapshot",
    "serialize_review_snapshot",
    "spawn_background_review_thread",
    "summarize_background_review_actions",
    "build_memory_write_metadata",
]
