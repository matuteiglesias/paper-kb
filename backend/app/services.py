# backend/app/services.py
"""
Services layer for Paper-KB.

Principles:
 - adapter-first: every public function takes `storage` and calls only adapter methods.
 - small, testable functions that orchestrate and normalize using chunks_fs.normalize_chunk.
 - safe FS fallbacks only if the adapter lacks an implementation (migration path).
 - minimal side-effects (in-memory cache) and clear failure modes.

Expected StorageAdapter methods (recommended):
 - list_papers() -> List[Dict] or List[PaperMeta-like dicts]
 - list_chunks(paper_id, offset=0, limit=200, q=None) -> {"n": int, "chunks": [ {id,text,meta,...}, ... ]}
 - get_chunk(paper_id, chunk_id) -> Dict or None
 - semantic_search(q, k, paper_id=None) -> List[{"id","text","meta","score"}]
 - load_caches(), counts(), maybe_persist(), close(), save_summary_record(...)
"""
from __future__ import annotations
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import uuid

from backend.app import chunks_fs, papers_fs
from backend.app.schemas import (
    PaperMeta,
    PapersList,
    PaperChunksResponse,
    ChunkResponse,
    CanonicalChunk,
    SearchResponse,
    SearchHit,
    SummaryRequest,
    SummaryResult,
)
from fastapi import HTTPException
from backend.app.storage_adapter import StorageAdapter
logger = logging.getLogger("backend.app.services")

# Read caching belongs to the storage adapter (and its accepted corpus), not a
# process-global service variable shared across independent storage instances.

# --- custom exceptions for clearer control flow ---
class NotFoundError(Exception):
    pass


class ReadContractError(ValueError):
    pass


# -------------------------
# Helpers: normalization + small utilities
# -------------------------
def _normalize_paper_meta(raw: Dict[str, Any]) -> Optional[PaperMeta]:
    if not raw or not isinstance(raw, dict):
        return None
    try:
        # PaperMeta is a pydantic model; parse_obj keeps things tolerant
        if hasattr(PaperMeta, "parse_obj"):
            return PaperMeta.parse_obj(raw)
        return PaperMeta(**raw)
    except Exception:
        logger.debug("failed coercing paper meta: %r", raw)
        return None


def _normalize_chunks_from_upstream(upstreams: List[Dict[str, Any]], paper_id: str) -> List[CanonicalChunk]:
    """Preserve source identity; never silently relabel a passage to its route."""
    out: List[CanonicalChunk] = []
    for raw in upstreams:
        if not isinstance(raw, dict):
            raise ReadContractError("chunk record must be a mapping")
        meta = raw.get("meta") or raw.get("metadata") or {}
        if not isinstance(meta, dict):
            raise ReadContractError("chunk metadata must be a mapping")
        declared_pids = [x for x in (raw.get("paper_id"), meta.get("paper_id")) if x]
        if any(pid != paper_id for pid in declared_pids):
            raise ReadContractError("chunk paper_id conflicts with requested paper")
        declared_uids = [x for x in (raw.get("paper_uid"), meta.get("paper_uid")) if x]
        if len(set(declared_uids)) > 1:
            raise ReadContractError("chunk paper_uid conflicts with metadata")
        record = dict(raw)
        if "chunk_id" not in record and "id" in record:
            record["chunk_id"] = record["id"]
        try:
            chunk = chunks_fs.normalize_chunk(record, paper_id=paper_id)
        except (ValueError, TypeError) as exc:
            raise ReadContractError("invalid chunk read model") from exc
        out.append(chunk)
    return out


def _add_verified_parent_uid(storage: StorageAdapter, paper_id: str, chunks: List[CanonicalChunk]) -> None:
    """Supplement missing chunk UID only from the corresponding accepted paper."""
    if not chunks or not hasattr(storage, "get_paper"):
        return
    parent = storage.get_paper(paper_id) or {}
    if not isinstance(parent, dict):
        raise ReadContractError("invalid parent paper metadata")
    if parent.get("paper_id") not in (None, paper_id):
        raise ReadContractError("parent paper identity mismatch")
    uid = parent.get("paper_uid")
    if not uid:
        return
    for chunk in chunks:
        if chunk.paper_uid and chunk.paper_uid != uid:
            raise ReadContractError("chunk and paper canonical identities conflict")
        chunk.paper_uid = uid

def _canonical_to_chunkresponse(cc: CanonicalChunk) -> ChunkResponse:
    return ChunkResponse(
        chunk_id=cc.chunk_id,
        paper_id=cc.paper_id,
        paper_uid=cc.paper_uid,
        text=cc.text,
        chunk_index=cc.chunk_index,
        char_len=cc.char_len,
        header_path=cc.header_path,
        pages=cc.pages,
        meta=cc.meta,
    )


# -------------------------
# Papers: refresh / list (adapter-first)
# -------------------------
def refresh_papers_cache(storage: StorageAdapter) -> List[PaperMeta]:
    """Explicit refresh, without retaining stale process-global cross-corpus state."""
    if hasattr(storage, "load_caches"):
        storage.load_caches()
    return list_papers(storage)


def list_papers(storage: StorageAdapter, prefer_cache: bool = True) -> List[PaperMeta]:
    """Read from the selected adapter; never reuse papers from another corpus.

    prefer_cache is retained for callers; the adapter owns its cache lifetime.
    """
    if hasattr(storage, "list_papers"):
        raw = storage.list_papers() or []
    else:
        # Legacy-only compatibility for adapters that lack a paper-list method.
        raw = papers_fs.list_papers_from_fs() or []
    normalized: List[PaperMeta] = []
    seen: set[str] = set()
    for record in raw:
        paper = _normalize_paper_meta(record)
        if paper is None:
            continue
        pid = (paper.paper_id or "").strip()
        if pid and pid not in seen:
            seen.add(pid)
            normalized.append(paper)
    return normalized


def filter_papers(
    papers: List[PaperMeta], *, q: str = "", year: Optional[int] = None,
    venue: str = "", tag: str = "",
) -> List[PaperMeta]:
    """Small, deterministic, metadata-only filters; no inferred missing fields."""
    query = q.strip().casefold()
    venue_term = venue.strip().casefold()
    tag_term = tag.strip().casefold()
    result: List[PaperMeta] = []
    for paper in papers:
        if year is not None and paper.year != year:
            continue
        if venue_term and venue_term not in (paper.venue or "").casefold():
            continue
        if tag_term and tag_term not in {t.casefold() for t in (paper.tags or [])}:
            continue
        if query:
            searchable = [
                paper.paper_id, paper.paper_uid, paper.title, paper.abstract,
                paper.venue, paper.doi, *(paper.authors or []),
            ]
            if not any(query in (value or "").casefold() for value in searchable):
                continue
        result.append(paper)
    return sorted(result, key=lambda p: (p.title.casefold(), p.paper_id))

# -------------------------
# Chunk accessors
# -------------------------
def get_paper_chunks(storage: StorageAdapter, paper_id: str, offset: int = 0, limit: int = 200, q: Optional[str] = None) -> PaperChunksResponse:
    """
    Primary: ask storage.list_chunks which should support server-side pagination.
    Adapter must return {"n": int, "chunks": [ {id,text,meta}, ... ]}.
    If adapter lacks list_chunks, fall back to FS (chunks_fs.read_chunks_as_models) and implement offset/limit there.
    """
    # validate
    if offset < 0 or limit <= 0:
        raise HTTPException(status_code=400, detail="invalid offset/limit")

    # Try adapter pagination
    try:
        if hasattr(storage, "list_chunks"):
            res = storage.list_chunks(paper_id=paper_id, offset=offset, limit=limit, q=q)
            if not isinstance(res, dict):
                raise ValueError("storage.list_chunks must return dict with 'n' and 'chunks'")
            raw_chunks = res.get("chunks", []) or []
            total = int(res.get("n", len(raw_chunks)))
            # normalize upstream chunk dicts to canonical
            canonical = _normalize_chunks_from_upstream(raw_chunks, paper_id)
            _add_verified_parent_uid(storage, paper_id, canonical)
            # ensure stable sort by chunk_index then return API objects
            canonical.sort(key=lambda x: int(x.chunk_index or 0))
            paged = canonical  # adapter already paginated, so trust it
            return PaperChunksResponse(paper_id=paper_id, total=total, chunks=[_canonical_to_chunkresponse(c) for c in paged])
    except ReadContractError as exc:
        raise HTTPException(status_code=500, detail="invalid chunk read contract") from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("storage.list_chunks failed for %s", paper_id)
        raise HTTPException(status_code=503, detail="chunk storage unavailable") from exc

    # FS fallback: try to use an indexed reader (chunks_fs.get_chunk_by_id or read_chunks_as_models)
    try:
        if hasattr(chunks_fs, "read_chunks_as_models"):
            models = chunks_fs.read_chunks_as_models(paper_id) or []
            # models may be CanonicalChunk instances or dicts; coerce minimally to dict then normalize
            upstreams = []
            for m in models:
                if hasattr(m, "model_dump"):
                    upstreams.append(m.model_dump())
                elif hasattr(m, "dict"):
                    upstreams.append(m.dict())
                elif isinstance(m, dict):
                    upstreams.append(m)
                else:
                    upstreams.append({
                        "chunk_id": getattr(m, "chunk_id", None),
                        "text": getattr(m, "text", None),
                        "chunk_index": getattr(m, "chunk_index", None),
                        "char_len": getattr(m, "char_len", None),
                        "meta": getattr(m, "meta", None),
                    })
            total = len(upstreams)
            # apply q filter if requested (simple substring on text)
            if q:
                qlow = q.lower()
                upstreams = [u for u in upstreams if qlow in ( (u.get("text") or u.get("preview") or "").lower() )]
                total = len(upstreams)
            # slice (source didn't support pagination)
            slice_upstreams = upstreams[offset: offset + limit]
            canonical = _normalize_chunks_from_upstream(slice_upstreams, paper_id)
            canonical.sort(key=lambda x: int(x.chunk_index or 0))
            return PaperChunksResponse(paper_id=paper_id, total=total, chunks=[_canonical_to_chunkresponse(c) for c in canonical])
    except Exception:
        logger.exception("chunks_fs fallback failed for %s", paper_id)

    # Nothing found -> empty stable response
    return PaperChunksResponse(paper_id=paper_id, total=0, chunks=[])


def get_chunk(storage, paper_id: str, chunk_id: str) -> Optional[ChunkResponse]:
    """Adapter-first retrieval, with no cross-source fallback on adapter failures."""
    if hasattr(storage, "get_chunk") or hasattr(storage, "get_chunk_by_id"):
        try:
            if hasattr(storage, "get_chunk"):
                record = storage.get_chunk(paper_id=paper_id, chunk_id=chunk_id)
            else:
                record = storage.get_chunk_by_id(chunk_id, paper_id=paper_id)
            if not record:
                return None
            chunks = _normalize_chunks_from_upstream([record], paper_id)
            _add_verified_parent_uid(storage, paper_id, chunks)
            if len(chunks) != 1 or chunks[0].chunk_id != chunk_id:
                raise ReadContractError("chunk identity does not match request")
            return _canonical_to_chunkresponse(chunks[0])
        except ReadContractError as exc:
            raise HTTPException(status_code=500, detail="invalid chunk read contract") from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("storage.get_chunk failed for %s/%s", paper_id, chunk_id)
            raise HTTPException(status_code=503, detail="chunk storage unavailable") from exc

    # Compatibility only for storage backends without an individual-chunk API.
    if hasattr(chunks_fs, "get_chunk_text"):
        text = chunks_fs.get_chunk_text(paper_id, chunk_id)
        if text is not None:
            chunks = _normalize_chunks_from_upstream(
                [{"chunk_id": chunk_id, "text": text}], paper_id
            )
            return _canonical_to_chunkresponse(chunks[0])
    return None

# -------------------------
# Search (semantic) - adapter-driven
# -------------------------
def search(storage, q: str, k: int = 6, paper_id: Optional[str] = None) -> SearchResponse:
    """Return identifiable hits or an explicit failure, never a fake empty success."""
    if not q:
        raise HTTPException(status_code=400, detail="empty query")

    try:
        if hasattr(storage, "semantic_search"):
            hits = storage.semantic_search(q=q, k=k, paper_id=paper_id) or []
        elif hasattr(storage, "search"):
            hits = storage.search(q=q, k=k, paper_id=paper_id) or []
        else:
            raise NotImplementedError("search not supported by storage")
    except Exception as exc:
        logger.exception("storage search failed")
        raise HTTPException(status_code=503, detail="search storage unavailable") from exc

    normalized: List[SearchHit] = []
    parent_uids: Dict[str, Optional[str]] = {}
    for hit in hits:
        if not isinstance(hit, dict):
            raise HTTPException(status_code=500, detail="invalid search result")
        meta = hit.get("meta") or {}
        if not isinstance(meta, dict):
            raise HTTPException(status_code=500, detail="invalid search result")
        chunk_id = hit.get("chunk_id") or hit.get("id")
        pids = [x for x in (hit.get("paper_id"), meta.get("paper_id")) if x]
        uids = [x for x in (hit.get("paper_uid"), meta.get("paper_uid")) if x]
        if not chunk_id or len(set(pids)) > 1 or len(set(uids)) > 1:
            raise HTTPException(status_code=500, detail="inconsistent search hit identity")
        if paper_id and (not pids or pids[0] != paper_id):
            raise HTTPException(status_code=500, detail="inconsistent search hit identity")

        # A paper-level canonical UID can supplement legacy hits, but only if
        # the accepted storage record confirms the same originating paper.
        resolved_uid = uids[0] if uids else None
        if pids and hasattr(storage, "get_paper"):
            pid = pids[0]
            if pid not in parent_uids:
                try:
                    parent = storage.get_paper(pid) or {}
                except Exception as exc:
                    logger.exception("paper identity lookup failed for search hit")
                    raise HTTPException(status_code=503, detail="search storage unavailable") from exc
                if not isinstance(parent, dict) or parent.get("paper_id") not in (None, pid):
                    raise HTTPException(status_code=500, detail="inconsistent search hit identity")
                parent_uids[pid] = parent.get("paper_uid")
            known_uid = parent_uids[pid]
            if resolved_uid and known_uid and resolved_uid != known_uid:
                raise HTTPException(status_code=500, detail="inconsistent search hit identity")
            resolved_uid = resolved_uid or known_uid
        try:
            normalized.append(SearchHit(
                id=str(hit.get("id") or chunk_id),
                chunk_id=str(chunk_id),
                paper_id=pids[0] if pids else None,
                paper_uid=resolved_uid,
                text=hit.get("text") or hit.get("preview") or "",
                score=float(hit["score"]) if hit.get("score") is not None else None,
                meta=meta,
            ))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=500, detail="invalid search result") from exc
    return SearchResponse(query=q, k=k, hits=normalized)

# -------------------------
# Summaries (lightweight job API)
# -------------------------
def _llm_summarize_heuristic(texts: List[str], req: SummaryRequest) -> str:
    """
    Minimal deterministic summarizer for fallback: concatenates and truncates.
    Replace with real LLM call (openai/etc.) in a controlled way.
    """
    joined = "\n\n".join(t.strip() for t in texts if t)
    max_chars = 2000
    if len(joined) <= max_chars:
        return joined
    return joined[:max_chars].rsplit("\n", 1)[0] + " ..."

def create_summary_task(storage, req: SummaryRequest, background_tasks: Optional[Any] = None) -> str:
    """
    Create and optionally queue a summary job.
    - If background_tasks provided (FastAPI BackgroundTasks), schedule process_summary_job and return task id immediately.
    - Otherwise run synchronously (blocking) and return summary_id when finished.
    """
    summary_id = str(uuid.uuid4())
    created_at = datetime.utcnow().timestamp()
    # Save stub record if adapter supports it
    try:
        if hasattr(storage, "save_summary_record"):
            storage.save_summary_record(summary_id, {"status": "queued", "request": req.dict(), "created_at": created_at})
    except Exception:
        logger.exception("storage.save_summary_record failed (stub)")

    if background_tasks:
        # schedule background worker
        background_tasks.add_task(process_summary_job, storage, summary_id, req)
        return summary_id

    # synchronous path (blocking)
    process_summary_job(storage, summary_id, req)
    return summary_id


def process_summary_job(storage, summary_id: str, req: SummaryRequest) -> None:
    """
    Worker: retrieve top-k chunks (semantic_search) -> call LLM (or heuristic) -> persist result via storage.save_summary_record.
    Keep this function idempotent-friendly: if storage.save_summary_record raises, log and continue.
    """
    try:
        # 1) retrieval
        k = int(req.k or 6)
        q = req.q or ""
        paper_id = req.paper_id
        texts = []
        try:
            hits = []
            if hasattr(storage, "semantic_search"):
                hits = storage.semantic_search(q=q, k=k, paper_id=paper_id) or []
            elif hasattr(storage, "search"):
                hits = storage.search(q=q, k=k, paper_id=paper_id) or []
            # extract snippet texts
            for h in hits:
                t = h.get("text") or (h.get("meta") or {}).get("preview") or ""
                if t:
                    texts.append(t)
        except Exception:
            logger.exception("semantic retrieval failed for summary job %s", summary_id)

        # 2) LLM / heuristic
        answer = _llm_summarize_heuristic(texts, req)

        # 3) write result
        result = {
            "summary_id": summary_id,
            "request": req.dict() if hasattr(req, "dict") else dict(req),
            "answer": answer,
            "rag": {"support": [h.get("id") or h.get("chunk_id") for h in (hits or [])], "n_support": len(hits or [])},
            "created_at": datetime.utcnow().timestamp(),
            "status": "done",
        }
        try:
            if hasattr(storage, "save_summary_record"):
                storage.save_summary_record(summary_id, result)
            else:
                # fallback: write to filesystem via papers_fs or STORE_SUMMARIES_DIR could be used by adapter; avoid adding coupling here
                logger.info("summary job completed (no storage.save_summary_record), summary_id=%s", summary_id)
        except Exception:
            logger.exception("failed persisting summary result for %s", summary_id)

    except Exception:
        logger.exception("unhandled exception in process_summary_job %s", summary_id)


# -------------------------
# Health helper
# -------------------------
def health(storage) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "backend": getattr(storage, "backend_name", None),
        "persisted": bool(getattr(storage, "persisted", False)),
        "cache_ready": bool(getattr(storage, "cache_ready", False)),
    }
    try:
        if hasattr(storage, "counts"):
            info.update(storage.counts())
        elif hasattr(storage, "n_papers"):
            info["n_papers"] = int(getattr(storage, "n_papers"))
    except Exception:
        logger.exception("failed getting storage counts")
    return info


def get_paper_detail(storage: StorageAdapter, paper_id: str) -> PaperMeta:
    paper_id = (paper_id or '').strip()
    if not paper_id:
        raise NotFoundError('paper not found')

    try:
        if hasattr(storage, 'get_paper'):
            raw = storage.get_paper(paper_id) or {}
            pm = _normalize_paper_meta(raw)
            if pm is not None:
                return pm
    except Exception:
        logger.exception('storage.get_paper failed for %s', paper_id)

    for p in list_papers(storage, prefer_cache=False):
        if p.paper_id == paper_id:
            return p

    raise NotFoundError('paper not found')
