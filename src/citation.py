"""引用校验：识别"编造来源"的回答。

为什么必须做：模型很容易写出 `[kb-after-sale#9]` 这种看起来专业、实际不存在的
引用。用户点开发现什么都没有，信任直接崩塌——这比"没有引用"严重得多。

策略（与计划的差异说明）：计划写"剔除标注并标记 citation_ok=false"。这里改为
**保留标注原文、只做标记与统计**——因为静默改写模型输出会让线上问题无法复现，
排查时看不到"模型到底编了什么"。是否对用户隐藏，由展示层决定。
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

from .metrics import citation_accuracy

CITATION_RE = re.compile(r"\[([^\[\]\s]+)\]")


@dataclass
class CitationReport:
    cited_ids: list[str] = field(default_factory=list)
    invalid_ids: list[str] = field(default_factory=list)
    accuracy: float = 0.0
    ok: bool = True


def extract_ids(answer: str) -> list[str]:
    """提取 `[chunk_id]` 形式的引用，去重且保序。

    注意排除 Markdown 链接 `[文字](url)`：那种 `[...]` 后面紧跟 `(`，
    不是引用标注，若混进来会制造大量假阳性。
    """
    ids: list[str] = []
    for match in CITATION_RE.finditer(answer or ""):
        following = (answer or "")[match.end() : match.end() + 1]
        if following == "(":
            continue
        chunk_id = match.group(1).strip()
        if chunk_id and chunk_id not in ids:
            ids.append(chunk_id)
    return ids


def validate_citation(answer: str, context_ids: Collection[str]) -> CitationReport:
    """把回答里的引用与本次上下文比对，返回报告。

    - `ok = not invalid_ids`：没有引用也算 ok（拒答场景本来就不该有引用）；
    - `accuracy` 沿用 metrics 的口径：无引用时为 0，便于评测区分"没引"和"引错"。
    """
    cited = extract_ids(answer)
    context = set(context_ids)
    invalid = [chunk_id for chunk_id in cited if chunk_id not in context]
    return CitationReport(
        cited_ids=cited,
        invalid_ids=invalid,
        accuracy=citation_accuracy(cited, context),
        ok=not invalid,
    )
