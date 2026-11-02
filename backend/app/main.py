# backend/app/main.py
"""Paper-KB FastAPI backend (refactored, minimal, robust).

This version centralizes storage access behind a StorageAdapter created at startup.
It keeps the API surface minimal and delegates data access + normalization to:
 - backend.app.storage_adapter (adapter factory + adapters)
 - backend.app.services (business logic)
 - backend.app.chunks_fs and backend.app.papers_fs (authoritative FS helpers)

Usage:
  CHROMA_DIR=./store/chroma CHROMA_COLLECTION=chunks uvicorn backend.app.main:app --reload --port 9000
"""
from __future__ import annotations
import os
import asyncio
import sys
import time
import json
import threading
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import uvicorn
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, Query
from fastapi.middleware.cors import CORSMiddleware

# Ensure repo root in path so we can import local modules
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Project config + helpers (existing modules)
from shared.config import CHROMA_DIR, SUMMARY_DB, chroma_collection_name, STORE_SUMMARIES_DIR

# Domain schemas (API-level)
from backend.app.schemas import (
    PaperMeta,
    PapersList,
    CanonicalChunk,
    ChunkResponse,
    PaperChunksResponse,
    SummaryRequest,
    SearchRequest,
    SummaryResult,
    SearchResponse,
    SearchHit,
    CorpusInfoResponse,
    CorpusHealthResponse,
    SearchV1Response,
    SummaryGenerateRequest,
    canonical_to_api_chunk,
)

# Services (business logic) - these should be adapted to accept a storage adapter
from backend.app import services
from backend.exports.generate_summaries import generate_summary_for_paper
from backend.exports.generate_summaries import _provider as summary_provider_factory
from backend.exports.summary_artifacts import summary_path, is_canonical_paper_id
from pipeline.corpus import resolve_corpus_paths

# FS normalizer kept authoritative
from backend.app.chunks_fs import normalize_chunk

# Adapter factory and types (create_adapter_from_env should return a StorageAdapter)
# Implement backend.app.storage_adapter.JsonlAdapter and create_adapter_from_env next.
try:
    from backend.app.storage_adapter import create_adapter_from_env, StorageAdapter, JsonlAdapter
except Exception:
    # If the adapter module doesn't exist yet, we'll still try to import JsonlAdapter as a fallback.
    create_adapter_from_env = None
    StorageAdapter = object  # type: ignore
    try:
        from backend.app.storage_adapter import JsonlAdapter  # type: ignore
    except Exception:
        JsonlAdapter = None  # type: ignore

# Basic logging
logger = logging.getLogger("backend.app.main")
logging.basicConfig(level=logging.INFO)

# FastAPI app
app = FastAPI(
    title="Paper-KB API",
    description="Paper-KB: fast prototype backend exposing Chroma-powered paper/chunk APIs, search and summarization jobs.",
    version="0.1.0",
    contact={"name": "Matias Iglesias", "email": "you@example.com"},
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# -------------------------------------------------------------------
# Helper: storage accessor dependency
# -------------------------------------------------------------------
def get_storage() -> "StorageAdapter":
    st = getattr(app.state, "storage", None)
    if st is None:
        # Not initialized or failed to create adapter
        raise HTTPException(status_code=503, detail="storage adapter not initialized")
    return cast("StorageAdapter", st)

# -------------------------------------------------------------------
# Startup / Shutdown: create adapter and warm caches (background)
# -------------------------------------------------------------------
@app.on_event("startup")
def _startup():
    """
    - Create adapter using factory (create_adapter_from_env) if available, otherwise fall back to JsonlAdapter.
    - Attach adapter to app.state.storage
    - Start background thread to run storage.load_caches() so startup doesn't block.
    """
    logger.info("[startup] initializing storage adapter (STORAGE_BACKEND=%s)", os.getenv("STORAGE_BACKEND"))
    storage = None
    try:
        if create_adapter_from_env:
            storage = create_adapter_from_env()
        else:
            # fallback: try JsonlAdapter if available
            if JsonlAdapter:
                storage = JsonlAdapter()
            else:
                raise RuntimeError("no storage adapter available; implement backend.app.storage_adapter")
    except Exception as e:
        logger.exception("[startup] failed to instantiate storage adapter: %s", e)
        # still attach a dummy object so get_storage returns 503 instead of AttributeError
        app.state.storage = None
        app.state.cache_ready = False
        return

    app.state.storage = storage
    app.state.cache_ready = False

    def _warm():
        logger.info("[startup] background cache warm-up starting")
        try:
            if hasattr(storage, "load_caches"):
                storage.load_caches()
            # adapter may expose persisted/readiness flags
            app.state.cache_ready = bool(getattr(storage, "cache_ready", True))
            logger.info("[startup] background cache warm-up finished (cache_ready=%s)", app.state.cache_ready)
        except Exception as e2:
            logger.exception("[startup] storage.load_caches failed: %s", e2)
            app.state.cache_ready = False

    t = threading.Thread(target=_warm, daemon=True)
    t.start()
    logger.info("[startup] storage adapter attached; caches loading in background")

@app.on_event("shutdown")
def _shutdown():
    """Attempt graceful adapter shutdown and persistence."""
    logger.info("[shutdown] shutting down storage adapter (if present)")
    st = getattr(app.state, "storage", None)
    if not st:
        logger.info("[shutdown] no storage adapter to close")
        return
    try:
        if hasattr(st, "maybe_persist"):
            try:
                st.maybe_persist()
            except Exception:
                logger.exception("[shutdown] maybe_persist failed; continuing to close")
        if hasattr(st, "close"):
            try:
                st.close()
            except Exception:
                logger.exception("[shutdown] storage.close failed")
    except Exception:
        logger.exception("[shutdown] unexpected error during storage shutdown")

# -------------------------------------------------------------------
# Health endpoint
# -------------------------------------------------------------------
@app.get("/", tags=["health"])
def root():
    return {"status": "ok", "message": "paper-kb backend running"}

@app.get("/api/_admin/papers_health", tags=["health"])
def papers_health(storage: StorageAdapter = Depends(get_storage)):
    """
    Returns a small health summary. Adapter should expose:
      - backend_name (str)
      - persisted (bool) or similar
      - optional counts: n_papers, n_chunks_sample (int)
    Adapter implementations should make best-effort to return this info.
    """
    info = {
        "cache_ready": bool(getattr(app.state, "cache_ready", False)),
        "storage_backend": getattr(storage, "backend_name", None),
        "storage_persisted": bool(getattr(storage, "persisted", False)),
    }
    # optional counters from adapter
    try:
        if hasattr(storage, "counts"):
            cnts = storage.counts()
            info.update(cnts)
        else:
            # best-effort shallow probes
            if hasattr(storage, "n_papers"):
                info["n_papers"] = int(getattr(storage, "n_papers"))
    except Exception:
        logger.exception("failed reading adapter counts for health")
    return info

# -------------------------------------------------------------------
# API endpoints (adapter-driven, minimal logic)
# -------------------------------------------------------------------


@app.get("/api/corpus", response_model=CorpusInfoResponse, tags=["health"], summary="Get active corpus identity and runtime config")
def api_corpus(storage: StorageAdapter = Depends(get_storage)):
    corpus_name = os.getenv("PAPER_KB_CORPUS")
    chunk_sets_dir = os.getenv("PAPER_KB_CHUNK_SETS_DIR")
    if not corpus_name and chunk_sets_dir:
        parts = Path(chunk_sets_dir).parts
        if "corpora" in parts:
            try:
                corpus_name = parts[parts.index("corpora") + 1]
            except Exception:
                pass
    return CorpusInfoResponse(
        corpus_name=corpus_name,
        storage_backend=getattr(storage, "backend_name", "unknown"),
        chunk_sets_dir=chunk_sets_dir,
        cache_ready=bool(getattr(app.state, "cache_ready", False)),
        loaded_at=getattr(storage, "loaded_at", None),
    )


@app.get("/api/corpus/health", response_model=CorpusHealthResponse, tags=["health"], summary="Get corpus diagnostics")
def api_corpus_health(storage: StorageAdapter = Depends(get_storage)):
    counts = {}
    if hasattr(storage, "counts"):
        counts = storage.counts() or {}
    diags = storage.diagnostics() if hasattr(storage, "diagnostics") else {}
    warnings = list(diags.get("warnings") or [])
    if counts.get("n_invalid_artifacts", 0) > 0:
        warnings.append("invalid artifacts detected")
    status = "ok" if not warnings else "warning"
    return CorpusHealthResponse(
        status=status,
        n_papers=int(counts.get("n_papers", 0)),
        n_chunks=int(counts.get("n_chunks", 0)),
        n_artifacts=int(counts.get("n_artifacts", 0)),
        n_invalid_artifacts=int(counts.get("n_invalid_artifacts", 0)),
        n_skipped_chunks=int(counts.get("n_skipped_chunks", 0)),
        dedupe_collisions=int(counts.get("dedupe_collisions", 0)),
        warnings=warnings,
    )

@app.get("/api/papers", response_model=PapersList, tags=["papers"], summary="List and filter accepted papers")
def api_list_papers(q: str = "", year: Optional[int] = None, venue: str = "", tag: str = "", status: str = "", offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000), storage: StorageAdapter = Depends(get_storage)):
    """
    Filter accepted paper metadata, then deterministically sort and paginate.
    Storage adapters own the read cache; status is not a corpus-backed field.
    """
    try:
        if status.strip():
            raise HTTPException(status_code=422, detail="status filtering is not supported by the corpus read model")
        papers = services.filter_papers(
            services.list_papers(storage), q=q, year=year, venue=venue, tag=tag
        )
        return PapersList(papers=papers[offset: offset + limit], total=len(papers))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("api_list_papers failed")
        raise HTTPException(status_code=500, detail="list papers failed")

@app.get("/api/papers/{paper_id}", response_model=PaperMeta, tags=["papers"],
         summary="Get paper metadata/details")
def api_get_paper(paper_id: str, storage: StorageAdapter = Depends(get_storage)):
    """
    Request a page of canonical chunks for a paper. Delegates to services.get_paper_chunks(storage, ...).
    This handler keeps the API contract stable.
    """
    try:
        return services.get_paper_detail(storage, paper_id=paper_id)
    except services.NotFoundError:
        raise HTTPException(status_code=404, detail="paper not found")
    except HTTPException:
        raise
    except Exception:
        logger.exception("api_get_paper failed for %s", paper_id)
        raise HTTPException(status_code=500, detail="get paper detail failed")

@app.get("/api/papers/{paper_id}/chunks/{chunk_id}", response_model=ChunkResponse, tags=["papers","chunks"],
         summary="Get single chunk")
def api_get_chunk(paper_id: str, chunk_id: str, storage: StorageAdapter = Depends(get_storage)):
    try:
        chunk = services.get_chunk(storage, paper_id=paper_id, chunk_id=chunk_id)
        if chunk is None:
            raise HTTPException(status_code=404, detail="chunk not found")
        return chunk
    except HTTPException:
        raise
    except Exception:
        logger.exception("api_get_chunk failed for %s/%s", paper_id, chunk_id)
        raise HTTPException(status_code=500, detail="get chunk failed")

# Filtered chunks endpoint kept for backwards compatibility; services will use adapter
@app.get("/api/papers/{paper_id}/chunks", tags=['papers','chunks'], response_model=PaperChunksResponse,
         summary="Get chunks for a paper (optionally filtered by q)")
def api_get_filtered_chunks(paper_id: str, q: str = "", offset: int = 0, limit: int = 200, storage: StorageAdapter = Depends(get_storage)):
    try:
        # Delegate to services.get_paper_chunks which supports q optionally
        resp = services.get_paper_chunks(storage, paper_id=paper_id, offset=offset, limit=limit, q=q or None)
        return resp
    except services.NotFoundError:
        raise HTTPException(status_code=404, detail="paper not found")
    except HTTPException:
        raise
    except Exception:
        logger.exception("api_get_filtered_chunks failed for %s", paper_id)
        raise HTTPException(status_code=500, detail="filtered chunks failed")



@app.get("/api/papers/{paper_id}/summary", tags=["papers","summaries"], summary="Get saved paper summary")
def api_get_paper_summary(paper_id: str, storage: StorageAdapter = Depends(get_storage)):
    if not is_canonical_paper_id(paper_id):
        raise HTTPException(status_code=404, detail="paper not found")
    try:
        services.get_paper_detail(storage, paper_id=paper_id)
    except services.NotFoundError:
        raise HTTPException(status_code=404, detail="paper not found")

    corpus_name = os.getenv("PAPER_KB_CORPUS", "default")
    paths = resolve_corpus_paths(corpus_name).ensure_dirs()
    p = summary_path(paths.root / "summaries", paper_id)
    if not p.exists():
        raise HTTPException(status_code=404, detail="summary not found")
    return json.loads(p.read_text(encoding="utf-8"))


@app.post("/api/papers/{paper_id}/summary:generate", tags=["papers","summaries"], summary="Generate or return paper summary")
def api_generate_paper_summary(paper_id: str, req: SummaryGenerateRequest, storage: StorageAdapter = Depends(get_storage)):
    if not is_canonical_paper_id(paper_id):
        raise HTTPException(status_code=404, detail="paper not found")
    try:
        services.get_paper_detail(storage, paper_id=paper_id)
    except services.NotFoundError:
        raise HTTPException(status_code=404, detail="paper not found")

    corpus_name = os.getenv("PAPER_KB_CORPUS", "default")
    try:
        summary, _written = asyncio.run(
            generate_summary_for_paper(
                corpus=corpus_name,
                paper_id=paper_id,
                provider_name=req.provider,
                force=bool(req.force),
                provider=summary_provider_factory(req.provider, agent_mode=getattr(req, "agent_mode", "client")),
            )
        )
        return summary
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except KeyError:
        raise HTTPException(status_code=404, detail="paper not found")


@app.post("/api/search", response_model=SearchV1Response, tags=["search"], summary="Lexical search")
def api_search_v1(req: SearchRequest, storage: StorageAdapter = Depends(get_storage)):
    q = (req.q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="empty query")
    hits = services.search(storage, q=q, k=int(req.k or 6), paper_id=req.paper_id)
    return SearchV1Response(capability="lexical", query=q, k=int(req.k or 6), hits=hits.hits)

# Dev seed route (keeps previous behaviour)
@app.post("/_dev/seed", tags=["dev"])
def dev_seed(n_papers: int = 8, min_chunks: int = 6, max_chunks: int = 12):
    try:
        created = services.seed_dev_fixture(n_papers=n_papers, min_chunks=min_chunks, max_chunks=max_chunks, write_files=True)
        return {"n": len(created), "papers": [p.dict() for p in created]}
    except Exception:
        logger.exception("dev_seed failed")
        raise HTTPException(status_code=500, detail="seed failed")

# -------------------------------------------------------------------
# (Optional) Search and summary endpoints can be re-attached later.
# For now we keep the minimal set needed for frontend smoke tests:
#  - GET /api/papers
#  - GET /api/papers/{paper_id}
#  - GET /api/papers/{paper_id}/chunks
#  - GET /api/papers/{paper_id}/chunks/{chunk_id}
# -------------------------------------------------------------------

# -------------------------------------------------------------------
# Run server (for manual runs)
# -------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run("backend.app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 9000)), reload=True)



# # -------------------------
# # Summary flow: enqueue + background worker
# # -------------------------
# def llm_generate(prompt: str, max_tokens: int = 256) -> str:
#     """Synchronous LLM call wrapper. In production, you would replace or route through a queue + billing monitor."""
#     if openai and OPENAI_API_KEY:
#         rsp = openai.ChatCompletion.create(
#             model="gpt-4o-mini",
#             messages=[{"role": "user", "content": prompt}],
#             max_tokens=max_tokens,
#             temperature=0.2,
#         )
#         return rsp.choices[0].message.content.strip()
#     # fallback: echo prompt header (deterministic, safe)
#     return f"[LLM not configured] prompt preview: {prompt[:500]}"



# def _build_prompt_and_prov(docs: List[Dict[str, Any]], question: str) -> tuple[str, List[Dict[str, Any]]]:
#     ctx = ""
#     prov = []
#     for n, item in enumerate(docs):
#         meta = item.get("meta") or {}
#         src = f"{meta.get('paper_id','?')}:{meta.get('pages','')}"
#         text = item.get("text","")
#         ctx += f"[{n+1}] {src} — {text}\n\n"
#         prov.append({"index": n+1, "id": item.get("id"), "meta": meta})
#     prompt = f"""You are a concise summarizer. Use ONLY the context paragraphs below to answer the question.
# Question: {question}

# Context paragraphs:
# {ctx}

# Answer in one paragraph, and for each factual sentence include a bracketed reference like [1] referring to the context index that supports it.
# """
#     return prompt, prov



# STORE_SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)



# # -----------------------------
# # helpers: summary file ops
# # -----------------------------
# def summary_id_for_request(req_obj: Dict[str, Any]) -> str:
#     key = json.dumps(req_obj, sort_keys=True, ensure_ascii=False)
#     return "sum-" + hashlib.sha1(key.encode("utf8")).hexdigest()


# def summary_path_for_id(summary_id: str) -> Path:
#     return STORE_SUMMARIES_DIR / f"{summary_id}.json"


# def load_summary_record(summary_id: str) -> Optional[Dict[str, Any]]:
#     p = summary_path_for_id(summary_id)
#     if not p.exists():
#         return None
#     try:
#         return json.loads(p.read_text(encoding="utf8"))
#     except Exception:
#         return None


# def save_summary_record(summary_id: str, payload: Dict[str, Any]) -> None:
#     p = summary_path_for_id(summary_id)
#     tmp = p.with_suffix(".json.tmp")
#     tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf8")
#     tmp.replace(p)


# # -----------------------------
# # /api/search - typed and robust
# # -----------------------------
# @app.post("/api/search", tags=["search"], response_model=SearchResponse, summary="Embedding search (RAG hits)")
# def api_search(req: SearchRequest):
#     q = (req.q or "").strip()
#     if not q:
#         raise HTTPException(status_code=400, detail="Empty query")

#     # produce embedding
#     emb = batch_embed_records("query:" + q, q)
#     if not emb:
#         raise HTTPException(status_code=500, detail="Embedding failed")
#     emb_list = list(emb)

#     coll = _get_coll_or_503()
#     where = {"paper_id": {"$eq": req.paper_id}} if req.paper_id else None

#     try:
#         res = safe_query(coll, emb_list, n_results=int(req.k or 6), where=where,
#                          include=["documents", "metadatas", "ids", "distances"])
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"safe_query error: {e}")

#     # safe_query returns shape with possibly nested lists; normalize
#     docs = res.get("documents") or []
#     metas = res.get("metadatas") or []
#     ids = res.get("ids") or []
#     distances = res.get("distances") or []

#     # flatten if nested
#     if docs and isinstance(docs[0], list):
#         docs = docs[0]
#         metas = metas[0] if metas and isinstance(metas[0], list) else metas
#         ids = ids[0] if ids and isinstance(ids[0], list) else ids
#         distances = distances[0] if distances and isinstance(distances[0], list) else distances

#     hits: List[SearchHit] = []
#     for i, (doc, meta, idd) in enumerate(zip(docs, metas, ids)):
#         score = None
#         try:
#             # try to extract score from distances if present (many stores return distance)
#             if distances:
#                 score = float(distances[i])
#         except Exception:
#             score = None

#         # try to expose canonical chunk info in the hit for frontend convenience
#         chunk_id = None
#         paper_id = None
#         try:
#             if isinstance(meta, dict):
#                 chunk_id = meta.get("chunk_id") or meta.get("id") or idd
#                 paper_id = meta.get("paper_id") or meta.get("pid") or None
#         except Exception:
#             pass

#         # keep meta as-is but avoid huge embeddings leaking to frontend
#         if isinstance(meta, dict) and "embedding" in meta:
#             meta = dict(meta)
#             meta.pop("embedding", None)

#         hits.append(SearchHit(
#             id=str(idd),
#             text=str(doc or ""),
#             score=score,
#             meta=meta,
#             chunk_id=chunk_id,
#             paper_id=paper_id
#         ))

#     return SearchResponse(query=q, k=int(req.k or 6), hits=hits)


# # -----------------------------
# # /api/summary - synchronous, idempotent
# # -----------------------------
# @app.post("/api/summary", tags=["summaries"], response_model=SummaryResult, summary="Create or fetch summary")
# def api_create_summary(req: SummaryRequest):
#     # must provide q or paper_id
#     if not (req.q or req.paper_id):
#         raise HTTPException(status_code=400, detail="Provide 'q' or 'paper_id'")

#     req_dict = req.dict()
#     summary_id = summary_id_for_request(req_dict)
#     existing = load_summary_record(summary_id)
#     if existing:
#         # Return cached result (idempotent)
#         return SummaryResult(**existing)

#     # Build the query text used for embedding / retrieval
#     if req.q:
#         query_text = req.q.strip()
#     else:
#         query_text = f"summarize paper {req.paper_id}"

#     # embed + retrieval
#     emb = batch_embed_records("query:" + query_text, query_text)
#     if not emb:
#         raise HTTPException(status_code=500, detail="Embedding failed")
#     emb_list = list(emb)

#     # choose retrieval method: use CHROMA_COLL.query directly for speed
#     coll = _get_coll_or_503()
#     try:
#         # prefer safe_query wrapper to handle versions
#         res = safe_query(coll, emb_list, n_results=int(req.k or 6), where=( {"paper_id":{"$eq": req.paper_id}} if req.paper_id else None),
#                          include=["documents", "metadatas", "ids"])
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"retrieval failed: {e}")

#     docs = res.get("documents") or []
#     metas = res.get("metadatas") or []
#     ids = res.get("ids") or []

#     # normalize nested
#     if docs and isinstance(docs[0], list):
#         docs = docs[0]; metas = metas[0] if metas else metas; ids = ids[0] if ids else ids

#     # build support list and used_chunk_ids
#     support = []
#     used_chunk_ids = []
#     for d, m, i in zip(docs, metas, ids):
#         # keep doc text, and a normalized small meta (avoid large embeddings)
#         small_meta = m if isinstance(m, dict) else {}
#         small_meta_copy = dict(small_meta)
#         small_meta_copy.pop("embedding", None)
#         # attempt to get canonical chunk id
#         chunk_id = small_meta_copy.get("chunk_id") or small_meta_copy.get("id") or i
#         used_chunk_ids.append(chunk_id)
#         support.append({"id": str(i), "text": str(d or ""), "meta": small_meta_copy})

#     # build prompt using your helper (keeps provenance)
#     try:
#         prompt, prov = _build_prompt_and_prov(support, query_text)
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"prompt build failed: {e}")

#     # call LLM synchronously (can be slow, but idempotent)
#     try:
#         llm_out = llm_generate(prompt, max_tokens=600)
#         answer = llm_out or ""
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=f"LLM generation failed: {e}")

#     now_ts = time.time()
#     result_payload = {
#         "summary_id": summary_id,
#         "request": req_dict,
#         "answer": answer,
#         "rag": {
#             "support": prov,
#             "used_chunk_ids": used_chunk_ids
#         },
#         "created_at": now_ts
#     }

#     # persist summary for idempotency
#     try:
#         save_summary_record(summary_id, result_payload)
#     except Exception:
#         # persist failure shouldn't prevent returning result
#         pass

#     return SummaryResult(**result_payload)


# @app.get("/api/summary/{summary_id}", tags=["summaries"], response_model=SummaryResult, summary="Get a saved summary")
# def api_get_summary(summary_id: str):
#     rec = load_summary_record(summary_id)
#     if not rec:
#         raise HTTPException(status_code=404, detail="summary not found")
#     return SummaryResult(**rec)


# # Minimal topics endpoint stub (non-blocking)
# @app.get("/api/topics", tags = ['summaries'])
# def api_topics(k: int = 200):
#     """Return a light sample of metadata to allow simple topic UIs (no UMAP here)."""
#     try:
#         raw = _safe_collection_get_all(CHROMA_COLL)
#         metas = raw.get("metadatas", []) or []
#         ids = raw.get("ids", []) or []
#         sample = []
#         for idx, meta in enumerate(metas):
#             if not isinstance(meta, dict):
#                 continue
#             pid = meta.get("paper_id") or meta.get("paper") or None
#             if pid:
#                 sample.append({"id": ids[idx] if idx < len(ids) else None, "meta": meta})
#             if len(sample) >= k:
#                 break
#         return {"n_sample": len(sample), "sample": sample}
#     except Exception as e:
#         raise HTTPException(status_code=500, detail=str(e))
