import pytest

from agent.question_first_gate import (
    has_explicit_post_question_command,
    has_question_first_lock,
    should_block_question_first_tool_calls,
)


@pytest.mark.parametrize(
    "message",
    [
        "메모리 반영 이상의 뭔가 액션을할수 없을까?",
        "물음표가 들어가면 실행을 하지 못하게 하면 안돼?",
        "그래서 해결이 되겠냐 이거야?",
        "뭔소리야 rank 0는?",
    ],
)
def test_question_mark_messages_enable_question_first_lock(message):
    assert has_question_first_lock(message) is True
    assert should_block_question_first_tool_calls(message, assistant_content="") is True


def test_plain_command_without_question_does_not_lock_tools():
    message = "SOUL.md에 반영해"

    assert has_question_first_lock(message) is False
    assert should_block_question_first_tool_calls(message, assistant_content="") is False


def test_question_plus_separate_post_question_command_still_requires_text_answer_first():
    message = "가능해? 가능하면 적용해."

    assert has_question_first_lock(message) is True
    assert has_explicit_post_question_command(message) is True
    assert should_block_question_first_tool_calls(message, assistant_content="") is True
    assert should_block_question_first_tool_calls(message, assistant_content="가능합니다.") is False


def test_command_like_question_does_not_unlock_without_separate_command_sentence():
    message = "이 파일 읽고 뭐가 문제인지 봐줄래?"

    assert has_question_first_lock(message) is True
    assert has_explicit_post_question_command(message) is False
    assert should_block_question_first_tool_calls(message, assistant_content="") is True
    assert should_block_question_first_tool_calls(message, assistant_content="네, 읽어보겠습니다.") is True
