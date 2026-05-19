"""Vendored upstream InterviewState subset for Hermes gateway-mode /ouro-intake.

Copied/adapted from Q00/ouroboros `src/ouroboros/bigbang/interview.py` at the commit recorded in VENDORED_UPSTREAM.md.
Only the data model portion is retained here; provider/runtime question generation is intentionally not included in the gateway wrapper so execution authority cannot leak into Hermes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


MAX_PROMPT_SAFE_INITIAL_CONTEXT_CHARS = 3500
INITIAL_CONTEXT_SUMMARY_QUESTION = (
    "Your saved initial context is too long to safely send to the interview "
    "model without risking CLI prompt failure. Please reply with a concise "
    "summary of the full context, including goals, constraints, and success "
    "criteria. I will use that summary for the next interview question."
)


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
