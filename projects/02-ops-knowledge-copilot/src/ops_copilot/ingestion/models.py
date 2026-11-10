"""Data contracts for documents and chunks."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DocType = Literal["sop", "policy", "supplier_contract", "memo"]
Status = Literal["active", "superseded", "expired"]


class DocumentMeta(BaseModel):
    """Front matter every document must carry. Unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str = Field(pattern=r"^[A-Z]+(-[A-Z0-9]+)+$")
    title: str = Field(min_length=3)
    doc_type: DocType
    version: int = Field(ge=1)
    status: Status
    effective_from: date
    effective_to: date | None = None
    department: str
    venue: str
    supersedes: str | None = None
    amends: str | None = None
    synthetic: bool

    @model_validator(mode="after")
    def _check_dates(self) -> "DocumentMeta":
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ValueError("effective_to is earlier than effective_from")
        if self.status != "active" and self.effective_to is None:
            raise ValueError(f"a {self.status} document needs an effective_to date")
        return self

    @property
    def doc_key(self) -> str:
        return f"{self.doc_id}@v{self.version}"


class Document(BaseModel):
    meta: DocumentMeta
    body: str
    source_file: str


class Chunk(BaseModel):
    """One retrievable unit. It carries the metadata needed to filter and to cite."""

    chunk_id: str
    doc_id: str
    version: int
    status: Status
    doc_type: DocType
    title: str
    section: str
    department: str
    venue: str
    effective_from: date
    effective_to: date | None
    source_file: str
    text: str
    n_words: int

    @property
    def embedding_text(self) -> str:
        """Text sent to the retriever: the section body prefixed with where it comes from."""
        return f"{self.title} > {self.section}\n{self.text}"
