"""Vendored upstream InterviewState subset for Hermes gateway-mode /ouro-intake.

Copied/adapted from Q00/ouroboros `src/ouroboros/bigbang/interview.py` at the commit recorded in VENDORED_UPSTREAM.md.
Only the data model portion is retained here; provider/runtime question generation is intentionally not included in the gateway wrapper so execution authority cannot leak into Hermes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


MIN_ROUNDS_BEFORE_EARLY_EXIT = 3
DEFAULT_INTERVIEW_ROUNDS = 10
MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS = 3500
PROMPT_SAFE_CONTEXT_TRUNCATION_NOTICE = "\n\n[Context truncated for prompt safety.]"
INITIAL_CONTEXT_SUMMARY_QUESTION = (
    "Your saved initial context is too long to safely send to the interview "
    "model without risking CLI prompt failure. Please reply with a concise "
    "summary of the full context, including goals, constraints, and success "
    "criteria. I will use that summary for the next interview question."
)


_TOOLLESS_INTERVIEW_BASE_PROMPT = """## Role Boundaries
- You are only an interviewer.
- Generate exactly one Socratic question that reduces requirements ambiguity.
- Do not explore files, commands, repositories, APIs, tools, or external systems.
- Do not ask to inspect implementation details unless the caller already supplied those details.
- The caller supplies any code or research context in answers.

## Response Format
- Ask one focused question in 1-2 sentences.
- Do not include a preamble.
- End with the question.

## Questioning Strategy
- Target the biggest unresolved decision.
- Prefer scope, non-goal, success criteria, ownership, risk, and verification questions.
- For brownfield work, focus on intent and decisions rather than discovering what exists.
"""


class InterviewPerspective(StrEnum):
    """Internal perspectives used by upstream to keep interviews broad."""

    RESEARCHER = "researcher"
    SIMPLIFIER = "simplifier"
    ARCHITECT = "architect"
    BREADTH_KEEPER = "breadth-keeper"
    SEED_CLOSER = "seed-closer"


class InterviewStatus(StrEnum):
    """Status of the interview process."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABORTED = "aborted"


class InterviewRound(BaseModel):
    """A single round of interview questions and responses."""

    round_number: int = Field(ge=1)
    question: str
    user_response: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InterviewState(BaseModel):
    """Persistent state of an interview session.

    Shape follows upstream Ouroboros. Hermes stores this state as the canonical
    interview ledger and wraps it with Discord origin metadata separately.
    """

    interview_id: str
    status: InterviewStatus = InterviewStatus.IN_PROGRESS
    rounds: list[InterviewRound] = Field(default_factory=list)
    initial_context: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_brownfield: bool = False
    codebase_paths: list[dict[str, str]] = Field(default_factory=list)
    codebase_context: str = ""
    explore_completed: bool = False
    ambiguity_score: float | None = Field(default=None, ge=0.0, le=1.0)
    ambiguity_breakdown: dict[str, Any] | None = None
    completion_candidate_streak: int = Field(default=0, ge=0)
    lateral_review_advised_milestones: list[str] = Field(default_factory=list)

    @property
    def current_round_number(self) -> int:
        return len(self.rounds) + 1

    @property
    def is_complete(self) -> bool:
        return self.status == InterviewStatus.COMPLETED

    @property
    def needs_initial_context_summary(self) -> bool:
        if len(self.initial_context) <= MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS:
            return False
        return not any(
            round_data.question == INITIAL_CONTEXT_SUMMARY_QUESTION
            and bool(round_data.user_response)
            for round_data in self.rounds
        )

    @property
    def can_reopen(self) -> bool:
        return self.is_complete

    def mark_updated(self) -> None:
        self.updated_at = datetime.now(UTC)

    def store_ambiguity(self, *, score: float, breakdown: dict[str, Any]) -> None:
        self.ambiguity_score = score
        self.ambiguity_breakdown = breakdown
        self.mark_updated()


def prompt_safe_initial_context(state: InterviewState) -> str:
    """Return initial context safe for question-generation prompts."""

    if len(state.initial_context) <= MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS:
        return state.initial_context
    for round_data in reversed(state.rounds):
        if round_data.question == INITIAL_CONTEXT_SUMMARY_QUESTION and round_data.user_response:
            return _truncate_prompt_safe_context(round_data.user_response)
    return "[Initial context exceeds the prompt-safe size and no user summary has been recorded yet. Ask the user to provide a concise summary before scoring or generating a seed.]"


def _truncate_prompt_safe_context(context: str) -> str:
    if len(context) <= MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS:
        return context
    content_budget = MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS - len(PROMPT_SAFE_CONTEXT_TRUNCATION_NOTICE)
    if content_budget <= 0:
        return context[:MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS]
    return context[:content_budget] + PROMPT_SAFE_CONTEXT_TRUNCATION_NOTICE


def _next_conversation_round_number(state: InterviewState) -> int:
    return sum(1 for round_data in state.rounds if round_data.question != INITIAL_CONTEXT_SUMMARY_QUESTION) + 1


def _build_ambiguity_snapshot_prompt(state: InterviewState) -> str:
    """Build upstream-style ambiguity prompt context from stored state."""

    if state.ambiguity_score is None:
        return ""
    lines = [
        "## Current Ambiguity Snapshot",
        f"- Overall ambiguity: {state.ambiguity_score:.2f}",
    ]
    breakdown = state.ambiguity_breakdown if isinstance(state.ambiguity_breakdown, dict) else {}
    dimensions = breakdown.get("ambiguity_ledger") or []
    weakest: list[tuple[float, str, str]] = []
    for item in dimensions:
        if not isinstance(item, dict) or item.get("clarity_score") is None:
            continue
        weakest.append((float(item.get("clarity_score") or 0.0), str(item.get("name") or "Unknown"), str(item.get("reason") or item.get("justification") or "")))
    weakest.sort(key=lambda item: item[0])
    for clarity, name, reason in weakest[:2]:
        lines.append(f"- Weakest area: {name} ({clarity:.2f} clarity)")
        if reason:
            lines.append(f"  Reason: {reason}")
    blockers = breakdown.get("blocking_questions") or []
    if blockers:
        lines.append("- Current material gaps:")
        lines.extend(f"  - {question}" for question in blockers[:3])
    lines.append("- Drill into the weakest area with a concrete, scenario-grounded question.")
    return "\n".join(lines)


def _build_perspective_panel_prompt(state: InterviewState) -> str:
    """Build the upstream toolless perspective-panel instructions."""

    perspectives = [InterviewPerspective.BREADTH_KEEPER, InterviewPerspective.RESEARCHER, InterviewPerspective.SIMPLIFIER]
    if _next_conversation_round_number(state) > 2 or state.is_brownfield:
        perspectives.append(InterviewPerspective.ARCHITECT)
    if state.ambiguity_score is not None and state.ambiguity_score <= 0.20:
        perspectives.append(InterviewPerspective.SEED_CLOSER)
    ordered = list(dict.fromkeys(perspectives))
    return "\n".join([
        "## Perspective Panel",
        "Silently check breadth, simplicity, architecture, and closure readiness.",
        "Use those perspectives only to choose one clarifying question.",
        f"Active perspectives: {', '.join(p.value for p in ordered)}",
        "",
        "## Panel Synthesis Rules",
        "- Keep independent ambiguity tracks visible instead of collapsing onto one favorite subtopic.",
        "- Preserve both implementation and written-output requirements when the user asked for both.",
        "- Prefer breadth recap questions when multiple unresolved tracks still exist.",
        "- Only ask a closure question when closure mode is active; otherwise keep drilling into the weakest area.",
        "- Even when the score is seed-ready, do not end the interview on the first low-ambiguity turn.",
    ])


def _vendored_text(relative_path: str) -> tuple[str, str]:
    """Read a vendored upstream interview UX authority document."""

    root = Path(__file__).resolve().parents[1]
    path = root / relative_path
    return str(path), path.read_text(encoding="utf-8")


def _upstream_interview_ux_authority_prompt() -> tuple[str, dict[str, str]]:
    """Return the actual vendored upstream interview skill/prompt assets."""

    skill_path, skill_text = _vendored_text("skills/interview/SKILL.md")
    interviewer_path, interviewer_text = _vendored_text("agents/socratic-interviewer.md")
    closer_path, closer_text = _vendored_text("agents/seed-closer.md")
    prompt = (
        "## Upstream Interview Skill — UX Authority\n"
        "The interview UX must follow this vendored upstream skill contract. Hermes may adapt IO and authority boundaries, but should not invent a separate local interview policy.\n\n"
        f"```markdown\n{skill_text}\n```\n\n"
        "## Upstream Socratic Interviewer\n"
        f"```markdown\n{interviewer_text}\n```\n\n"
        "## Upstream Seed Closer\n"
        f"```markdown\n{closer_text}\n```"
    )
    return prompt, {
        "skill_contract_source": skill_path,
        "socratic_interviewer_source": interviewer_path,
        "seed_closer_source": closer_path,
    }


def build_toolless_question_prompt(state: InterviewState, *, max_chars: int = 60000) -> dict[str, Any]:
    """Build the vendored upstream question-generation contract without calling an LLM.

    This is the safe Hermes gateway equivalent of upstream `InterviewEngine.ask_next_question`'s
    prompt construction path: it preserves the upstream role boundaries, prompt-safe initial
    context, ambiguity snapshot, and perspective panel while leaving the actual provider call
    outside the gateway/no-restart implementation lane.
    """

    effective_round_number = _next_conversation_round_number(state)
    initial_context = prompt_safe_initial_context(state)
    prompt_initial_context = initial_context[:1800] + ("\n\n[Initial context continues in the first user message.]" if len(initial_context) > 1800 else "")
    dynamic_header = (
        "You are an expert requirements engineer conducting a Socratic interview.\n\n"
        + ("CRITICAL: Start your FIRST response with a DIRECT QUESTION about the project. Do NOT introduce yourself. Just ask a specific, clarifying question immediately.\n\n" if effective_round_number == 1 else "")
        + f"This is Round {effective_round_number}. Your ONLY job is to ask questions that reduce ambiguity.\n\n"
        + f"Initial context: {prompt_initial_context}\n"
        + "\n\nAnswer prefixes the caller may use:\n"
        + "- [from-code]: Existing codebase state (factual, read from files).\n"
        + "- [from-user]: Human decisions/judgments.\n"
        + "- [from-research]: Externally researched information (API docs, pricing, compatibility)."
    )
    if state.is_brownfield:
        dynamic_header += (
            "\n\nThis is a BROWNFIELD project. The caller (main session) has direct codebase access "
            "and will enrich answers with code context. Focus your questions on INTENT and DECISIONS, not on discovering what exists."
        )
    ambiguity = _build_ambiguity_snapshot_prompt(state)
    if ambiguity:
        dynamic_header += f"\n\n{ambiguity}"
    upstream_ux_prompt, upstream_sources = _upstream_interview_ux_authority_prompt()
    prompt = f"{dynamic_header}\n{upstream_ux_prompt}\n\n{_TOOLLESS_INTERVIEW_BASE_PROMPT}\n\n{_build_perspective_panel_prompt(state)}"
    if len(prompt) > max_chars:
        prompt = prompt[:max_chars]
    return {
        "source": "vendored_q00_ouroboros_interview_prompt_contract",
        "ux_authority": "vendored_upstream_interview_skill",
        **upstream_sources,
        "round_number": effective_round_number,
        "is_brownfield": state.is_brownfield,
        "system_prompt": prompt,
        "requires_provider_question": True,
    }
