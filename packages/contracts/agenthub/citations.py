"""Additive, backend-derived citation provenance; never grants source access."""
from typing import TypedDict


class CitationLocation(TypedDict):
    method: str  # exact | relocated_exact
    declared_chunk_id: str
    chunk_sha256: str
    quote_sha256: str
    chunk_start: int
    chunk_end: int


class CitationIssue(TypedDict):
    unit_id: str
    source_index: int
    chunk_id: str
    code: str
    message: str
