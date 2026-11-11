"""Run the evaluations and write the results next to the question sets.

uv run poe eval-retrieval            compare bm25, vector and hybrid search (no LLM needed)
uv run poe eval                      one run: configured model and variant, development set
uv run poe eval --limit 5            quick trial with the first 5 questions (not saved)
uv run poe eval-all                  every variant on both sets for the configured model;
                                     runs whose saved result is still valid are skipped
uv run poe eval-report               rebuild evals/REPORT.md from the saved results

The model comes from OPS_COPILOT_LLM_MODEL; the variant of a single run from
OPS_COPILOT_PROMPT and OPS_COPILOT_CONTEXT.
"""

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

from ops_copilot.evals.golden import EVALS_DIR, GOLDEN_FILE, SPLITS, GoldenCase, load_golden
from ops_copilot.evals.metrics import (
    RETRIEVAL_K,
    CaseResult,
    first_relevant_rank,
    score_case,
    summarize_generation,
    summarize_retrieval,
)
from ops_copilot.generation.answer import PROMPTS, AnswerConfig, answer_question
from ops_copilot.generation.llm import LLM
from ops_copilot.retrieval.hybrid import HybridRetriever

RESULTS_DIR = EVALS_DIR / "results"
REPORT_FILE = EVALS_DIR / "REPORT.md"
MODES = ("bm25", "vector", "hybrid")
PIPELINE_VERSION = "2"  # raise it when the answering code changes, so saved results go stale
BASELINE = AnswerConfig()
VARIANTS = (
    BASELINE,
    AnswerConfig(prompt="v2"),
    AnswerConfig(prompt="v2", context="documents"),
)


def fingerprint(retriever: HybridRetriever, *extra: str, questions: Path = GOLDEN_FILE) -> str:
    """Changes when the questions, the corpus chunks or the given extras (prompt) change."""
    digest = hashlib.sha256()
    digest.update(questions.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
    for chunk in sorted(retriever.chunks, key=lambda c: c.chunk_id):
        digest.update(f"{chunk.chunk_id}\n{chunk.text}\n".encode())
    for item in extra:
        digest.update(item.encode("utf-8"))
    return digest.hexdigest()[:12]


def generation_fingerprint(
    retriever: HybridRetriever,
    model: str,
    config: AnswerConfig = BASELINE,
    split: str = "dev",
) -> str:
    extra = [PROMPTS[config.prompt], model, PIPELINE_VERSION]
    if config.context != BASELINE.context:
        extra.append(f"{config.context}:{config.max_documents}")
    return fingerprint(retriever, *extra, questions=SPLITS[split])


def run_retrieval(cases: list[GoldenCase], retriever: HybridRetriever) -> dict:
    answerable = [case for case in cases if case.answerable]
    modes = {}
    for mode in MODES:
        ranks, misses = [], []
        for case in answerable:
            hits = retriever.search(case.question, k=RETRIEVAL_K, as_of=case.as_of, mode=mode)
            rank = first_relevant_rank(case, [hit.chunk.chunk_id for hit in hits])
            ranks.append(rank)
            if rank is None:
                misses.append(case.id)
        modes[mode] = {**summarize_retrieval(ranks), "misses": misses}
    return {"kind": "retrieval", "fingerprint": fingerprint(retriever), "modes": modes}


def run_generation(
    cases: list[GoldenCase],
    retriever: HybridRetriever,
    llm: LLM,
    model: str,
    config: AnswerConfig = BASELINE,
    split: str = "dev",
    verbose: bool = False,
) -> dict:
    results: list[CaseResult] = []
    for number, case in enumerate(cases, start=1):
        answer = answer_question(
            case.question, retriever, llm, as_of=case.as_of, k=RETRIEVAL_K, config=config
        )
        result = score_case(case, answer)
        results.append(result)
        if verbose:
            mark = "pass" if result.passed else "FAIL"
            print(
                f"[{number:>2}/{len(cases)}] {case.id} {mark}  {result.latency_ms} ms", flush=True
            )
    return {
        "kind": "generation",
        "model": model,
        "variant": config.variant,
        "split": split,
        "fingerprint": generation_fingerprint(retriever, model, config, split),
        "summary": summarize_generation(results),
        "cases": [result.model_dump() for result in results],
    }


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def result_name(model: str, config: AnswerConfig = BASELINE, split: str = "dev") -> str:
    name = f"generation-{slug(model)}"
    if config != BASELINE:
        name += f"--{config.variant}"
    if split != "dev":
        name += f"--{split}"
    return name


def save(result: dict, name: str) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"run_date": date.today().isoformat(), **result}
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    (RESULTS_DIR / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")


def is_fresh(name: str, expected_fingerprint: str) -> bool:
    path = RESULTS_DIR / f"{name}.json"
    if not path.exists():
        return False
    return json.loads(path.read_text(encoding="utf-8")).get("fingerprint") == expected_fingerprint


def run_all(retriever: HybridRetriever, llm: LLM, model: str) -> None:
    """Every variant on both sets. A run with a valid saved result is not repeated."""
    for split in SPLITS:
        cases = load_golden(SPLITS[split])
        for config in VARIANTS:
            name = result_name(model, config, split)
            if is_fresh(name, generation_fingerprint(retriever, model, config, split)):
                print(f"skip {name}: saved result is still valid", flush=True)
                continue
            print(f"\nrun {name}: {len(cases)} questions", flush=True)
            save(run_generation(cases, retriever, llm, model, config, split, verbose=True), name)


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _answers_table(runs: list[dict]) -> list[str]:
    lines = [
        "| Model | Variant | Pass rate | Wrong answers | False abstentions | "
        "Correct abstention | p50 latency | p95 latency |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        s = run["summary"]
        rates = ("pass_rate", "wrong_answer_rate", "false_abstention_rate", "correct_abstention")
        cells = " | ".join(_percent(s.get(name)) for name in rates)  # n/a in older files
        lines.append(
            f"| {run['model']} | {run.get('variant', BASELINE.variant)} | {cells} | "
            f"{s['latency_p50_ms'] / 1000:.1f} s | {s['latency_p95_ms'] / 1000:.1f} s |"
        )
    return lines


def _failed_cases(runs: list[dict]) -> list[str]:
    lines = []
    for run in runs:
        wrong = [c["id"] for c in run["cases"] if c["answered"] and not c["passed"]]
        silent = [c["id"] for c in run["cases"] if not c["answered"] and not c["passed"]]
        lines.append(
            f"- **{run['model']}, {run.get('variant', BASELINE.variant)}**: "
            f"wrong answers: {', '.join(wrong) or 'none'}; "
            f"false abstentions: {', '.join(silent) or 'none'}"
        )
    return lines


def build_report() -> str:
    lines = [
        "# Evaluation report",
        "",
        "Generated by `uv run poe eval-report` from the files in `evals/results/`. Do not edit.",
        "",
        "- Development set: 60 questions. Held-out set: 20 questions that were never used to "
        "change the prompt or the code. The corpus is synthetic: 32 documents.",
        "- Scoring is deterministic string and citation matching, not human review.",
        "- A case passes when the answer has the expected facts, none of the stale or injected "
        "values, and cites an expected document. An unanswerable case passes when the copilot "
        "abstains.",
        "- Wrong answers: the copilot answered and the answer was wrong, incomplete, or should "
        "have been an abstention (share of all questions). False abstentions: it abstained "
        "although the documents had the answer (share of answerable questions).",
        "- Variants: `v1`/`v2` is the prompt version; `sections`/`documents` is whether the "
        "model reads the matching sections or the whole documents they belong to.",
        "",
    ]
    retrieval_file = RESULTS_DIR / "retrieval.json"
    if retrieval_file.exists():
        retrieval = json.loads(retrieval_file.read_text(encoding="utf-8"))
        key = f"recall_at_{RETRIEVAL_K}"
        lines += [
            "## Retrieval (development set)",
            "",
            f"| Mode | Recall@1 | Recall@3 | Recall@{RETRIEVAL_K} | MRR | Misses |",
            "|---|---|---|---|---|---|",
        ]
        for mode, s in retrieval["modes"].items():
            lines.append(
                f"| {mode} | {_percent(s['recall_at_1'])} | {_percent(s['recall_at_3'])} | "
                f"{_percent(s[key])} | {s['mrr']:.3f} | {', '.join(s['misses']) or '-'} |"
            )
        lines.append("")

    runs = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(RESULTS_DIR.glob("generation-*.json"))
    ]
    runs.sort(key=lambda run: (run["model"], run.get("variant", BASELINE.variant)))
    dev = [run for run in runs if run.get("split", "dev") == "dev"]
    holdout = [run for run in runs if run.get("split", "dev") == "holdout"]
    if dev:
        lines += ["## Answers (development set)", "", *_answers_table(dev)]
        categories = sorted({c for run in dev for c in run["summary"]["pass_rate_by_category"]})
        lines += [
            "",
            "### Pass rate by category",
            "",
            "| Model | Variant | " + " | ".join(categories) + " |",
            "|---|---|" + "---|" * len(categories),
        ]
        for run in dev:
            by_category = run["summary"]["pass_rate_by_category"]
            cells = " | ".join(_percent(by_category.get(c)) for c in categories)
            lines.append(f"| {run['model']} | {run.get('variant', BASELINE.variant)} | {cells} |")
        lines += ["", "### Failed cases", "", *_failed_cases(dev), ""]
    if holdout:
        lines += ["## Answers (held-out set)", "", *_answers_table(holdout)]
        lines += ["", "### Failed cases", "", *_failed_cases(holdout), ""]
    return "\n".join(lines)


def write_report() -> None:
    REPORT_FILE.write_text(build_report(), encoding="utf-8", newline="\n")
    print(f"report written to {REPORT_FILE.relative_to(EVALS_DIR.parent)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the copilot.")
    parser.add_argument("command", choices=["retrieval", "generation", "all", "report"])
    parser.add_argument("--limit", type=int, help="only the first N questions (not saved)")
    args = parser.parse_args()

    if args.command == "report":
        write_report()
        return

    from ops_copilot.generation.llm import ChatClient
    from ops_copilot.net import use_system_certificates
    from ops_copilot.retrieval.cli import build_retriever

    use_system_certificates()
    retriever = build_retriever()

    if args.command == "all":
        llm = ChatClient()
        print(f"model={llm.model}")
        run_all(retriever, llm, llm.model)
        write_report()
        return

    cases = load_golden()[: args.limit] if args.limit else load_golden()
    if args.command == "retrieval":
        result = run_retrieval(cases, retriever)
        for mode, summary in result["modes"].items():
            print(mode, {k: v for k, v in summary.items() if k != "misses"})
        name = "retrieval"
    else:
        llm = ChatClient()
        config = AnswerConfig.from_env()
        print(f"model={llm.model} variant={config.variant} questions={len(cases)}")
        result = run_generation(cases, retriever, llm, llm.model, config, verbose=True)
        print(json.dumps(result["summary"], indent=2))
        name = result_name(llm.model, config)

    if args.limit:
        print("trial run with --limit: results not saved")
    else:
        save(result, name)
        write_report()


if __name__ == "__main__":
    main()
