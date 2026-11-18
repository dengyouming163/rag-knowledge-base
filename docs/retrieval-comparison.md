# 三路检索对比（vector / BM25 / hybrid）

对比同一份语料、同一批查询下三条召回路径的效果：纯向量、纯 BM25、RRF 混合。目标是回答两个问题：**单路各自擅长什么**、**融合是否真的比单路 recall 更高**。

## 为什么需要对比

- 向量检索（bge-m3）擅长语义改写：用户说「买完不想要了怎么退钱」，文档写「七天无理由退款」，字面几乎不重叠，但语义一致。
- BM25 擅长精确串：型号、错误码（`E102`）、专有名词这类 token，向量会被上下文稀释，BM25 的 IDF 加权反而更准。
- 两者互补，所以用 RRF 融合：`score = Σ 1/(RRF_K + rank)`，`RRF_K = 60`。取 60 是因为它让头部名次之间的分差足够平缓（rank1≈1/61，rank10≈1/70），单路的第一名不会压倒另一路的多条中等结果；同时 RRF 只用名次，不需要把余弦相似度和 BM25 原始分数归一化到同一量纲。

## 运行方式

无需 API key（用确定性哈希假向量，只验证流程能否跑通）：

```bash
py -3.13 scripts/compare_routes.py --mock
```

用真实 embedding（需要 `EMBED_API_KEY`，参考 `.env.example`）：

```bash
py -3.13 scripts/compare_routes.py \
  --input eval/demo_queries.json \
  --corpus eval/demo_corpus.json
```

自定义数据时保持两个文件格式：

```json
// 查询文件
[{"query": "退款一般几天到账", "gold_chunk_ids": ["kb-after-sale#1"]}]
```

```json
// 语料文件
[{"chunk_id": "kb-after-sale#1", "doc_id": "kb-after-sale", "text": "……", "audience": "public"}]
```

可用参数：`--top-k`（默认 5）、`--recall-k`（每路召回候选数，默认 40）、`--dim`（mock 向量维度，默认 64）。

## 输出怎么读

```
[1/6] 查询：退款一般几天到账
  gold  : kb-after-sale#1
  vector: kb-after-sale#1[OK]  kb-logistics#1[--]  ...
  bm25  : kb-after-sale#1[OK]  ...
  hybrid: kb-after-sale#1[OK]  ...
```

- `[OK]` 表示该条命中 gold，`[--]` 表示未命中。
- 末尾 `Recall@5 汇总` 是每条查询「top-5 覆盖了多大比例的 gold」的平均值（gold 为空按 0 计）；这是检索层指标，不代表最终回答质量。

汇总段的输出结构示意（分数占位，真实数值以实际运行为准）：

```
Recall@5 汇总（查询数 6）
  vector: 0.xxx
  bm25  : 0.xxx
  hybrid: 0.xxx
```

真实 embedding 下的结果（跑完 `--input eval/demo_queries.json` 后填写；`--mock` 的输出**不能**当作实验结论）：

| 模式 | Recall@5 | 备注 |
| --- | --- | --- |
| vector | 待填真实结果 | bge-m3 语义召回 |
| bm25 | 待填真实结果 | 自己实现的中文 bigram + BM25 |
| hybrid | 待填真实结果 | RRF，k=60，两路各取 40 条 |

同时建议记录：哪些查询只有 BM25 命中（多半是错误码/型号类）、哪些只有向量命中（多半是口语化改写）；这类逐条对比比单个汇总数字更适合面试讲解。

## 面试可讲的三点结论

1. **语义改写靠向量**：query 与文档零字面重叠时（「买完不想要了怎么退钱」vs「七天无理由退款」），BM25 的分数接近 0，向量仍能召回。这是稠密检索不可替代的部分。
2. **型号/错误码靠 BM25**：`E102`、`E205` 这类短 token 在向量空间里权重被稀释，容易和「错误码」整体语义混淆；BM25 的精确匹配 + IDF 能把它顶到第一。
3. **融合提升召回**：RRF 只依赖名次，两路互补后 top-k 覆盖 gold 的概率通常高于任一单路；代价是分数不可解释（只有相对大小），需要和 rerank 配合做精排。

## 局限

- 自研 BM25 用「单字 + 相邻双字」替代词典分词：零依赖、可预测，但会产生「款政」这类跨词 bigram 噪声，词表也比 jieba 大。
- `--mock` 的哈希向量不具备语义能力，只用于离线演示与 CI。
- 当前评估只算 Recall@5，未覆盖排序质量（MRR/NDCG）与端到端回答正确率，属于 Day 5 评测集的范围。
