#!/usr/bin/env python3
"""
src/grobid_ingest.py

Patched GROBID ingestion runner for the paper-kb project.

Key behaviour:
 - POST to Grobid using form-data teiCoordinates (head, s)
 - Parse TEI with lxml.etree when available (namespace-aware)
 - Prefer xml:id for stable chunk ids (fallback to synthetic ids)
 - Preserve pages/bboxes when present (best-effort parsing)
 - Emit JSONL of canonical records and optional LangChain Documents
 - Safer resource handling (with open(...))
 - Integrates with your cached_embed (imported from embed_cache)
"""
from __future__ import annotations
import hashlib
import re, json
import requests
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path
from typing import Optional

# from shared.config import GROBID_URL

import os
import logging

logger = logging.getLogger("app.services")

from shared.config import GROBID_URL

from backend.app.chunks_fs import write_chunks_jsonl, _atomic_write_text




def _short_hash(s: str, n: int = 8) -> str:
    return hashlib.sha1(s.encode("utf8")).hexdigest()[:n]


def post_pdf_to_grobid(pdf_path: Path | str, timeout_seconds: int = 180, max_retries: int = 3, backoff: float = 1.0, consolidate_header: bool = True) -> str:
    """
    POST a PDF to a local Grobid instance and return TEI XML as str.

    - pdf_path: Pathlike to PDF (will open safely)
    - timeout_seconds: requests timeout for the POST
    - max_retries: number of attempts on transient failures
    - backoff: initial backoff seconds (exponential)
    """
    pdf_p = Path(pdf_path).expanduser().resolve()
    if not pdf_p.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_p}")

    data = {
        "generateIDs": "1",
        "consolidateHeader": "1" if consolidate_header else "0",
        "segmentSentences": "1",
        # request coordinates for head and sentences if GROBID supports
        "teiCoordinates": ["head", "s"],
    }

    attempt = 0
    last_exc: Optional[Exception] = None
    while attempt < max_retries:
        attempt += 1
        try:
            with pdf_p.open("rb") as fh:
                files = {"input": (pdf_p.name, fh, "application/pdf")}
                resp = requests.post(GROBID_URL, files=files, data=data, timeout=timeout_seconds)
                resp.raise_for_status()
                tei_text = resp.text
                if not tei_text or not tei_text.strip():
                    raise RuntimeError("Empty TEI returned by Grobid")
                return tei_text
        except requests.HTTPError as e:
            # 4xx likely permanent, 5xx maybe retryable
            last_exc = e
            status = getattr(e.response, "status_code", None)
            if status and 400 <= status < 500:
                # probably client problem — no retry
                raise RuntimeError(f"Grobid returned {status}: {e}") from e
            # else retry
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
        except Exception as e:
            last_exc = e

        # backoff
        sleep = backoff * (2 ** (attempt - 1))
        time.sleep(sleep)

    raise RuntimeError(f"Failed to get TEI from Grobid after {max_retries} attempts") from last_exc




# ---------------------------
# Main runner
# ---------------------------

from pipeline.parsers.tei_parser import parse_tei_text
# Replace or patch the existing runner with this code (keep post_pdf_to_grobid and parse_tei_text definitions above)

from pathlib import Path
from typing import Optional, List, Dict, Any
import glob
import traceback

def grobid_version() -> str | None:
    """Best-effort running service revision; absence is explicitly unknown."""
    parsed = urlsplit(GROBID_URL)
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, "/api/version", "", ""))
    try:
        response = requests.get(endpoint, timeout=3)
        response.raise_for_status()
        value = response.text.strip()
        return value[:160] or None
    except requests.RequestException:
        return None


def processing_receipt(pdf_path: Path, tei_path: Path, tei_text: str, *,
                       consolidate_header: bool, version: str | None,
                       extracted_doi: str | None = None) -> dict:
    """Evidence of processing settings, *not* proof of provider field origins."""
    h = hashlib.sha256()
    with Path(pdf_path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return {
        "schema_version": 1, "producer": "paper-kb",
        "input_pdf_basename": pdf_path.name,
        "input_pdf_sha256": h.hexdigest(),
        "tei_basename": tei_path.name,
        "tei_sha256": hashlib.sha256(tei_text.encode("utf-8")).hexdigest(),
        "grobid_version": version,
        "grobid_version_status": "OBSERVED" if version else "UNKNOWN",
        "consolidate_header": "1" if consolidate_header else "0",
        "external_enrichment": "POSSIBLE_NOT_VERIFIED" if consolidate_header else "DISABLED",
        "field_level_metadata_origin": "NOT_OBSERVABLE",
        "observed_tei_doi": extracted_doi,
        "parser": "grobid_processFulltextDocument",
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }


def _collect_pdf_paths(input_path: str, recursive: bool = False) -> List[Path]:
    p = Path(input_path).expanduser().resolve()
    if p.exists() and p.is_file():
        return [p]
    if p.exists() and p.is_dir():
        if recursive:
            # rglob for all PDFs under directory
            return sorted([pp for pp in p.rglob("*.pdf") if pp.is_file()])
        else:
            return sorted([pp for pp in p.glob("*.pdf") if pp.is_file()])
    # treat input as glob pattern (e.g. "storage/downloads/*.pdf")
    matches = [Path(m).expanduser().resolve() for m in glob.glob(input_path, recursive=recursive)]
    return sorted([m for m in matches if Path(m).is_file()])


def _sanitize_filename(s: str) -> str:
    """Simple filename sanitizer: keep letters, numbers, dash, underscore and dot; collapse others to _"""
    if not s:
        return ""
    s = re.sub(r"[^\w\-. ]+", "_", s)
    s = s.strip()
    # keep it reasonable length
    return s[:180]





# ---------- 1) Generate TEIs from PDFs ----------
def generate_teis_from_pdfs(pdf_dir: Path,
                            out_tei_dir: Path,
                            recursive: bool = False,
                            timeout: int = 180,
                            max_retries: int = 3,
                            max_files: Optional[int] = None,
                            force: bool = False,
                            consolidate_header: bool = True) -> Dict[str, Any]:
    """
    POST PDFs to GROBID and write TEI files to out_tei_dir.
    Returns summary with successes and failures.
    """
    pdf_dir = Path(pdf_dir).expanduser().resolve()
    out_tei_dir = Path(out_tei_dir).expanduser().resolve()
    out_tei_dir.mkdir(parents=True, exist_ok=True)
    failures_dir = out_tei_dir / "failures"
    failures_dir.mkdir(exist_ok=True)
    receipts_dir = out_tei_dir / "receipts"

    files = _collect_pdf_paths(str(pdf_dir), recursive=recursive)
    if max_files:
        files = files[:max_files]

    summary = {"n_input": len(files), "n_success": 0, "n_failures": 0, "successes": [], "failures": []}
    version = grobid_version() if files else None

    for pdf_path in files:
        try:
            pdf_path = Path(pdf_path)
            logger.info("grobid: processing %s", pdf_path)
            tei_text = post_pdf_to_grobid(pdf_path, timeout_seconds=timeout, max_retries=max_retries, consolidate_header=consolidate_header)
            # try to get title/paper_id from parsed TEI (pure parse)
            parsed = {}
            try:
                parsed = parse_tei_text(tei_text) or {}
            except Exception:
                # not fatal - we'll still write the TEI using pdf stem
                logger.debug("parse_tei_text failed for preview metadata: %s", traceback.format_exc())

            title = parsed.get("title") or pdf_path.stem
            pid = parsed.get("paper_id") or parsed.get("pid") or None

            preferred_base = _sanitize_filename(title) or _sanitize_filename(pdf_path.stem) or "paper"
            candidate = out_tei_dir / f"{preferred_base}.tei.xml"

            # disambiguate collisions (only if file exists or provided pid doesn't match)
            if candidate.exists():
                # Repeated processing of the same input is not a new document.
                # Different PDFs with equal extracted titles may coexist.
                existing_receipt = receipts_dir / (candidate.name + ".provenance.json")
                if existing_receipt.exists():
                    old_receipt = json.loads(existing_receipt.read_text(encoding="utf-8"))
                    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
                    if (old_receipt.get("input_pdf_sha256") == digest
                            and old_receipt.get("consolidate_header") ==
                            ("1" if consolidate_header else "0")):
                        if not force:
                            raise FileExistsError(
                                f"Source/configuration already processed: {candidate}; use --force to replace")
                    else:
                        suffix = _short_hash(pdf_path.name + digest, 12)
                        candidate = out_tei_dir / f"{preferred_base}-{suffix}.tei.xml"
                else:
                    suffix = _short_hash(pdf_path.name, 8)
                    candidate = out_tei_dir / f"{preferred_base}-{suffix}.tei.xml"
            if candidate.exists() and not force:
                raise FileExistsError(f"TEI already exists: {candidate}; use --force to replace")

            receipt = processing_receipt(
                pdf_path, candidate, tei_text,
                consolidate_header=consolidate_header,
                version=version, extracted_doi=parsed.get("doi"),
            )
            receipts_dir.mkdir(parents=True, exist_ok=True)
            receipt_file = receipts_dir / (candidate.name + ".provenance.json")
            # The new TEI is not considered successful without its receipt.
            _atomic_write_text(candidate, tei_text)
            try:
                _atomic_write_text(receipt_file, json.dumps(receipt, sort_keys=True, indent=2) + "\n")
            except Exception:
                if not force:
                    candidate.unlink(missing_ok=True)
                raise
            logger.info("grobid: wrote TEI %s and receipt %s", candidate, receipt_file)
            summary["n_success"] += 1
            summary["successes"].append({
                "pdf": str(pdf_path), "tei": str(candidate),
                "receipt": str(receipt_file), "paper_id": pid, "title": title,
                "tei_sha256": receipt["tei_sha256"],
            })
        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("grobid: failed %s", pdf_path)
            err = {"pdf": str(pdf_path), "error": str(e), "traceback": tb}
            summary["n_failures"] += 1
            summary["failures"].append(err)
            # write failure dump
            fail_path = failures_dir / f"{_sanitize_filename(pdf_path.stem)}.fail.json"
            fail_path.write_text(json.dumps(err, ensure_ascii=False, indent=2), encoding="utf8")
            continue

    return summary


