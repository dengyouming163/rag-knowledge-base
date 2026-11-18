"""多轮问题改写：把依赖上下文的追问补全为可独立检索的查询。

改写失败时一律回退原问题——检索链路不能因为一次额外的模型调用而中断。
"""

from __future__ import annotations

import re
from pathlib import Path

from .llm import ChatClient, LLMError

_PROMPT_PATH = Path(__file__).with_name("prompts") / "rewrite.txt"
REWRITE_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

# 最多拼接最近 6 轮对话，控制 prompt 成本
MAX_HISTORY_TURNS = 6
# 超过该长度视为自带完整语境，不再走"指代 → 追问"的启发式
MAX_FOLLOWUP_LENGTH = 20
_FOLLOWUP_MARKERS = (
    "那",
    "这个",
    "那个",
    "这些",
    "那些",
    "它",
    "他们",
    "还有",
    "呢",
    "上面",
    "刚才",
    "前面",
    "上述",
    "那么",
)

_PREFIX_RE = re.compile(
    r"^(?:改写后的问题|改写问题|改写后的查询|改写后|改写|重写后的问题|重写后的查询|重写"
    r"|优化后的问题|优化后的查询|优化|补全后的问题|补全|最终问题|检索问题|问题|查询|query)"
    r"\s*[:：]\s*",
    re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^(?:[-*•]|\d+[.、)])\s+")
_QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
    ("「", "」"),
    ("『", "』"),
    ("`", "`"),
)


def rewrite_query(
    question: str, history: list[dict], client: ChatClient | None = None
) -> str:
    """结合历史把追问改写成完整问题；无历史、无模型或任何异常时返回原问题。

    history 不足 2 条时说明还没有可消解的上文，直接跳过模型调用以省成本。
    """
    if not history or len(history) <= 1:
        return question

    formatted = _format_history(history)
    if not formatted:
        return question

    owns_client = False
    if client is None:
        try:
            client = ChatClient()
        except LLMError:
            return question
        owns_client = True

    try:
        prompt = REWRITE_PROMPT.format(history=formatted, question=question)
        result = client.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        return _clean(result.text) or question
    except Exception:  # noqa: BLE001 - 改写是可选优化，任何失败都应静默降级
        return question
    finally:
        if owns_client:
            client.close()


def is_followup(question: str) -> bool:
    """启发式判断问题是否依赖上文：含指代词且篇幅较短视为追问。

    只做粗筛，用于决定"要不要多花一次模型调用改写"；误判为追问只是多花一次
    低成本调用，误判为非追问则可能检索不到，所以阈值偏宽松。
    """
    text = (question or "").strip()
    if not text or len(text) > MAX_FOLLOWUP_LENGTH:
        return False
    return any(marker in text for marker in _FOLLOWUP_MARKERS)


def _format_history(history: list[dict]) -> str:
    lines: list[str] = []
    for message in history[-MAX_HISTORY_TURNS * 2 :]:
        content = (message.get("content") or "").strip()
        if not content:
            continue
        role = "用户" if message.get("role") == "user" else "助手"
        lines.append(f"{role}：{content}")
    return "\n".join(lines)


def _clean(text: str) -> str:
    """去首尾空白、解释行、前缀、包裹引号与列表符号。"""
    candidate = (text or "").strip()
    for line in candidate.splitlines():
        if line.strip():
            candidate = line.strip()
            break

    candidate = _PREFIX_RE.sub("", candidate)
    candidate = _BULLET_RE.sub("", candidate)
    candidate = _strip_wrapping_quotes(candidate)
    return candidate.strip()


def _strip_wrapping_quotes(text: str) -> str:
    result = text.strip()
    changed = True
    while changed and len(result) >= 2:
        changed = False
        for left, right in _QUOTE_PAIRS:
            if result.startswith(left) and result.endswith(right):
                result = result[len(left) : -len(right)].strip()
                changed = True
                break
    return result
