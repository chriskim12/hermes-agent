"""Question-first tool execution guard.

This module keeps the policy intentionally narrow and strict: a message
containing a question mark is treated as an answer request, not an action
request. Tool execution is allowed in the same assistant turn only when the
user put a separate command after the final question mark and the model has
already produced visible answer text.
"""

from __future__ import annotations

import re
from typing import Any

QUESTION_MARKS = ("?", "？")

# Narrow Korean/English command tails that Chris may put *after* a question,
# e.g. "가능해? 가능하면 적용해." A command-looking question such as
# "봐줄래?" does not unlock because there is no separate post-question
# command sentence.
_POST_QUESTION_COMMAND_RE = re.compile(
    r"(?:^|[\s,.;:!…])"
    r"(가능하면\s*)?"
    r"(진행|적용|수정|반영|기록|실행|확인|읽|검색|테스트|패치|커밋|보내|만들)"
    r"(?:해|해줘|해주|해라|해봐|해줘요|해주세요|하자|하겠습니다|해도\s*돼|해도\s*됨)?"
)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def has_question_first_lock(user_message: Any) -> bool:
    """Return True when a user turn should enter answer-first mode."""
    message = _text(user_message)
    return any(mark in message for mark in QUESTION_MARKS)


def _tail_after_last_question_mark(message: str) -> str:
    last = max(message.rfind(mark) for mark in QUESTION_MARKS)
    if last < 0:
        return ""
    return message[last + 1 :].strip()


def has_explicit_post_question_command(user_message: Any) -> bool:
    """Return True only for a separate command after the final question mark."""
    message = _text(user_message)
    if not has_question_first_lock(message):
        return False
    tail = _tail_after_last_question_mark(message)
    if not tail:
        return False
    return bool(_POST_QUESTION_COMMAND_RE.search(tail))


def _has_visible_answer(assistant_content: Any) -> bool:
    content = _text(assistant_content).strip()
    if not content:
        return False
    # Strip simple hidden reasoning blocks so a thinking-only preface does not
    # count as answering the user before tool execution.
    content = re.sub(
        r"<(?:think|thinking|reasoning|REASONING_SCRATCHPAD)[^>]*>.*?</(?:think|thinking|reasoning|REASONING_SCRATCHPAD)>",
        "",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    return bool(content)


def should_block_question_first_tool_calls(
    user_message: Any,
    *,
    assistant_content: Any = "",
) -> bool:
    """Return True when tool calls must be blocked for this assistant turn.

    Rules:
    - No question mark: no lock.
    - Question mark without a separate post-question command: tool calls are
      blocked for the turn, even if the model emits text.
    - Question mark plus a separate command: the model must still answer first;
      tool calls are allowed only when the same assistant message has visible
      answer text before/with the tool calls.
    """
    if not has_question_first_lock(user_message):
        return False
    if not has_explicit_post_question_command(user_message):
        return True
    return not _has_visible_answer(assistant_content)


def question_first_block_message(user_message: Any) -> str:
    """Synthetic tool result shown to the model when the lock blocks a call."""
    return (
        "QuestionFirstLock: blocked tool execution because the user's latest "
        "message contains a question mark. Answer the user's question directly "
        "in plain text first. Do not execute tools, mutate files, create cards, "
        "restart services, or perform external actions unless the user gives a "
        "separate explicit command after receiving the answer."
    )
