
import json
import os
import re
import hashlib
import unicodedata
from pathlib import Path
from typing import List, Dict, Generator, Optional, Any
from datetime import datetime
import logging

from shared.config import CHUNKS_DIR
from backend.app.schemas import CanonicalChunk, PaperMeta

logger = logging.getLogger(__name__)

# Ensure dir exists
CHUNKS_DIR.mkdir(parents=True, exist_ok=True)



def _safe_serialize_meta(m: Any) -> Any:
    """Return a JSON-serializable representation for metadata (minimal)."""
    try:
        json.dumps(m)
        return m
    except Exception:
        try:
            return json.loads(json.dumps(m, default=str))
        except Exception:
            return str(m)

def write_embeddings_fallback(
    chunk_ids: List[str],
    paper_ids: List[str],
    metadatas: List[Dict[str, Any]],
    embeddings: List[List[float]],
    dest_dir: str = "store/chroma_fallback",
    fname: Optional[str] = None,
) -> Path:
    """
    Write a durable JSONL fallback when Chroma persistence is unavailable.

    Produces one JSON object per line with keys:
      - id
      - paper_id
      - meta
      - embedding

    The write is atomic (temp file + os.replace). Returns the Path to the written file.
    """
    n = len(chunk_ids)
    if not all(len(lst) == n for lst in (paper_ids, metadatas, embeddings)):
        raise ValueError("Mismatched input lengths")

    outdir = Path(dest_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    fname = fname or f"emb_fallback_{ts}.jsonl"
    tmp = outdir / (fname + ".tmp")
    final = outdir / fname

    with tmp.open("w", encoding="utf8") as fh:
        for cid, pid, meta, emb in zip(chunk_ids, paper_ids, metadatas, embeddings):
            record = {
                "id": str(cid),
                "paper_id": str(pid),
                "meta": _safe_serialize_meta(meta),
                "embedding": emb,
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # atomic replace
    os.replace(str(tmp), str(final))
    return final

def chunk_file_for(paper_id: str) -> Path:
    """Return filesystem path for `<paper_id>_chunks.jsonl` (canonical)."""
    safe = str(paper_id)
    return CHUNKS_DIR / f"{safe}_chunks.jsonl"


def index_file_for(paper_id: str) -> Path:
    """Return path to index file `<paper_id>_chunks.idx` (JSON)."""
    safe = str(paper_id)
    return CHUNKS_DIR / f"{safe}_chunks.idx"


def _atomic_replace_bytes(path: Path, data_bytes: bytes):
    """Write bytes to tmp file and os.replace() to target path."""
    tmp = Path(str(path) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("wb") as fh:
        fh.write(data_bytes)
    os.replace(str(tmp), str(path))


def _atomic_write_text(path: Path, text: str):
    tmp = Path(str(path) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(text, encoding="utf8")
    os.replace(str(tmp), str(path))


def iter_chunks_jsonl(path: Path) -> Generator[Dict[str, Any], None, None]:
    """
    Generator over lines in a JSONL chunk file. Yields raw dicts. Does not validate.
    Robust to malformed lines (skips and logs).
    """
    if not path.exists():
        return
    with path.open("r", encoding="utf8") as fh:
        for lineno, ln in enumerate(fh, start=1):
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except Exception as exc:
                logger.warning("skip malformed json line %s:%d : %s", path, lineno, exc)
                continue


def read_chunks_jsonl(paper_id: str) -> List[Dict[str, Any]]:
    """Load chunk JSONL as raw dicts. Returns empty list if not found."""
    path = chunk_file_for(paper_id)
    if not path.exists():
        return []
    return list(iter_chunks_jsonl(path))


def read_chunks_as_models(paper_id: str) -> List[CanonicalChunk]:
    """Load JSONL and return list[CanonicalChunk]. Invalid lines are skipped but logged."""
    path = chunk_file_for(paper_id)
    if not path.exists():
        return []
    out: List[CanonicalChunk] = []
    for rec in iter_chunks_jsonl(path):
        try:
            model = CanonicalChunk(**rec)
            out.append(model)
        except Exception as exc:
            logger.warning("invalid chunk in %s (chunk_id=%s): %s", path, rec.get("chunk_id") or rec.get("id"), exc)
            continue
    return out


def _build_index_from_jsonl(path: Path) -> Dict[str, Dict[str, Any]]:
    """
    Scan JSONL in binary mode and build index mapping chunk_id -> {offset, line_no, chunk_index, char_len}.
    Returns dict (not written).
    """
    if not path.exists():
        return {}
    idx: Dict[str, Dict[str, Any]] = {}
    with path.open("rb") as fh:
        lineno = 0
        while True:
            offset = fh.tell()
            raw_line = fh.readline()
            if not raw_line:
                break
            lineno += 1
            try:
                line = raw_line.decode("utf8").strip()
            except Exception:
                # If decode fails, skip
                logger.warning("failed to decode line %d during index build for %s", lineno, path)
                continue
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception as exc:
                logger.warning("skip malformed json line while building index %s:%d : %s", path, lineno, exc)
                continue
            cid = rec.get("chunk_id") or rec.get("id")
            if not cid:
                # skip records without id
                continue
            try:
                chunk_index = int(rec.get("chunk_index") or rec.get("pos") or 0)
            except Exception:
                chunk_index = 0
            try:
                char_len = int(rec.get("char_len") or len(rec.get("text") or ""))
            except Exception:
                char_len = None
            idx[str(cid)] = {
                "offset": offset,
                "line_no": lineno,
                "chunk_index": chunk_index,
                "char_len": char_len,
            }
    return idx


def _build_chunks_index(paper_id: str) -> Path:
    """
    Scan existing JSONL for paper_id and write an index file atomically.
    Returns path to index file.
    """
    path = chunk_file_for(paper_id)
    idx_path = index_file_for(paper_id)
    if not path.exists():
        # ensure no stale idx
        if idx_path.exists():
            idx_path.unlink()
        return idx_path
    idx = _build_index_from_jsonl(path)
    # write index atomically
    _atomic_write_text(idx_path, json.dumps(idx, ensure_ascii=False, indent=2))
    logger.info("built index for %s (%d entries)", paper_id, len(idx))
    return idx_path


# from backend.app.schemas import CanonicalChunk
# from pydantic import ValidationError
import tempfile
from typing import Iterable

# helper serializer / normalizer
def _to_mapping(obj: Any) -> Dict[str, Any]:
    """
    Return a plain mapping for obj. Accepts:
      - dict -> returns as-is
      - CanonicalChunk instance -> returns model_dump() / dict()
      - any object exposing model_dump() or dict()
      - fallback to __dict__
    Raises TypeError if cannot coerce.
    """
    if isinstance(obj, dict):
        return obj
    # handle pydantic model instances (CanonicalChunk or others)
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        return dict(getattr(obj, "__dict__"))
    raise TypeError(f"Cannot coerce object to mapping for serialization: {type(obj)}")


def write_chunks_jsonl(paper_id: str,
                      records: Iterable[Any],
                      target_dir: str = "store/chunks",
                      build_index: bool = True) -> None:
    """
    Write <paper_id>_chunks.jsonl atomically in target_dir from a list of
    records that may be either mappings or CanonicalChunk-like model objects.
    Each record is validated via CanonicalChunk(...) and then serialized to JSONL.

    Keeps previous API: callers can pass list[dict] or list[CanonicalChunk].
    """
    target_dir = Path(target_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / f"{paper_id}_chunks.jsonl"
    tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{paper_id}_chunks.", dir=str(target_dir))
    os.close(tmp_fd)
    tmp_path = Path(tmp_path)

    validated = []
    for i, rec in enumerate(records):
        try:
            # coerce to mapping first
            mapping = _to_mapping(rec)
            # validate by constructing CanonicalChunk (this will enforce schema)
            try:
                inst = CanonicalChunk(**mapping)
            except TypeError:
                # fallback to model_validate if using pydantic v2 with that API (rare here)
                inst = CanonicalChunk(**mapping)
            # retrieve mapping from validated model in a pydantic-version-safe way
            if hasattr(inst, "model_dump"):
                validated_mapping = inst.model_dump()
            else:
                # pydantic v1 fallback
                validated_mapping = inst.dict()
            validated.append(validated_mapping)
        except Exception as e:
            logger.warning("write_chunks_jsonl: skipping invalid record for paper=%s seq=%d err=%s rec_type=%s", paper_id, i, e, type(rec))
            # continue: skip invalid record
            continue

    # if nothing validated, raise (or write nothing) — we prefer raising so upstream can handle
    if not validated:
        raise RuntimeError(f"No valid canonical chunks produced for paper {paper_id}")

    # write all validated mappings to tmp file
    with tmp_path.open("wb") as fh:
        for rec in validated:
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            fh.write(line.encode("utf8"))

    # atomic move into place
    os.replace(str(tmp_path), str(out_path))

    # optionally build index (if you have an index builder function elsewhere)
    if build_index:
        try:
            _build_chunks_index(paper_id, target_dir)  # implement or reuse existing index writer
        except Exception:
            logger.debug("write_chunks_jsonl: index build failed (nonfatal)", exc_info=True)


def get_chunk_by_id(paper_id: str, chunk_id: str) -> Optional[CanonicalChunk]:
    """
    Returns CanonicalChunk for given id or None.
    Prefer index seek, fallback to scanning file.
    """
    path = chunk_file_for(paper_id)
    idx_path = index_file_for(paper_id)
    if not path.exists():
        return None

    # try index first
    try:
        if idx_path.exists():
            raw = idx_path.read_text(encoding="utf8")
            idx = json.loads(raw)
            ent = idx.get(chunk_id)
            if ent:
                offset = int(ent.get("offset", 0))
                with path.open("rb") as fh:
                    fh.seek(offset)
                    raw_line = fh.readline()
                    if not raw_line:
                        return None
                    try:
                        rec = json.loads(raw_line.decode("utf8").strip())
                        return CanonicalChunk(**rec)
                    except Exception as exc:
                        logger.warning("index seek returned invalid json for %s:%s (%s)", paper_id, chunk_id, exc)
                        # fall back to scan
    except Exception as exc:
        logger.warning("failed reading index %s: %s", idx_path, exc)

    # fallback: scan
    for rec in iter_chunks_jsonl(path):
        if rec.get("chunk_id") == chunk_id or rec.get("id") == chunk_id:
            try:
                return CanonicalChunk(**rec)
            except Exception:
                # tolerant normalization
                try:
                    return normalize_chunk(rec, paper_id=paper_id)
                except Exception:
                    logger.debug("fallback normalize_chunk failed for %s/%s", paper_id, chunk_id)
                    return None
    return None


def get_chunk_text(paper_id: str, chunk_id: str) -> Optional[str]:
    """Fetch text for a single chunk by id. Uses index then fallback."""
    c = get_chunk_by_id(paper_id, chunk_id)
    return c.text if c is not None else None


# ---------- normalization helpers (kept adapted from your earlier draft) ----------
def normalize_chunk(raw: dict, paper_id: str, default_index: int = 0) -> CanonicalChunk:
    cid = raw.get("chunk_id") or raw.get("id")
    if not cid:
        raise ValueError("missing chunk id")
    text = (raw.get("text") or raw.get("preview") or "").strip()
    try:
        chunk_index = int(raw.get("chunk_index") or raw.get("pos") or default_index)
    except Exception:
        chunk_index = default_index
    try:
        char_len = int(raw.get("char_len") or len(text))
    except Exception:
        char_len = len(text)
    # header_path normalization
    hp = raw.get("header_path")
    if isinstance(hp, str):
        header_path = [p.strip() for p in hp.split(" / ") if p.strip()]
    elif isinstance(hp, (list, tuple)):
        header_path = [str(x) for x in hp]
    else:
        header_path = None
    # pages normalization -> tuple (first,last)
    pages = raw.get("pages")
    first = last = None
    if isinstance(pages, (list, tuple)):
        if len(pages) >= 2:
            first, last = pages[0], pages[1]
        elif len(pages) == 1:
            first = last = pages[0]
    elif isinstance(pages, dict):
        first = pages.get("first") or pages.get("start")
        last = pages.get("last") or pages.get("end")
    elif isinstance(pages, (int, str)) and str(pages).isdigit():
        first = last = int(pages)
    # coerce to ints where possible
    try:
        first = int(first) if first is not None else None
    except Exception:
        first = None
    try:
        last = int(last) if last is not None else None
    except Exception:
        last = None

    return CanonicalChunk(
        chunk_id=str(cid),
        paper_id=str(paper_id),
        paper_uid=raw.get("paper_uid") or (raw.get("meta") or raw.get("metadata") or {}).get("paper_uid"),
        text=text,
        chunk_index=chunk_index,
        char_len=char_len,
        header_path=header_path,
        pages=(first, last) if (first is not None or last is not None) else None,
        meta=raw.get("meta") or raw.get("metadata") or {}
    )


def chunks_to_records(title: str, paper_id: str, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert parsed TEI 'chunks' into canonical records by delegating to `normalize_chunk`.
    Returns list[dict] (CanonicalChunk.dict()).
    """
    pid = paper_id or (title or "paper")
    seq = 0
    canonical_results: List[Dict[str, Any]] = []

    for c in chunks:
        seq += 1

        unit = (c.get("unit") or "").lower()
        level = "sent" if unit == "sent" else "para"

        sec_label = c.get("section_title") or "section"
        sec_num = c.get("section_number") or ""
        sec_hash = hashlib.sha1((str(sec_num) + "::" + sec_label).encode("utf8")).hexdigest()[:8]
        sec_key = f"{pid}::sec::{sec_hash}"

        xmlid = c.get("xml_id") or c.get("id") or c.get("xml:id")
        if xmlid:
            chunk_id = f"{pid}::{str(xmlid)}"
        else:
            chunk_id = f"{pid}::{level}::{seq:06d}"

        pages_raw = c.get("pages")
        pages_val = None
        try:
            if pages_raw is None:
                pages_val = None
            elif isinstance(pages_raw, (list, tuple)):
                if len(pages_raw) >= 1:
                    pages_val = pages_raw[0]
                else:
                    pages_val = None
            elif isinstance(pages_raw, dict):
                first = pages_raw.get("first") or pages_raw.get("start")
                pages_val = first if first is not None else None
            else:
                pages_val = pages_raw
        except Exception:
            pages_val = None

        bboxes = c.get("coords") or c.get("bboxes") or c.get("boxes") or None
        header_path_str = " / ".join(filter(None, [str(title or ""), str(sec_label)]))
        text_val = (c.get("text") or "").strip()

        upstream_rec: Dict[str, Any] = {
            "chunk_id": chunk_id,
            "paper_id": pid,
            "text": text_val,
            "chunk_index": seq - 1,
            "header_path": header_path_str,
            "pages": pages_val,
            "meta": {
                "level": level,
                "parent_section": sec_key,
                "section_label": sec_label,
                "section_number": sec_num,
                **({"bboxes": bboxes} if bboxes is not None else {}),
            },
            "char_len": len(text_val),
        }

        canonical_model = normalize_chunk(upstream_rec, pid, default_index=seq - 1)
        canonical_results.append(canonical_model.dict())

    return canonical_results
