"""Scoring. Everything here is deterministic: no model judges another model.

String matching is a proxy for correctness. It is cheap and reproducible, and it can be
wrong in both directions, so the failed cases are listed for a human to read.
"""

import re
from statistics import mean

from pydantic import BaseModel

from ops_copilot.evals.golden import GoldenCase
from ops_copilot.generation.answer import Answer

RETRIEVAL_K = 6


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip().lower()


def matches(text: str, pattern: str) -> bool:
    """True if any alternative of `pattern` ("a|b|c") appears in the text."""
    haystack = normalize(text)
    return any(normalize(option) in haystack for option in pattern.split("|"))


def doc_key(chunk_id: str) -> str:
    return chunk_id.split("#")[0]


def first_relevant_rank(case: GoldenCase, retrieved: list[str]) -> int | None:
    """Position (from 1) of the first retrieved chunk that belongs to an expected document."""
    for rank, chunk_id in enumerate(retrieved, start=1):
        if doc_key(chunk_id) in case.expected_docs:
            return rank
    return None


class CaseResult(BaseModel):
    id: str
    category: str
    answerable: bool
    passed: bool
    answered: bool
    facts_ok: bool
    forbidden: list[str]
    citation_ok: bool
    retrieval_rank: int | None
    abstain_reason: str | None
    answer: str
    cited: list[str]
    latency_ms: int
    tokens: int


def score_case(case: GoldenCase, answer: Answer) -> CaseResult:
    answered = answer.answerable
    text = answer.text if answered else ""
    facts_ok = answered and all(matches(text, pattern) for pattern in case.must_include)
    forbidden = [pattern for pattern in case.must_not_include if matches(text, pattern)]
    cited = [doc_key(citation.chunk_id) for citation in answer.citations]
    citation_ok = any(key in case.expected_docs for key in cited)
    if case.answerable:
        passed = answered and facts_ok and not forbidden and citation_ok
    else:
        passed = not answered
    return CaseResult(
        id=case.id,
        category=case.category,
        answerable=case.answerable,
        passed=passed,
        answered=answered,
        facts_ok=facts_ok,
        forbidden=forbidden,
        citation_ok=citation_ok,
        retrieval_rank=first_relevant_rank(case, answer.retrieved),
        abstain_reason=answer.abstain_reason,
        answer=answer.text,
        cited=cited,
        latency_ms=answer.latency_ms,
        tokens=answer.prompt_tokens + answer.completion_tokens,
    )


def rate(flags: list[bool]) -> float | None:
    return round(sum(flags) / len(flags), 3) if flags else None


def percentile(values: list[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))] if ordered else 0


def summarize_generation(results: list[CaseResult]) -> dict:
    answerable = [r for r in results if r.answerable]
    unanswerable = [r for r in results if not r.answerable]
    answered = [r for r in answerable if r.answered]
    categories = sorted({r.category for r in results})
    return {
        "cases": len(results),
        "pass_rate": rate([r.passed for r in results]),
        "answer_rate": rate([r.answered for r in answerable]),
        "fact_accuracy": rate([r.facts_ok and not r.forbidden for r in answerable]),
        "citation_accuracy": rate([r.citation_ok for r in answered]),
        "correct_abstention": rate([not r.answered for r in unanswerable]),
        "latency_p50_ms": percentile([r.latency_ms for r in results], 0.5),
        "latency_p95_ms": percentile([r.latency_ms for r in results], 0.95),
        "avg_tokens": round(mean(r.tokens for r in results)) if results else 0,
        "pass_rate_by_category": {
            c: rate([r.passed for r in results if r.category == c]) for c in categories
        },
    }


def summarize_retrieval(ranks: list[int | None]) -> dict:
    """`ranks` holds, per answerable case, the rank of the first relevant chunk (or None)."""
    return {
        "cases": len(ranks),
        "recall_at_1": rate([r is not None and r <= 1 for r in ranks]),
        "recall_at_3": rate([r is not None and r <= 3 for r in ranks]),
        f"recall_at_{RETRIEVAL_K}": rate([r is not None for r in ranks]),
        "mrr": round(mean(1 / r if r else 0.0 for r in ranks), 3) if ranks else None,
    }
