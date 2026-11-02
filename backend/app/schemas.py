# backend/app/schemas.py
from __future__ import annotations
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime

# ------------------------------
# Basic aliases
# ------------------------------
PageRange = Tuple[Optional[int], Optional[int]]


# ------------------------------
# Canonical domain models (single source of truth)
# ------------------------------
class PaperMeta(BaseModel):
    paper_id: str
    paper_uid: Optional[str] = None
    title: str
    authors: Optional[List[str]] = None
    n_chunks: int
    preview: Optional[str] = None            # short text, capped at 2000 chars
    pages: Optional[int] = None              # total pages (paper-level)
    source_file: Optional[str] = None
    abstract: Optional[str] = None
    date: Optional[str] = None
    year: Optional[int] = None
    venue: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    tags: Optional[List[str]] = None
    created_at: Optional[datetime] = None
    pipeline_version: Optional[str] = None   # e.g., git sha or pipeline tag
    embed_model: Optional[str] = None        # model used for embeddings (if any)

    # @validator("preview")
    def cap_preview(cls, v):
        if v is None:
            return v
        return v[:2000]


class CanonicalChunk(BaseModel):
    chunk_id: str
    paper_id: str
    paper_uid: Optional[str] = None
    text: str
    chunk_index: int = Field(..., ge=0)
    char_len: int
    header_path: Optional[List[str]] = None      # structured: [paper_title, section_title, subsection...]
    pages: Optional[PageRange] = None            # (first_page, last_page)
    meta: Optional[Dict[str, Any]] = None        # small provenance map

    # @validator("text")
    def trim_text(cls, v: str):
        return v.strip()

    # @validator("char_len", pre=True, always=True)
    def ensure_charlen(cls, v, values):
        if v is None:
            txt = values.get("text") or ""
            return len(txt)
        return int(v)


class EmbeddingRecord(BaseModel):
    chunk_id: str
    embedding: List[float]
    dim: int
    embed_model: Optional[str] = None
    created_at: Optional[datetime] = None


# ------------------------------
# Search / Summary / Topic models (API-level)
# ------------------------------
class SearchRequest(BaseModel):
    q: str
    k: Optional[int] = Field(6, ge=1, le=200)
    paper_id: Optional[str] = None


class SummaryRequest(BaseModel):
    q: Optional[str] = None
    paper_id: Optional[str] = None
    k: Optional[int] = 6


class SearchHit(BaseModel):
    id: str
    text: str
    score: Optional[float] = None
    meta: Optional[Dict[str, Any]] = None
    # optionally canonical snippet fields:
    chunk_id: Optional[str] = None
    paper_id: Optional[str] = None
    paper_uid: Optional[str] = None


class SearchResponse(BaseModel):
    query: str
    k: int
    hits: List[SearchHit]




class CorpusInfoResponse(BaseModel):
    corpus_name: Optional[str] = None
    storage_backend: str
    chunk_sets_dir: Optional[str] = None
    cache_ready: bool
    loaded_at: Optional[float] = None


class CorpusHealthResponse(BaseModel):
    status: str
    n_papers: int
    n_chunks: int
    n_artifacts: int
    n_invalid_artifacts: int
    n_skipped_chunks: int = 0
    dedupe_collisions: int = 0
    warnings: List[str] = []


class SearchV1Response(BaseModel):
    capability: str
    query: str
    k: int
    hits: List[SearchHit]
class SummaryResult(BaseModel):
    summary_id: str
    request: Dict[str, Any]
    answer: str
    rag: Dict[str, Any]         # e.g. { "support": [...], "used_chunk_ids": [...] }
    created_at: float


class Topic(BaseModel):
    topic_id: str
    label: str
    count: int
    key_phrases: Optional[List[str]] = None
    top_papers: Optional[List[Dict[str, Any]]] = None  # [{paper_id, title}]
    top_snippets: Optional[List[Dict[str, Any]]] = None  # [{chunk_id, paper_id, text}]



class SummaryGenerateRequest(BaseModel):
    provider: str = "mock"
    force: bool = False
    agent_mode: str = "client"


class SummaryTask(BaseModel):
    task_id: str
    status: str  # queued|in_progress|done|failed
    request: Optional[Dict[str, Any]] = None
    result: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ------------------------------
# API wrapper shapes (convenience)
#                 (these are the canonical HTTP shapes your frontend currently expects)
# ------------------------------
class ChunkResponse(BaseModel):
    chunk_id: str
    paper_id: Optional[str] = None
    paper_uid: Optional[str] = None
    text: str
    chunk_index: int
    char_len: int
    header_path: Optional[List[str]] = None
    pages: Optional[PageRange] = None
    meta: Optional[Dict[str, Any]] = None


class PapersList(BaseModel):
    papers: List[PaperMeta]
    total: Optional[int] = None


class PaperChunksResponse(BaseModel):
    paper_id: str
    total: int
    chunks: List[ChunkResponse]


# ------------------------------
# Helpers: convert canonical -> API shapes
# ------------------------------
def canonical_to_api_chunk(c: CanonicalChunk, include_id_alias: bool = True) -> Dict[str, Any]:
    """
    Convert a CanonicalChunk into the small API chunk shape frontend expects.
    By default includes both 'chunk_id' (canonical) and 'id' (alias) for compatibility.
    """
    out = {
        "chunk_id": c.chunk_id,
        "paper_uid": c.paper_uid,
        "text": c.text,
        "chunk_index": c.chunk_index,
        "char_len": c.char_len,
        "header_path": c.header_path,
        "pages": c.pages,
        "meta": c.meta or {},
        "paper_id": c.paper_id,
    }
    if include_id_alias:
        out["id"] = c.chunk_id
    return out


def canonical_paper_to_api(p: PaperMeta) -> Dict[str, Any]:
    """Return dict matching API PaperMeta shape (snake_case)."""
    return {
        "paper_id": p.paper_id,
        "paper_uid": p.paper_uid,
        "title": p.title,
        "authors": p.authors,
        "n_chunks": p.n_chunks,
        "preview": p.preview,
        "pages": p.pages,
        "source_file": p.source_file,
        "abstract": p.abstract,
        "date": p.date,
        "year": p.year,
        "venue": p.venue,
        "doi": p.doi,
        "arxiv_id": p.arxiv_id,
        "tags": p.tags,
        "created_at": p.created_at.isoformat() if isinstance(p.created_at, datetime) else p.created_at,
        "pipeline_version": p.pipeline_version,
        "embed_model": p.embed_model,
    }
