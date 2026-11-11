import json
import re
from collections import Counter
from datetime import date

import pytest

from ops_copilot.evals import run as eval_run
from ops_copilot.evals.golden import GoldenCase, load_golden
from ops_copilot.evals.metrics import matches, score_case
from ops_copilot.generation.answer import SYSTEM_PROMPT, Answer, Citation
from ops_copilot.generation.llm import LLMReply
from ops_copilot.ingestion import pipeline
from ops_copilot.paths import CORPUS_DIR
from ops_copilot.retrieval.embedding import hashing_embedder
from ops_copilot.retrieval.hybrid import HybridRetriever, is_valid, load_chunks

CASES = load_golden()


@pytest.fixture(scope="module")
def retriever(tmp_path_factory) -> HybridRetriever:
    data_dir = tmp_path_factory.mktemp("data")
    pipeline.run(CORPUS_DIR, data_dir)
    chunks, _ = load_chunks(data_dir / "gold" / "chunks.jsonl")
    return HybridRetriever(chunks, hashing_embedder())


def make_answer(text="The fee is $45.", cited=("SOP-FD-001@v1#03",), answerable=True) -> Answer:
    citations = [
        Citation(
            number=n,
            chunk_id=chunk_id,
            doc_id=chunk_id.split("@")[0],
            version=1,
            title="t",
            section="s",
            effective_from=date(2025, 1, 1),
        )
        for n, chunk_id in enumerate(cited, start=1)
    ]
    return Answer(
        question="q",
        as_of=date(2026, 11, 10),
        answerable=answerable,
        text=text if answerable else "I could not find this.",
        citations=citations if answerable else [],
        abstain_reason=None if answerable else "model_declined",
        retrieved=list(cited),
        latency_ms=10,
        prompt_tokens=5,
        completion_tokens=5,
    )


# ---------------------------------------------------------------- the golden set itself


def test_golden_set_has_60_cases_with_unique_ids():
    assert len(CASES) == 60
    assert len({case.id for case in CASES}) == 60
    assert Counter(case.category for case in CASES) == {
        "direct": 20,
        "multi": 8,
        "versioned": 8,
        "temporal": 8,
        "conflict": 4,
        "unanswerable": 8,
        "injection": 4,
    }


def test_answerable_cases_name_sources_and_facts():
    for case in CASES:
        if case.answerable:
            assert case.expected_docs and case.must_include, case.id
        else:
            assert not case.expected_docs and not case.must_include, case.id


def test_expected_documents_were_in_force_on_the_case_date(retriever):
    in_force: dict[str, list] = {}
    for chunk in retriever.chunks:
        in_force.setdefault(f"{chunk.doc_id}@v{chunk.version}", []).append(chunk)
    for case in CASES:
        for key in case.expected_docs:
            assert key in in_force, f"{case.id}: {key} is not in the corpus"
            assert is_valid(in_force[key][0], case.as_of), f"{case.id}: {key} not in force"


def test_expected_facts_really_appear_in_the_expected_documents(retriever):
    text_by_doc: dict[str, str] = {}
    for chunk in retriever.chunks:
        key = f"{chunk.doc_id}@v{chunk.version}"
        text_by_doc[key] = text_by_doc.get(key, "") + " " + chunk.text
    for case in CASES:
        evidence = " ".join(text_by_doc[key] for key in case.expected_docs)
        for pattern in case.must_include:
            assert matches(evidence, pattern), f"{case.id}: {pattern!r} is not in its sources"


# ---------------------------------------------------------------- scoring

LATE_CHECKOUT = GoldenCase(
    id="x",
    category="direct",
    question="q",
    expected_docs=["SOP-FD-001@v1"],
    must_include=["45"],
    must_not_include=["35"],
)
PARKING = GoldenCase(id="y", category="unanswerable", question="q", answerable=False)


def test_alternatives_are_separated_by_a_bar():
    assert matches("Check-in starts at 3 PM.", "3:00|3 pm")
    assert not matches("Check-in starts at 4 PM.", "3:00|3 pm")


def test_correct_cited_answer_passes():
    assert score_case(LATE_CHECKOUT, make_answer()).passed


def test_stale_value_fails_even_with_the_right_fact():
    result = score_case(LATE_CHECKOUT, make_answer(text="It is $45, formerly $35."))
    assert not result.passed
    assert result.forbidden == ["35"]


def test_right_fact_with_the_wrong_citation_fails():
    result = score_case(LATE_CHECKOUT, make_answer(cited=("POL-LOY-001@v1#00",)))
    assert result.facts_ok
    assert not result.passed


def test_abstaining_on_an_answerable_question_fails():
    assert not score_case(LATE_CHECKOUT, make_answer(answerable=False)).passed


def test_unanswerable_question_passes_only_by_abstaining():
    assert score_case(PARKING, make_answer(answerable=False)).passed
    assert not score_case(PARKING, make_answer(text="Parking is free.")).passed


# ---------------------------------------------------------------- gates that run in CI


def test_bm25_recall_gate(retriever):
    """Needs no model, so it runs on every push: keyword search must keep finding the sources."""
    summary = eval_run.run_retrieval(CASES, retriever)["modes"]["bm25"]
    assert summary["recall_at_6"] >= 0.9, summary


def oracle_llm(messages) -> LLMReply:
    """A perfect model: it answers with the expected facts and cites an expected source."""
    user = messages[1]["content"]
    question = user.rsplit("Question: ", 1)[1]
    as_of = date.fromisoformat(re.search(r"Reference date: (\S+)", user).group(1))
    case = next(c for c in CASES if c.question == question and c.as_of == as_of)
    if not case.answerable:
        return LLMReply(text='{"answerable": false, "answer": "", "citations": []}')
    numbers = [
        int(number)
        for number, doc_id, version in re.findall(r"^\[(\d+)\] .*\((\S+) v(\d+),", user, re.M)
        if f"{doc_id}@v{version}" in case.expected_docs
    ]
    answer = " and ".join(pattern.split("|")[0] for pattern in case.must_include)
    reply = {"answerable": True, "answer": answer, "citations": numbers[:1]}
    return LLMReply(text=json.dumps(reply), prompt_tokens=50, completion_tokens=10)


def test_harness_gives_a_perfect_model_a_near_perfect_score(retriever):
    result = eval_run.run_generation(CASES, retriever, oracle_llm, "oracle")
    summary = result["summary"]
    assert summary["cases"] == 60
    assert summary["correct_abstention"] == 1.0
    assert summary["fact_accuracy"] >= 0.9  # the rest are retrieval misses, not model errors
    assert summary["citation_accuracy"] == 1.0


def test_report_lists_models_and_failed_cases(retriever, tmp_path, monkeypatch):
    monkeypatch.setattr(eval_run, "RESULTS_DIR", tmp_path)
    eval_run.save(eval_run.run_retrieval(CASES, retriever), "retrieval")
    eval_run.save(
        eval_run.run_generation(CASES[:5], retriever, oracle_llm, "oracle"), "generation-oracle"
    )
    report = eval_run.build_report()
    assert "| hybrid |" in report
    assert "| oracle | 100% |" in report
    assert "**oracle**: none" in report


def test_saved_results_were_produced_with_the_current_prompt_and_data(retriever):
    """Changing the prompt, the questions or the corpus makes the saved numbers stale."""
    for path in sorted(eval_run.RESULTS_DIR.glob("*.json")):
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved["kind"] == "retrieval":
            current = eval_run.fingerprint(retriever)
        else:
            current = eval_run.fingerprint(retriever, SYSTEM_PROMPT, saved["model"])
        assert saved["fingerprint"] == current, (
            f"{path.name} is stale: run the evals again (uv run poe eval / eval-retrieval)"
        )
