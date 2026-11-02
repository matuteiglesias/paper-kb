from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import re
import json
import os

logger = logging.getLogger(__name__)

try:
    from backend.app.chunks_fs import iter_chunks_jsonl, read_chunks_as_models, get_chunk_by_id
except Exception:
    iter_chunks_jsonl = None
    read_chunks_as_models = None
    get_chunk_by_id = None

DEFAULT_CHUNKS_DIR = Path("fixture/chunks")
DEFAULT_PAPERS_DIR = Path("fixture/papers")
DEFAULT_CHUNK_SETS_DIR = Path("artifacts/chunk_sets")
_WORD_RE = re.compile(r"[a-z0-9]{2,}", re.I)


class StorageAdapter:
    backend_name = "unknown"
    persisted = False


def _normalize_pages(pages_raw) -> Optional[Tuple[Optional[int], Optional[int]]]:
    if pages_raw is None:
        return None
    if isinstance(pages_raw, (list, tuple)):
        a = int(pages_raw[0]) if len(pages_raw) >= 1 and pages_raw[0] is not None else None
        b = int(pages_raw[1]) if len(pages_raw) >= 2 and pages_raw[1] is not None else a
        return (a, b)
    if isinstance(pages_raw, str):
        s = pages_raw.strip()
        if "-" in s:
            parts = s.split("-", 1)
            try:
                return (int(parts[0]) if parts[0] else None, int(parts[1]) if parts[1] else None)
            except ValueError:
                return None
        try:
            return (int(s), int(s))
        except ValueError:
            return None
    if isinstance(pages_raw, dict):
        try:
            return (int(pages_raw.get("start")) if pages_raw.get("start") else None,
                    int(pages_raw.get("end")) if pages_raw.get("end") else None)
        except Exception:
            return None
    return None


def _ensure_chunk_shape(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    meta = rec.get("meta") or rec.get("metadata") or {}
    out["chunk_id"] = rec.get("chunk_id") or rec.get("id") or meta.get("chunk_id") or ""
    out["paper_id"] = rec.get("paper_id") or meta.get("paper_id") or ""
    out["paper_uid"] = rec.get("paper_uid") or meta.get("paper_uid") or None
    text = rec.get("text")
    if text is None:
        text = rec.get("preview") or meta.get("preview") or ""
    out["text"] = text or ""
    try:
        out["chunk_index"] = int(rec.get("chunk_index") or meta.get("chunk_index") or 0)
    except Exception:
        out["chunk_index"] = 0
    try:
        out["char_len"] = int(rec.get("char_len") or meta.get("char_len") or len(out["text"] or ""))
    except Exception:
        out["char_len"] = len(out["text"] or "")
    out["header_path"] = rec.get("header_path") or meta.get("header_path")
    out["source_file"] = rec.get("source_file") or meta.get("source_file")
    out["pages"] = _normalize_pages(rec.get("pages") or meta.get("pages"))
    out["meta"] = meta
    out["_raw"] = rec
    return out


class ChunkSetStorageAdapter(StorageAdapter):
    backend_name = "chunk-set"
    persisted = False

    def __init__(self, chunk_sets_dir: Optional[str] = None):
        self.chunk_sets_dir = Path(chunk_sets_dir or os.getenv("PAPER_KB_CHUNK_SETS_DIR") or DEFAULT_CHUNK_SETS_DIR)
        self._paper_chunks: Dict[str, List[Dict[str, Any]]] = {}
        self._chunk_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._papers: Dict[str, Dict[str, Any]] = {}
        self.loaded_at: Optional[float] = None
        self.n_artifacts: int = 0
        self.n_invalid_artifacts: int = 0
        self.n_skipped_chunks: int = 0
        self.dedupe_collisions: int = 0
        self.warnings: List[str] = []

    def _reconstruct_paper_meta(self, paper_id: str, chunks: List[Dict[str, Any]]) -> Dict[str, Any]:
        first = chunks[0] if chunks else {}
        meta = first.get("meta") or {}
        title = meta.get("title")
        if not title:
            hp = first.get("header_path")
            if isinstance(hp, list) and hp:
                title = hp[0]
            elif isinstance(hp, str) and hp.strip():
                title = hp
        if not title:
            title = first.get("source_file") or paper_id
        preview = ""
        for c in chunks:
            t = (c.get("text") or "").strip()
            if t:
                preview = t[:240]
                break
        paper_uid = first.get("paper_uid") or meta.get("paper_uid")
        return {
            "paper_id": paper_id,
            "paper_uid": paper_uid,
            "title": title,
            "authors": meta.get("authors") or [],
            "n_chunks": len(chunks),
            "preview": preview,
            "source_file": first.get("source_file"),
            "pipeline_version": meta.get("pipeline_version") or first.get("producer") or first.get("entrypoint"),
        }

    def _reconstruct_paper_meta_with_payload(
        self,
        paper_id: str,
        chunks: List[Dict[str, Any]],
        paper_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        base = self._reconstruct_paper_meta(paper_id, chunks)
        pm = paper_meta if isinstance(paper_meta, dict) else {}

        paper_uid = pm.get("paper_uid")
        if isinstance(paper_uid, str) and paper_uid.strip():
            base["paper_uid"] = paper_uid.strip()

        title = pm.get("title")
        if isinstance(title, str) and title.strip():
            base["title"] = title.strip()

        authors = pm.get("authors")
        if isinstance(authors, list):
            base["authors"] = authors
        elif base.get("authors") is None:
            base["authors"] = []
        elif not isinstance(base.get("authors"), list):
            base["authors"] = []

        source_file = pm.get("source_file")
        if isinstance(source_file, str) and source_file.strip():
            base["source_file"] = source_file.strip()

        for field in ("abstract", "date", "venue", "doi", "arxiv_id"):
            value = pm.get(field)
            if isinstance(value, str) and value.strip():
                base[field] = value.strip()
            elif value is None:
                base[field] = None

        year = pm.get("year")
        base["year"] = int(year) if isinstance(year, int) and not isinstance(year, bool) else None
        tags = pm.get("tags")
        base["tags"] = list(tags) if isinstance(tags, list) else []

        if not isinstance(base.get("authors"), list):
            base["authors"] = []
        return base

    def load_caches(self) -> None:
        self._paper_chunks.clear()
        self._chunk_index.clear()
        self._papers.clear()
        self.loaded_at = None
        self.n_artifacts = 0
        self.n_invalid_artifacts = 0
        self.n_skipped_chunks = 0
        self.dedupe_collisions = 0
        self.warnings = []
        if not self.chunk_sets_dir.exists():
            self.warnings.append(f"chunk_sets_dir does not exist: {self.chunk_sets_dir}")
            return

        # Deterministic artifact precedence:
        # - newer mtime wins
        # - filename breaks ties
        # We load oldest -> newest so later records overwrite earlier duplicates.
        artifact_paths = sorted(
            self.chunk_sets_dir.glob("*.chunk_set.json"),
            key=lambda p: (p.stat().st_mtime, p.name),
        )
        self.n_artifacts = len(artifact_paths)

        # Per-paper dedup index keyed by chunk_id to avoid duplicate chunks when
        # the same paper/chunk appears in multiple artifacts.
        per_paper_by_chunk_id: Dict[str, Dict[str, Dict[str, Any]]] = {}
        paper_meta_by_paper_id: Dict[str, Dict[str, Any]] = {}

        for p in artifact_paths:
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                logger.exception("failed reading chunk set file: %s", p)
                self.n_invalid_artifacts += 1
                self.warnings.append(f"invalid artifact: {p.name}")
                continue
            payload_paper_meta = payload.get("paper_meta")
            if isinstance(payload_paper_meta, dict):
                payload_paper_id = payload_paper_meta.get("paper_id") or payload_paper_meta.get("paper_uid")
                if isinstance(payload_paper_id, str) and payload_paper_id.strip():
                    paper_meta_by_paper_id[payload_paper_id] = dict(payload_paper_meta)
            for ch in payload.get("chunks", []) or []:
                rec = dict(ch)
                rec["producer"] = payload.get("producer")
                rec["entrypoint"] = payload.get("entrypoint")
                rec = _ensure_chunk_shape(rec)
                pid = rec.get("paper_id")
                cid = rec.get("chunk_id")
                if not pid or not cid:
                    self.n_skipped_chunks += 1
                    continue

                bucket = per_paper_by_chunk_id.setdefault(pid, {})
                if cid in bucket:
                    self.dedupe_collisions += 1
                bucket[cid] = rec
                self._chunk_index[(pid, cid)] = rec

        for pid, by_chunk_id in per_paper_by_chunk_id.items():
            chunks = list(by_chunk_id.values())
            chunks.sort(key=lambda c: int(c.get("chunk_index") or 0))
            self._paper_chunks[pid] = chunks
            self._papers[pid] = self._reconstruct_paper_meta_with_payload(
                pid,
                chunks,
                paper_meta=paper_meta_by_paper_id.get(pid),
            )
        import time
        self.loaded_at = time.time()

    def list_papers(self) -> List[Dict[str, Any]]:
        if not self._papers:
            self.load_caches()
        return list(self._papers.values())

    def get_paper(self, paper_id: str) -> Dict[str, Any]:
        if not self._papers:
            self.load_caches()
        return self._papers.get(paper_id, {})

    def list_chunks(self, paper_id: str, limit: int = 200, offset: int = 0, q: Optional[str] = None) -> Dict[str, Any]:
        if not self._paper_chunks:
            self.load_caches()
        chunks = list(self._paper_chunks.get(paper_id, []))
        if q:
            ql = q.lower()
            chunks = [c for c in chunks if ql in (c.get("text") or "").lower()]
        total = len(chunks)
        return {"paper_id": paper_id, "n": total, "chunks": chunks[offset:offset + limit]}

    def get_chunk(self, paper_id: str, chunk_id: str) -> Optional[Dict[str, Any]]:
        if not self._chunk_index:
            self.load_caches()
        return self._chunk_index.get((paper_id, chunk_id))

    def semantic_search(self, q: str, k: int = 6, paper_id: Optional[str] = None) -> List[Dict[str, Any]]:
        if not q:
            return []
        if not self._paper_chunks:
            self.load_caches()
        q_tokens = _WORD_RE.findall(q.lower())
        candidates = self._paper_chunks.get(paper_id, []) if paper_id else [c for arr in self._paper_chunks.values() for c in arr]
        hits = []
        for c in candidates:
            txt = (c.get("text") or "").lower()
            tokens = _WORD_RE.findall(txt)
            if not tokens:
                continue
            matches = sum(1 for t in q_tokens if t in tokens)
            score = matches / (1 + len(tokens))
            if score > 0:
                hits.append({"id": c.get("chunk_id"), "text": c.get("text"), "score": score, "meta": c.get("meta", {}), "paper_id": c.get("paper_id"), "paper_uid": c.get("paper_uid")})
        return sorted(hits, key=lambda x: x.get("score", 0), reverse=True)[:k]

    def counts(self) -> Dict[str, int]:
        if not self._paper_chunks:
            self.load_caches()
        return {"n_papers": len(self._paper_chunks), "n_chunks": sum(len(v) for v in self._paper_chunks.values()), "n_artifacts": self.n_artifacts, "n_invalid_artifacts": self.n_invalid_artifacts, "n_skipped_chunks": self.n_skipped_chunks, "dedupe_collisions": self.dedupe_collisions}

    def diagnostics(self) -> Dict[str, Any]:
        return {"loaded_at": self.loaded_at, "warnings": list(self.warnings), "n_artifacts": self.n_artifacts, "n_invalid_artifacts": self.n_invalid_artifacts, "n_skipped_chunks": self.n_skipped_chunks, "dedupe_collisions": self.dedupe_collisions}

    def close(self) -> None:
        return None

    def maybe_persist(self) -> None:
        return None


class JsonlAdapter:
    backend_name = "jsonl-fixture"
    persisted = False

    def __init__(self, chunks_dir: Optional[str] = None, papers_dir: Optional[str] = None, embeddings_dir: Optional[str] = None):
        self.chunks_dir = Path(chunks_dir or DEFAULT_CHUNKS_DIR)
        self.papers_dir = Path(papers_dir or DEFAULT_PAPERS_DIR)
        self.embeddings_dir = Path(embeddings_dir) if embeddings_dir else None
        self._papers_index: Optional[List[Dict[str, Any]]] = None

    def load_caches(self) -> None:
        # load simple papers index by scanning fixture/papers
        try:
            out = []
            if self.papers_dir.exists():
                for f in self.papers_dir.glob("*.json"):
                    try:
                        raw = json.loads(f.read_text(encoding="utf8"))
                        out.append({
                            "paper_id": raw.get("paper_id", f.stem),
                            "paper_uid": raw.get("paper_uid"),
                            "title": raw.get("title", f.stem),
                            "n_chunks": raw.get("n_chunks", 0),
                            "metadata": raw
                        })
                    except Exception:
                        logger.debug("skipping malformed paper file %s", f)
            self._papers_index = out
        except Exception:
            logger.exception("JsonlAdapter.load_caches failed")

    def list_papers(self) -> List[Dict[str, Any]]:
        if self._papers_index is not None:
            return self._papers_index
        self.load_caches()
        return self._papers_index or []

    def get_paper(self, paper_id: str) -> Dict[str, Any]:
        # try paper file
        pfile = self.papers_dir / f"{paper_id}.json"
        if pfile.exists():
            try:
                return json.loads(pfile.read_text(encoding="utf8"))
            except Exception:
                logger.debug("malformed paper file %s", pfile)
        # fallback to listing
        for p in self.list_papers():
            if p.get("paper_id") == paper_id:
                return p.get("metadata") or p
        return {}

    def list_chunks(self, paper_id: str, limit: int = 200, offset: int = 0, q: Optional[str] = None) -> Dict[str, Any]:
        out = []
        total = 0
        # find the file: <paper_id>_chunks.jsonl in fixture/chunks
        file_path = self.chunks_dir / f"{paper_id}_chunks.jsonl"
        if not file_path.exists():
            # try alternative pattern
            matches = list(self.chunks_dir.glob(f"{paper_id}*_chunks.jsonl"))
            if matches:
                file_path = matches[0]
            else:
                return {"paper_id": paper_id, "n": 0, "chunks": []}

        try:
            # stream lines; use provided iter_chunks_jsonl if available
            if callable(iter_chunks_jsonl):
                iterator = iter_chunks_jsonl(file_path)
            else:
                # fallback: simple JSONL parser
                def iterator_gen():
                    with open(file_path, "r", encoding="utf8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                yield json.loads(line)
                            except Exception:
                                continue
                iterator = iterator_gen()

            idx = 0
            start = offset
            end = offset + limit
            qlow = (q or "").lower() if q else None
            for rec in iterator:
                # count candidate even if filtered-out, but ensure filter semantics: if q present, only count matches
                text = (rec.get("text") or rec.get("preview") or (rec.get("meta") or {}).get("preview") or "")
                if qlow and qlow not in (text or "").lower():
                    continue
                if idx >= start and idx < end:
                    out.append(_ensure_chunk_shape(rec))
                idx += 1
            total = idx
        except Exception:
            logger.exception("JsonlAdapter.list_chunks streaming failed for %s", paper_id)
            # fallback: attempt to read via read_chunks_as_models if available
            try:
                if callable(read_chunks_as_models):
                    models = read_chunks_as_models(paper_id)
                    maps = [m.model_dump() if hasattr(m, "model_dump") else (m.dict() if hasattr(m, "dict") else m) for m in models]
                    if q:
                        qlow = q.lower()
                        maps = [m for m in maps if qlow in ((m.get("text") or m.get("preview") or "")).lower()]
                    total = len(maps)
                    out = [ _ensure_chunk_shape(m) for m in maps[offset:offset+limit] ]
                else:
                    return {"paper_id": paper_id, "n": 0, "chunks": []}
            except Exception:
                logger.exception("JsonlAdapter.list_chunks fallback also failed")
                return {"paper_id": paper_id, "n": 0, "chunks": []}

        return {"paper_id": paper_id, "n": int(total), "chunks": out}

    def get_chunk(self, paper_id: str, chunk_id: str) -> Optional[Dict[str, Any]]:
        # first try chunks file scan via get_chunk_by_id if available
        try:
            if callable(get_chunk_by_id):
                res = get_chunk_by_id(paper_id, chunk_id)
                if res:
                    # convert to dict shape
                    if hasattr(res, "model_dump"):
                        d = res.model_dump()
                    elif hasattr(res, "dict"):
                        d = res.dict()
                    else:
                        d = dict(getattr(res, "__dict__", {}))
                    return _ensure_chunk_shape(d)
            # fallback: stream file
            file_path = self.chunks_dir / f"{paper_id}_chunks.jsonl"
            if not file_path.exists():
                matches = list(self.chunks_dir.glob(f"{paper_id}*_chunks.jsonl"))
                if matches:
                    file_path = matches[0]
                else:
                    return None
            with open(file_path, "r", encoding="utf8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    cid = rec.get("chunk_id") or rec.get("id") or (rec.get("meta") or {}).get("chunk_id")
                    if cid == chunk_id:
                        return _ensure_chunk_shape(rec)
        except Exception:
            logger.exception("JsonlAdapter.get_chunk failed %s/%s", paper_id, chunk_id)
        return None

    def semantic_search(self, q: str, k: int = 6, paper_id: Optional[str] = None) -> List[Dict[str, Any]]:
        # quick lexical scoring; same as earlier but returning canonical fields
        if not q:
            return []
        q_tokens = _WORD_RE.findall(q.lower())
        hits = []
        def score_text(text: str):
            tokens = _WORD_RE.findall((text or "").lower())
            if not tokens:
                return 0.0
            matches = sum(1 for t in q_tokens if t in tokens)
            return matches / (1 + len(tokens))
        candidates = []
        if paper_id:
            res = self.list_chunks(paper_id, limit=10000, offset=0, q=None)
            candidates = res.get("chunks", [])
        else:
            # stream all chunks (careful: could be large, but this is emergency mode)
            for f in self.chunks_dir.glob("*_chunks.jsonl"):
                tmp = self.list_chunks(f.stem.replace("_chunks",""), limit=1000000, offset=0, q=None)
                candidates.extend(tmp.get("chunks", []))
        for c in candidates:
            s = score_text(c.get("text","") or "")
            if s > 0:
                hits.append({"id": c["chunk_id"], "text": c["text"], "score": s, "meta": c.get("meta", {}), "paper_id": c.get("paper_id"), "paper_uid": c.get("paper_uid")})
        hits_sorted = sorted(hits, key=lambda x: x.get("score",0), reverse=True)[:k]
        return hits_sorted

    def close(self) -> None:
        logger.debug("JsonlAdapter.close noop")

    def maybe_persist(self) -> None:
        logger.debug("JsonlAdapter.maybe_persist noop")


def create_adapter_from_env() -> StorageAdapter:
    backend = (os.getenv("STORAGE_BACKEND") or "jsonl").strip().lower()
    if backend in {"chunk_set", "chunk-set", "chunkset"}:
        return ChunkSetStorageAdapter()
    return JsonlAdapter(
        chunks_dir=os.getenv("PAPER_KB_CHUNKS_DIR"),
        papers_dir=os.getenv("PAPER_KB_PAPERS_DIR"),
    )
