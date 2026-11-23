# Evaluation

The evaluation suite is the project's regression standard. Any change to chunking, embedding, retrieval, reranking, prompting or the chat model is expected to be measured against it before merge.

## Dataset

| Artifact | Content |
|---|---|
| `eval/cases.jsonl` | 50 labelled cases. 38 answerable, 12 unanswerable. Types: 25 `fact`, 8 `compare`, 5 `multi_hop`, 12 `unanswerable`. Several fact cases are adversarially phrased (colloquial, boundary values, leading questions). |
| `eval/demo_corpus.json` | 26 pre-chunked documents used as the retrieval corpus. Pre-chunking keeps `chunk_id`s stable so labels survive code changes. |
| `eval/baseline.json` | Mock retrieval baseline recorded on the current suite and corpus. |
| `eval/baseline_real_gen.json` | Real embedding plus real generation snapshot reported below. |
| `eval/refusal_calibration.json` | Threshold sweep used to select the refusal gate value. |

Case fields and labelling rules are described in [`eval/README.md`](../eval/README.md). In short: a chunk counts as correct only if reading it alone can answer the question; a case is unanswerable only if the entire corpus lacks support. The corpus was expanded to 26 chunks specifically so that top-5 retrieval has something to discriminate between.

## Metrics

| Metric | Definition |
|---|---|
| `recall_at_k` | Over answerable cases: per case, the fraction of labelled chunks present in the top-k results, then averaged. Multi-label cases receive partial credit. |
| `mrr` | Over answerable cases: mean reciprocal rank of the first correct chunk in the top-k list. |
| `hit_rate` | Over answerable cases: fraction with `recall_at_k > 0`. |
| `refusal_accuracy` | Over unanswerable cases: fraction correctly refused. An answer counts as a refusal when no context is returned or the answer contains a refusal marker such as `无法确定`. |
| `answer_coverage` | Over answerable cases: fraction not refused. |
| `citation_accuracy` | Over answers that were answerable, generated successfully and not refused: mean fraction of cited `chunk_id`s that exist in the actual context. An answer with no citations contributes 0. |
| `generation_errors` | Count of cases where rewriting or generation raised an error. These are excluded from generation-dependent denominators. |

Retrieval metrics require no generation and are always produced. Refusal accuracy, answer coverage and citation accuracy are `null` unless the run uses `--generate`.

## Current results

Run conditions: real `BAAI/bge-m3` embeddings, real `deepseek-chat` generation, `mode=hybrid`, `top_k=5`, suite `eval/cases.jsonl`, corpus `eval/demo_corpus.json`, 26 chunks. Source: `eval/baseline_real_gen.json`.

| Metric | Value |
|---|---:|
| Recall@5 | **1.0000** |
| MRR@5 | **0.9737** |
| Hit rate@5 | **1.0000** |
| Citation accuracy | **1.0000** |
| Refusal accuracy | **1.0000** |
| Answer coverage | **0.9211** |
| Generation errors | **0** |

Interpretation: retrieval placed every labelled chunk in the top 5, so the remaining losses are ranking position (MRR `0.9737`) and three answerable questions refused by the model or the gate (`0.9211` coverage). Citation accuracy is `1.0000` across 35 answers; no answer omitted citations.

Mock baseline (`eval/baseline.json`, deterministic hash vectors, retrieval only): Recall@5 `0.9342`, MRR `0.8575`, hit rate `0.9474`. These numbers exist to detect retrieval regressions in CI. Hash vectors carry no semantics, so they say nothing about production quality.

## Refusal threshold

The gate compares the maximum vector cosine in the retrieved context against `REFUSAL_MIN_SIMILARITY`. The default `0.56` comes from the sweep in `eval/refusal_calibration.json` (vector mode, real embeddings, 50 cases: 38 answerable / 12 unanswerable):

| Threshold | Accuracy | False refusals | False answers |
|---:|---:|---:|---:|
| 0.00 (answer everything) | 0.76 | 0 | 12 |
| 0.52 | 0.84 | 1 | 7 |
| 0.54 | 0.86 | 1 | 6 |
| **0.56** | **0.88** | **2** | **4** |
| 0.66 | 0.84 | 8 | 0 |

At `0.56` the gate achieves 0.88 accuracy on the calibration run, with two answerable questions refused and four unanswerable questions answered. Higher thresholds suppress false answers but refuse far more answerable questions. Re-calibrate with `scripts/calibrate_refusal.py` whenever the embedding model changes:

```bash
py -3.13 scripts/calibrate_refusal.py --real --mode vector --write
```

The threshold can be overridden with `REFUSAL_MIN_SIMILARITY`; `0` disables the gate and is used by the test suite.

## Commands

All commands run from the repository root. `--mock` and `--real` are mutually exclusive and one is required.

```bash
# Deterministic, offline, used by CI
py -3.13 scripts/run_eval.py --mock --out eval/baseline.json

# Real embeddings, retrieval only
py -3.13 scripts/run_eval.py --real --out eval/baseline_real.json

# Full RAG: retrieval + rewriting + generation + citation validation
py -3.13 scripts/run_eval.py --real --generate --out eval/baseline_real_gen.json

# Compare a run against a stored baseline; exit code 1 when it regresses
py -3.13 scripts/run_eval.py --mock --compare eval/baseline.json
```

Additional flags: `--suite`, `--corpus`, `--mode vector|bm25|hybrid`, `--top-k`, `--threshold` (allowed total-score drop for `--compare`, default `0.02`), `--out`, and `--limit N` to run a prefix of cases when verifying that a key works. `--generate` performs two LLM calls per case (rewrite and answer). A single generation failure is recorded on the case and does not abort the run.

## Regression gate

Two layers enforce the baseline:

1. **`scripts/run_eval.py --compare eval/baseline.json`** re-runs the suite under the baseline's recorded conditions and compares per-case results, failing when a case that used to have hits drops to zero or when an aggregate metric drops beyond the threshold.
2. **`scripts/ci_gate.py`** compares a fresh run with a baseline file, defaulting to `eval/baseline.json` with a `0.02` allowed drop:

```bash
py -3.13 scripts/ci_gate.py
py -3.13 scripts/ci_gate.py --baseline eval/baseline.json --threshold 0.02
```

The gate reads `mode`, `top_k`, `mock`, `suite`, `corpus` and `corpus_chunks` from the baseline and refuses to compare when the corpus size changed, so results from different conditions cannot be mixed. It watches `recall_at_k`, `mrr` and `hit_rate`, fails when any watched metric drops beyond the threshold, and additionally fails when an answerable case moves from any hit to no hit. Other per-case decreases are reported as warnings. Exit codes: `0` pass, `1` regression, `2` not comparable.

CI runs the mock gate on every push and pull request (`.github/workflows/ci.yml`, `eval` job).

## Limitations

- **Small sample** — 50 cases in one domain (after-sales, logistics, device faults). A single mis-ranked case moves MRR by roughly 0.026.
- **Single domain and single language** — the corpus and questions are Chinese consumer-support content; results do not transfer to other domains or languages without a new suite.
- **High retrieval scores** — Recall@5 `1.0000` on a 26-chunk corpus with top-5 retrieval is expected to be near saturation. Treat it as a regression signal, not as a competitive benchmark result.
- **Mock vectors are not quality evidence** — deterministic hash embeddings verify plumbing and guard against retrieval regressions only.
- **External model dependency** — real runs depend on the embedding and chat providers. Provider-side model changes, latency and failures affect reproducibility; record the model names and date with any snapshot.
- **Generation is non-deterministic** — answers are requested at temperature 0, but providers may still vary. Small movements in citation accuracy and coverage should be re-measured before being treated as regressions.
- **Refusal calibration is dataset-specific** — the `0.56` threshold was selected on these 50 cases and must be re-derived for a different corpus or embedding model.
