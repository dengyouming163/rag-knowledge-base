"""拒答阈值：把"检索没找到"和"模型可以答"区分开。

为什么不能直接用融合分数当阈值：
- RRF 分只是 `Σ 1/(60+rank)` 的函数，量级 0.01~0.04，**归一化后与相关性无关**；
  一条完全无关但排第 1 的块也能拿到 0.0164。
- 实测（mock 向量，50 条用例）：hybrid 模式下可回答题与不可回答题的最高分中位数
  分别是 0.0164 / 0.0161，几乎不可分，最优阈值只比"全部作答"高 2 个百分点。

所以这里的置信度统一取**向量路的原始余弦相似度**：
它才是"语义有多像"的量，且跨模式可比。

阈值来自实际标定（50 条用例，可回答 38 / 不可回答 12）：
- **真实 bge-m3**：0.56 → 准确率 0.88（误拒 2 / 误答 4）
- mock 哈希向量：0.18 → 准确率 0.80（仅比全答 0.76 高 4 点，参考价值有限）

默认值取真实模型的结果。换 embedding 后必须重标：
    py -3.13 scripts/calibrate_refusal.py --real --mode vector --write
"""

from __future__ import annotations

import os

DEFAULT_MIN_SIMILARITY = 0.56


def default_min_similarity() -> float:
    """允许用 REFUSAL_MIN_SIMILARITY 覆盖阈值。

    用途：换 embedding 后重新标定不必改代码；测试里设 0 可关闭这道闸门。
    解析失败时退回默认值，不让一个错误的环境变量把服务打挂。
    """
    raw = os.environ.get("REFUSAL_MIN_SIMILARITY")
    if not raw:
        return DEFAULT_MIN_SIMILARITY
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_MIN_SIMILARITY


def confidence_of(hits: list) -> float | None:
    """取本次上下文里的语义置信度。

    优先用向量路原始余弦（hybrid 模式会把 recall 时的 cosine 写进
    `metadata["vector_score"]`）；纯 BM25 模式没有向量分，返回 None，
    由调用方决定是否放宽为不拦。
    """
    if not hits:
        return None
    scores = [
        float(hit.metadata["vector_score"])
        for hit in hits
        if hit.metadata.get("vector_score") is not None
    ]
    if scores:
        return max(scores)
    return None
