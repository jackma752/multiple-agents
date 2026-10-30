"""Paper research tables (P6). Additive: ``Database.create_all`` creates them.

Migration contract: these are new tables only (no column added to an existing
table), so an older database gains them on start and an older build simply
ignores them. ``paper_schema`` records the paper schema version once; a future
breaking change must add a versioned migration instead of altering in place.

Text of papers, chunks, evidence quotes and drafts is research data: it stays
in the user's data directory database, never in events, logs or Git.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func
from sqlalchemy.types import JSON

from agenthub_kernel.db import Base

PAPER_SCHEMA_VERSION = 1


class PaperSchemaRow(Base):
    __tablename__ = "paper_schema"
    version = Column(Integer, primary_key=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ResearchTaskRow(Base):
    """One research request of a user; runs as stages of one root task tree."""

    __tablename__ = "paper_tasks"
    __table_args__ = (UniqueConstraint("user_id", "client_request_id"),)

    id = Column(String, primary_key=True)
    user_id = Column(String, index=True, nullable=False)
    client_request_id = Column(String, nullable=False)
    conversation_id = Column(ForeignKey("conversations.id"), index=True, nullable=False)
    root_task_id = Column(String, index=True, nullable=False)
    #: The stage now carrying this task (a new one after a re-authorization).
    stage_id = Column(String, nullable=True)
    request = Column(JSON, nullable=False)
    request_hash = Column(String, nullable=False)
    #: Input version: bumps only when the material set must be recomputed.
    input_version = Column(Integer, nullable=False, default=1)
    roles = Column(JSON, nullable=False)
    resource_rules = Column(JSON, nullable=False)
    resource_rules_hash = Column(String, nullable=False)
    phase = Column(String, nullable=False, default="plan")
    #: ``completed`` / ``blocked`` / ... (execution) and passed/failed/... (quality), separately.
    execution_status = Column(String, nullable=False, default="running")
    quality_status = Column(String, nullable=False, default="pending")
    outcome = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)


class PaperStepRow(Base):
    """A pipeline checkpoint: one model or tool step under a stable idempotency key.

    ``key`` = task / input version / step / paper or section / input hash. A
    succeeded step is reused, never re-run; a running tool step found after a
    restart is re-checked (its stored output verified) before anything repeats.
    """

    __tablename__ = "paper_steps"
    __table_args__ = (UniqueConstraint("task_id", "key"), {"sqlite_autoincrement": True})

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(ForeignKey("paper_tasks.id"), index=True, nullable=False)
    key = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # model | tool
    name = Column(String, nullable=False)
    target = Column(String, nullable=True)  # paper id / section id / query
    input_hash = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    invocation_ids = Column(JSON, nullable=False, default=list)
    template = Column(JSON, nullable=True)  # id, version, hash
    result = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class PaperRow(Base):
    __tablename__ = "paper_records"
    __table_args__ = (UniqueConstraint("task_id", "source", "source_id"),)

    id = Column(String, primary_key=True)
    task_id = Column(ForeignKey("paper_tasks.id"), index=True, nullable=False)
    source = Column(String, nullable=False)
    source_id = Column(String, nullable=False)  # arXiv id without version
    source_version = Column(String, nullable=True)
    title = Column(Text, nullable=False)
    authors = Column(JSON, nullable=False, default=list)
    year = Column(Integer, nullable=True)
    published = Column(String, nullable=True)
    venue = Column(String, nullable=True)
    doi = Column(String, nullable=True)
    url = Column(String, nullable=True)
    pdf_url = Column(String, nullable=True)
    abstract = Column(Text, nullable=True)
    categories = Column(JSON, nullable=False, default=list)
    queries = Column(JSON, nullable=False, default=list)
    merged = Column(JSON, nullable=False, default=list)  # other records merged into this one, with reason
    rank = Column(Integer, nullable=False, default=0)
    #: include / exclude / uncertain, why, which call decided it.
    screen = Column(String, nullable=True)
    screen_reason = Column(Text, nullable=True)
    screen_invocation_id = Column(String, nullable=True)
    included = Column(Boolean, nullable=False, default=False)
    fulltext_status = Column(String, nullable=False, default="unchecked")  # unchecked|available|missing|supplied
    fulltext_reason = Column(String, nullable=True)
    document_id = Column(String, nullable=True)
    read_depth = Column(String, nullable=True)
    read_status = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class PaperDocumentRow(Base):
    """A stored file for a paper (download, upload, local file or URL) and its parse result."""

    __tablename__ = "paper_documents"

    id = Column(String, primary_key=True)
    task_id = Column(ForeignKey("paper_tasks.id"), index=True, nullable=False)
    paper_id = Column(String, index=True, nullable=True)
    origin = Column(String, nullable=False)  # download | upload | local | url
    origin_ref = Column(Text, nullable=True)
    rel_path = Column(String, nullable=True)
    sha256 = Column(String, index=True, nullable=True)
    size = Column(Integer, nullable=True)
    content_type = Column(String, nullable=True)
    parse_status = Column(String, nullable=False, default="pending")
    parse_error = Column(Text, nullable=True)
    extractor = Column(String, nullable=True)
    pages = Column(Integer, nullable=True)
    chars = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class PaperChunkRow(Base):
    __tablename__ = "paper_chunks"
    __table_args__ = (UniqueConstraint("document_id", "seq"),)

    id = Column(String, primary_key=True)
    document_id = Column(ForeignKey("paper_documents.id"), index=True, nullable=False)
    paper_id = Column(String, index=True, nullable=True)
    seq = Column(Integer, nullable=False)
    page = Column(Integer, nullable=True)  # 1-based PDF page index; NULL when unknown
    char_start = Column(Integer, nullable=False)
    char_end = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    sha256 = Column(String, nullable=False)


class EvidenceRow(Base):
    __tablename__ = "paper_evidence"

    id = Column(String, primary_key=True)
    task_id = Column(ForeignKey("paper_tasks.id"), index=True, nullable=False)
    paper_id = Column(String, index=True, nullable=False)
    document_id = Column(String, nullable=True)
    chunk_id = Column(String, nullable=True)  # NULL for abstract-level evidence
    level = Column(String, nullable=False)  # fulltext | abstract
    kind = Column(String, nullable=False)
    statement = Column(Text, nullable=False)
    quote = Column(Text, nullable=False)
    location = Column(JSON, nullable=False, default=dict)
    attribution = Column(String, nullable=False, default="reported")  # reported | speculated | inferred
    verified = Column(Boolean, nullable=False, default=False)  # quote found verbatim in the source text
    invocation_id = Column(String, nullable=True)
    step_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class SupplementRequestRow(Base):
    """One summary request for missing full texts (per task input version)."""

    __tablename__ = "paper_supplement_requests"
    __table_args__ = (UniqueConstraint("task_id", "input_version"),)

    id = Column(String, primary_key=True)
    task_id = Column(ForeignKey("paper_tasks.id"), index=True, nullable=False)
    input_version = Column(Integer, nullable=False)
    policy = Column(String, nullable=False)
    status = Column(String, nullable=False)
    round = Column(Integer, nullable=False, default=1)
    #: Only 方案一's first question has one; cleared the moment the user chooses to supplement.
    deadline_at = Column(DateTime, nullable=True)
    first_asked_at = Column(DateTime, nullable=False)
    supplement_chosen_at = Column(DateTime, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision = Column(String, nullable=True)
    decided_by = Column(String, nullable=True)
    history = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class MissingItemRow(Base):
    __tablename__ = "paper_missing_items"
    __table_args__ = (UniqueConstraint("request_id", "paper_id"),)

    id = Column(String, primary_key=True)
    request_id = Column(ForeignKey("paper_supplement_requests.id"), index=True, nullable=False)
    paper_id = Column(String, nullable=False)
    number = Column(Integer, nullable=False)
    reference = Column(Text, nullable=False)
    metadata_snapshot = Column(JSON, nullable=False)
    reason = Column(String, nullable=False)
    abstract_available = Column(Boolean, nullable=False, default=False)
    state = Column(String, nullable=False, default="missing")  # missing | resolved | not_supplied
    document_id = Column(String, nullable=True)
    batch_id = Column(String, nullable=True)


class SupplementBatchRow(Base):
    __tablename__ = "paper_supplement_batches"
    __table_args__ = (UniqueConstraint("request_id", "round"),)

    id = Column(String, primary_key=True)
    request_id = Column(ForeignKey("paper_supplement_requests.id"), index=True, nullable=False)
    round = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="open")  # open | submitted | validated
    submitted_at = Column(DateTime, nullable=True)
    validated_at = Column(DateTime, nullable=True)
    result = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class SupplementFileRow(Base):
    __tablename__ = "paper_supplement_files"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(ForeignKey("paper_supplement_batches.id"), index=True, nullable=False)
    kind = Column(String, nullable=False)  # upload | local | url
    origin_ref = Column(Text, nullable=False)  # file name, path or URL as given
    target_paper_id = Column(String, nullable=True)
    rel_path = Column(String, nullable=True)
    sha256 = Column(String, nullable=True)
    size = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="received")
    detail = Column(Text, nullable=True)
    matched_paper_id = Column(String, nullable=True)
    document_id = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class SectionRow(Base):
    __tablename__ = "paper_sections"
    __table_args__ = (UniqueConstraint("task_id", "outline_version", "idx"),)

    id = Column(String, primary_key=True)
    task_id = Column(ForeignKey("paper_tasks.id"), index=True, nullable=False)
    outline_version = Column(Integer, nullable=False, default=1)
    idx = Column(Integer, nullable=False)
    title = Column(Text, nullable=False)
    goal = Column(Text, nullable=False, default="")
    evidence_ids = Column(JSON, nullable=False, default=list)
    gaps = Column(JSON, nullable=False, default=list)
    text = Column(Text, nullable=True)
    claims = Column(JSON, nullable=False, default=list)
    issues = Column(JSON, nullable=False, default=list)  # deterministic citation problems
    status = Column(String, nullable=False, default="pending")
    review_status = Column(String, nullable=False, default="pending")
    review = Column(JSON, nullable=True)
    revisions = Column(Integer, nullable=False, default=0)


class ArtifactRow(Base):
    __tablename__ = "paper_artifacts"
    __table_args__ = (UniqueConstraint("task_id", "version", "name"),)

    id = Column(String, primary_key=True)
    task_id = Column(ForeignKey("paper_tasks.id"), index=True, nullable=False)
    version = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    rel_path = Column(String, nullable=False)
    sha256 = Column(String, nullable=False)
    size = Column(Integer, nullable=False)
    final = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


class ProjectRevisionRow(Base):
    __tablename__ = "paper_project_revisions"
    __table_args__ = (UniqueConstraint("task_id", "version"),)
    id = Column(String, primary_key=True)
    task_id = Column(ForeignKey("paper_tasks.id"), index=True, nullable=False)
    version = Column(Integer, nullable=False)
    selected_candidate = Column(String, nullable=False, default="")
    notes = Column(JSON, nullable=False, default=dict)
    actor = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)


def ensure_schema(session) -> None:
    if session.get(PaperSchemaRow, PAPER_SCHEMA_VERSION) is None:
        session.add(PaperSchemaRow(version=PAPER_SCHEMA_VERSION))
