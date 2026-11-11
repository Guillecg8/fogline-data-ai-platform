import json
from datetime import date

import httpx
import pytest

from ops_copilot.generation.answer import answer_question, build_messages, parse_output
from ops_copilot.generation.llm import ChatClient, LLMReply
from ops_copilot.ingestion import pipeline
from ops_copilot.paths import CORPUS_DIR
from ops_copilot.retrieval.embedding import hashing_embedder
from ops_copilot.retrieval.hybrid import HybridRetriever, load_chunks

TODAY = date(2026, 11, 10)
QUESTION = "What is the fee for late check-out?"


class FakeLLM:
    """Returns scripted replies and records the prompts it received."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages):
        self.calls.append(messages)
        return LLMReply(text=self.replies.pop(0), prompt_tokens=100, completion_tokens=20)


@pytest.fixture(scope="module")
def retriever(tmp_path_factory) -> HybridRetriever:
    data_dir = tmp_path_factory.mktemp("data")
    pipeline.run(CORPUS_DIR, data_dir)
    chunks, _ = load_chunks(data_dir / "gold" / "chunks.jsonl")
    return HybridRetriever(chunks, hashing_embedder())


def reply(answerable=True, answer="The fee is $45.", citations=(1,)) -> str:
    return json.dumps({"answerable": answerable, "answer": answer, "citations": list(citations)})


def test_prompt_numbers_the_sources_and_shows_their_dates(retriever):
    hits = retriever.search(QUESTION, as_of=TODAY)
    user = build_messages(QUESTION, hits, TODAY)[1]["content"]
    assert "Reference date: 2026-11-10" in user
    assert "[1] Check-in and Check-out (SOP-FD-001 v1" in user
    assert "effective from 2025-04-01" in user


def test_answer_carries_citations_that_point_to_real_chunks(retriever):
    answer = answer_question(QUESTION, retriever, FakeLLM(reply()), as_of=TODAY)
    assert answer.answerable
    assert answer.text == "The fee is $45."
    assert answer.citations[0].doc_id == "SOP-FD-001"
    assert answer.citations[0].chunk_id == answer.retrieved[0]
    assert answer.prompt_tokens == 100


def test_model_can_decline(retriever):
    answer = answer_question(QUESTION, retriever, FakeLLM(reply(answerable=False)), as_of=TODAY)
    assert not answer.answerable
    assert answer.abstain_reason == "model_declined"
    assert "could not find" in answer.text


def test_answer_without_a_valid_citation_is_not_shown(retriever):
    llm = FakeLLM(reply(answer="Parking is free.", citations=(99,)))
    answer = answer_question(QUESTION, retriever, llm, as_of=TODAY)
    assert answer.abstain_reason == "uncited"
    assert "Parking" not in answer.text


def test_invalid_output_is_retried_once(retriever):
    llm = FakeLLM("Sure! The fee is 45 dollars.", reply())
    answer = answer_question(QUESTION, retriever, llm, as_of=TODAY)
    assert answer.answerable
    assert len(llm.calls) == 2
    assert answer.prompt_tokens == 200


def test_two_invalid_outputs_mean_abstention(retriever):
    answer = answer_question(QUESTION, retriever, FakeLLM("nope", "still nope"), as_of=TODAY)
    assert answer.abstain_reason == "invalid_output"


def test_no_sources_means_no_model_call(retriever):
    llm = FakeLLM()
    answer = answer_question(QUESTION, retriever, llm, as_of=date(2020, 1, 1))
    assert answer.abstain_reason == "no_sources"
    assert llm.calls == []


def test_parse_output_tolerates_code_fences():
    output = parse_output('```json\n{"answerable": true, "answer": "ok", "citations": [2]}\n```')
    assert output is not None
    assert output.citations == [2]


def test_chat_client_speaks_the_openai_protocol():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
        )

    client = ChatClient(
        base_url="http://localhost:11434/v1", model="tiny", transport=httpx.MockTransport(handler)
    )
    result = client([{"role": "user", "content": "hi"}])
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["body"]["model"] == "tiny"
    assert seen["body"]["temperature"] == 0
    assert result == LLMReply(text="hello", prompt_tokens=7, completion_tokens=3)


def test_numeric_answers_are_accepted_exactly_as_written():
    output = parse_output('{"answerable": true, "answer": 9.80, "citations": [1]}')
    assert output is not None
    assert output.answer == "9.80"
    assert output.citations == [1]


def test_raw_model_output_is_kept_for_debugging(retriever):
    answer = answer_question(QUESTION, retriever, FakeLLM("nope", "still nope"), as_of=TODAY)
    assert answer.raw_output == "still nope"
