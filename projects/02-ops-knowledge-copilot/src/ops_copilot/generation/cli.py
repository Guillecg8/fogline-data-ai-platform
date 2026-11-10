"""Ask the copilot from the terminal.

uv run poe ask "What is the fee for late check-out?"
uv run poe ask "How many hours before arrival can a guest cancel for free?" --as-of 2025-06-01
"""

import argparse
from datetime import date

import httpx

from ops_copilot.generation.answer import answer_question
from ops_copilot.generation.llm import ChatClient
from ops_copilot.net import use_system_certificates
from ops_copilot.retrieval.cli import build_retriever


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask Fogline's operations copilot.")
    parser.add_argument("question")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()

    use_system_certificates()
    llm = ChatClient()
    try:
        answer = answer_question(args.question, build_retriever(), llm, as_of=args.as_of)
    except httpx.ConnectError:
        raise SystemExit(
            f"Cannot reach the model at {llm.base_url}. Is Ollama running? Try: ollama list"
        ) from None

    print(f"\nQ: {answer.question}  (as of {answer.as_of})")
    print(f"A: {answer.text}")
    if answer.abstain_reason:
        print(f"   abstained: {answer.abstain_reason}")
    for c in answer.citations:
        print(f"   [{c.number}] {c.doc_id} v{c.version} - {c.title} > {c.section}")
    tokens = answer.prompt_tokens + answer.completion_tokens
    print(f"\nmodel={llm.model} latency={answer.latency_ms} ms tokens={tokens}")


if __name__ == "__main__":
    main()
