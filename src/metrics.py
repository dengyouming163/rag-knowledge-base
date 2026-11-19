"""检索与回答质量指标。

全部是纯函数：输入两个列表，输出一个 0~1 的小数。
不读文件、不连数据库、不调模型，所以能在 CI 里秒级跑完。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence


def _check_k(k: int) -> None:
    """k 是"看前几条"，必须正整数，否则后续切片会静默出错。"""
    if k <= 0:
        raise ValueError("k 必须为正整数")


def recall_at_k(predicted: Sequence[str], gold: Sequence[str], *, k: int) -> float:
    """Recall@k：前 k 条里命中了多少比例的标注 chunk。

    - 只在前 k 条里找（k 决定"看多远"）
    - 多标注按比例给分：标 2 个、命中 1 个 -> 0.5
    - gold 为空表示不可回答题，返回 0；调用方不应把它计入分母
    """
    _check_k(k)
    if not gold:
        return 0.0

    # 去重后再算，避免标注重复导致分母虚高
    gold_set = set(gold)
    # 只看前 k 条，这正是 "Recall@k" 里那个 k 的含义
    top_k = set(predicted[:k])

    hits = sum(1 for chunk_id in gold_set if chunk_id in top_k)
    return hits / len(gold_set)


def mrr(predicted: Sequence[str], gold: Sequence[str], *, k: int) -> float:
    """MRR@k：第一条正确结果的排名取倒数；前 k 条里都没有则为 0。

    注意它只看"第一条"，所以标了多个、只命中靠后那个时，
    分数可能低于 Recall —— 这正是两者互补的原因。
    """
    _check_k(k)
    if not gold:
        return 0.0

    gold_set = set(gold)
    # enumerate 从 1 开始，因为"第 1 名"的倒数是 1/1
    for rank, chunk_id in enumerate(predicted[:k], start=1):
        if chunk_id in gold_set:
            return 1.0 / rank
    return 0.0


def refusal_accuracy(results: Sequence[bool]) -> float:
    """拒答准确率：不可回答题里，正确拒答的比例。

    results 每一项代表"这道不可回答题是否被正确拒答"。
    单独看它会有一个漏洞：系统把所有题都拒答也能拿 1.0，
    所以必须搭配 answer_coverage 一起看。
    """
    if not results:
        return 0.0
    return sum(1 for ok in results if ok) / len(results)


def answer_coverage(results: Sequence[bool]) -> float:
    """答案覆盖率：可回答题里，系统正常给出答案的比例。

    它防的是"为了不编答案，干脆什么都不答"。
    """
    if not results:
        return 0.0
    return sum(1 for answered in results if answered) / len(results)


def citation_accuracy(cited: Sequence[str], context_ids: Collection[str]) -> float:
    """引用准确率：回答里引用的 chunk_id，有多少真实存在于本次上下文。

    cited 是模型回答中出现的引用 id；context_ids 是这次真正喂给模型的
    chunk 集合。引用了不存在的 id，说明模型在编来源。
    """
    if not cited:
        # 一条都没引用：不算"全对"，交由调用方判断是否强制要求引用
        return 0.0

    context = set(context_ids)
    valid = sum(1 for chunk_id in cited if chunk_id in context)
    return valid / len(cited)
