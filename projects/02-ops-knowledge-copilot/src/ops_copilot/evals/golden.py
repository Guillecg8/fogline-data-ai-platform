"""The golden set: questions with the sources and facts a correct answer must contain."""

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ops_copilot.paths import PROJECT_DIR

EVALS_DIR = PROJECT_DIR / "evals"
GOLDEN_FILE = EVALS_DIR / "golden.jsonl"
DEFAULT_AS_OF = date(2026, 11, 10)  # fixed, so results do not depend on the day you run them

Category = Literal[
    "direct", "multi", "versioned", "temporal", "conflict", "unanswerable", "injection"
]


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: Category
    question: str
    as_of: date = DEFAULT_AS_OF
    answerable: bool = True
    expected_docs: list[str] = []  # any of these documents (doc_id@vN) supports the answer
    must_include: list[str] = []  # every item must appear; "a|b" means either a or b
    must_not_include: list[str] = []  # none may appear: stale values, injected text


def load_golden(path: Path = GOLDEN_FILE) -> list[GoldenCase]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [GoldenCase.model_validate_json(line) for line in lines if line.strip()]
