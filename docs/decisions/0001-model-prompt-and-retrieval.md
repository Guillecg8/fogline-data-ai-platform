# 0001. Model, prompt, context and search mode for the Ops Knowledge Copilot

- Status: accepted
- Date: 2026-11-12
- Project: `projects/02-ops-knowledge-copilot`

## Context

The copilot answers operations questions from a versioned corpus (SOPs, policies,
contracts, memos) and must cite its sources. It runs on a laptop, in CPU, at no cost.
We compared two local models, two prompts and two ways of giving context to the model.

How we measured (details in `projects/02-ops-knowledge-copilot/evals/REPORT.md`):

- 60 development questions and 20 held-out questions. The held-out set was never used to
  change the prompt or the code.
- Deterministic scoring by expected facts, forbidden stale values and citation to an expected
  document. No model-as-judge.
- Wrong answer = the copilot answered and was wrong (or should have abstained). False
  abstention = it abstained although the documents had the answer.

## Options and results

Held-out set (20 questions; one question is 5 points):

| Model | Prompt / context | Pass | Wrong | False abstention | p50 |
|---|---|---|---|---|---|
| qwen2.5:7b | v1 / sections | 80% | 10% | 13% | 23.2 s |
| qwen2.5:7b | v2 / documents | 80% | 10% | 13% | 25.7 s |
| qwen2.5:7b | v2 / sections | 80% | 10% | 13% | 22.6 s |
| llama3.2:3b | v1 / sections | 65% | 30% | 7% | 6.2 s |
| llama3.2:3b | v2 / documents | 70% | 20% | 13% | 7.4 s |
| llama3.2:3b | v2 / sections | 65% | 30% | 7% | 6.1 s |

Development set (60 questions; one question is 1.7 points):

| Model | Prompt / context | Pass | Wrong | False abstention | p50 |
|---|---|---|---|---|---|
| qwen2.5:7b | v1 / sections | 92% | 2% | 8% | 14.8 s |
| qwen2.5:7b | v2 / documents | 93% | 5% | 2% | 24.4 s |
| qwen2.5:7b | v2 / sections | 90% | 7% | 4% | 14.5 s |
| llama3.2:3b | v1 / sections | 87% | 12% | 2% | 5.9 s |
| llama3.2:3b | v2 / documents | 90% | 8% | 2% | 7.4 s |
| llama3.2:3b | v2 / sections | 90% | 8% | 2% | 7.3 s |

Retrieval (development set): hybrid recall@6 100%, MRR 0.949; vector 100%, 0.941;
BM25 98%, 0.890.

## Decision

- Model: `qwen2.5:7b`.
- Prompt: v1. Context: matching sections. Search: hybrid (BM25 + vectors, RRF) with the
  validity-date filter.

## Why

- The model is the variable that matters. On held-out questions qwen scores 80% in all three
  variants and llama 65-70%. The prompt and context variants differ by one or two questions,
  which is noise.
- Prompt v2 was written after looking at development failures. It gained a little on the
  development set and nothing on the held-out set: it did not generalise. We keep v1, the
  simpler one, and record v2 as a negative result.
- Full documents as context add latency (24 s vs 15 s p50) without a gain on held-out.
- Error type matters more than the headline rate. With v1/sections qwen gives the fewest
  wrong answers on the development set (2%). For an operations assistant, abstaining is
  cheaper than inventing a procedure.
- llama3.2:3b is 3-4 times faster but gives wrong answers on 30% of held-out questions. It
  stays as a documented low-cost baseline, not as the default.
- Hybrid search: 100% recall@6. Its advantage over vector-only is one question out of 60, so
  it is not conclusive. We keep it because BM25 covers exact identifiers (document codes) and
  it costs little. A reranker is not justified while recall@6 is already 100%.

## Consequences

- Answers take about 15-25 s on CPU. Acceptable for a portfolio demo, not for production.
- The development score (92%) is optimistic; the held-out score (80%) is the figure to quote.
- Four held-out cases fail with every qwen variant: h09 and h10 (wrong answer), h07 and h19
  (false abstention). They were inspected as diagnosis only and not fixed, so the held-out set
  stays clean. They are known limitations.
- CI gates (`tests/test_quality_gates.py`) check the committed results: hybrid recall@6 >= 95%
  and MRR >= 0.90; default configuration pass rate >= 85% and wrong answers <= 5% on the
  development set, pass rate >= 75% and wrong answers <= 15% on the held-out set.
- Limitations: questions written with AI help, scoring by text matching, small samples
  (20 and 60 questions), synthetic corpus, injection tests only at question level.
- Revisit if the corpus grows, the model changes, or a hosted model is used for the demo.
