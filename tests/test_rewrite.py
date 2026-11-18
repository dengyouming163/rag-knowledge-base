"""src/rewrite.py 的单元测试：全部用假 ChatClient，不联网。"""

from __future__ import annotations

import pytest

from src.llm import ChatResult, LLMError
from src.rewrite import REWRITE_PROMPT, is_followup, rewrite_query


class FakeChatClient:
    """返回预置文本或抛出预置异常，并记录每次调用的参数。"""

    def __init__(self, text: str = "退款到账要多久？", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 512) -> ChatResult:
        self.calls.append(
            {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        )
        if self.error is not None:
            raise self.error
        return ChatResult(
            text=self.text,
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=1.0,
            model="fake",
        )


HISTORY = [
    {"role": "user", "content": "你们的退款政策是什么？"},
    {"role": "assistant", "content": "签收后七天内可无理由退款。"},
]


def test_prompt_template_has_placeholders():
    assert "{history}" in REWRITE_PROMPT
    assert "{question}" in REWRITE_PROMPT


def test_no_history_returns_question_without_calling_model():
    client = FakeChatClient()
    assert rewrite_query("退款到账要多久", [], client) == "退款到账要多久"
    assert client.calls == []


def test_single_turn_history_skips_model():
    client = FakeChatClient()
    history = [{"role": "user", "content": "退款政策是什么？"}]
    assert rewrite_query("那要多久", history, client) == "那要多久"
    assert client.calls == []


def test_history_triggers_model_with_cheap_params():
    client = FakeChatClient("退款到账要多久？")
    result = rewrite_query("那要多久", HISTORY, client)

    assert result == "退款到账要多久？"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["temperature"] == 0.0
    assert call["max_tokens"] == 200
    prompt = call["messages"][-1]["content"]
    assert "你们的退款政策是什么？" in prompt
    assert "那要多久" in prompt


def test_strips_prefix_quotes_and_brackets():
    client = FakeChatClient('改写后的问题：“退款到账要多久？”')
    assert rewrite_query("那要多久", HISTORY, client) == "退款到账要多久？"

    client = FakeChatClient("「退款到账要多久」")
    assert rewrite_query("那要多久", HISTORY, client) == "退款到账要多久"


def test_strips_single_quotes_and_whitespace():
    client = FakeChatClient("  '退款到账要多久'  \n")
    assert rewrite_query("那要多久", HISTORY, client) == "退款到账要多久"


def test_model_error_falls_back_to_original_question():
    client = FakeChatClient(error=LLMError("服务不可用", status_code=500))
    assert rewrite_query("那要多久", HISTORY, client) == "那要多久"


def test_empty_output_falls_back_to_original_question():
    client = FakeChatClient("   \n ")
    assert rewrite_query("那要多久", HISTORY, client) == "那要多久"


@pytest.mark.parametrize(
    "question",
    ["那保修期是多久呢", "它支持七天无理由吗", "还有别的退款方式吗", "这个怎么申请"],
)
def test_is_followup_detects_pronoun_short_questions(question):
    assert is_followup(question) is True


@pytest.mark.parametrize(
    "question",
    ["退款到账需要几个工作日", "怎么申请七天无理由退货", "E102 错误码是什么意思", ""],
)
def test_is_followup_rejects_self_contained_questions(question):
    assert is_followup(question) is False


def test_is_followup_rejects_long_question_even_with_pronoun():
    question = "我在上周三下单买了这个型号的扫地机器人，现在发现它吸力明显变弱，想了解这种情况下是否可以整机换新"
    assert is_followup(question) is False
