#!/usr/bin/env python3
"""Fail-closed PDF→paper_uid→page chunk validation for a registered Paper KB corpus.

Does not parse/write any paper content. Prints a metadata-only receipt.
Proof is for exact PDF bytes, metadata/chunk links and sampled first-page text.
It does not establish scientific reading or section-aware parsing.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def chunk_errors(chunk: dict, *, paper_uid: str, pdf_name: str, sha: str,
                 pages: int) -> list[str]:
    meta = chunk.get("metadata") or {}
    faults = []
    if chunk.get("paper_uid") != paper_uid or chunk.get("document_id") != paper_uid:
        faults.append("paper/document identity mismatch")
    if chunk.get("source_file") != pdf_name or chunk.get("source_ref") != "sha256:" + sha:
        faults.append("source reference mismatch")
    if meta.get("source_sha256") != sha:
        faults.append("source checksum mismatch")
    try:
        start, end = int(meta["page_start"]), int(meta["page_end"])
        if not (1 <= start <= end <= pages):
            faults.append("invalid source page span")
    except (KeyError, TypeError, ValueError):
        faults.append("source page span missing")
    if not chunk.get("chunk_id") or not str(chunk.get("text") or "").strip():
        faults.append("empty chunk ID or text")
    return faults


def page_witness(pdf: Path, chunk: dict) -> dict:
    page = int(chunk["metadata"]["page_start"])
    proc = subprocess.run(
        ["pdftotext", "-f", str(page), "-l", str(page), "-layout", str(pdf), "-"],
        capture_output=True, text=True, timeout=20, check=True)
    candidate = re.findall(r"\w+", str(chunk["text"]).casefold())[:30]
    available = Counter(re.findall(r"\w+", proc.stdout.casefold()))
    overlap = (sum(min(n, available[w]) for w, n in Counter(candidate).items())
               / len(candidate) if candidate else 0.0)
    return {
        "page": page,
        "first_chunk_token_overlap": round(overlap, 3),
        "status": "PASS" if overlap >= 0.75 else "FAIL",
    }


def verify(corpus: Path, *, max_files: int = 100) -> dict:
    manifest_file = corpus / "source-manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest.get("corpus_id") == corpus.name
    input_files = manifest["files"]
    if not 1 <= len(input_files) <= max_files:
        raise ValueError("count outside budget")
    chunk_files = list((corpus / "chunk_sets").glob("*.chunk_set.json"))
    if len(chunk_files) != len(input_files):
        raise ValueError("chunk set count does not match manifest")
    papers = {}
    uids = set()
    ids = set()
    for path in chunk_files:
        c = json.loads(path.read_text(encoding="utf-8"))
        meta = c.get("paper_meta") or {}
        uid = meta.get("paper_uid")
        src = c.get("source_items")
        if (not isinstance(src, list) or len(src) != 1 or
            not isinstance(src[0], str) or
            src[0] in papers or not uid or uid in uids):
            raise ValueError("missing, duplicate or non-unique paper source mapping")
        papers[src[0]] = c
        uids.add(uid)
    registered = {row["registered_filename"]: row for row in input_files}
    if (set(papers) != set(registered) or
        {p.name for p in (corpus / "pdfs").glob("*.pdf")} != set(registered)):
        raise ValueError("registered PDF / source_items mismatch")
    sources = []
    for name in sorted(papers):
        pdf = corpus / "pdfs" / name
        original = registered[name]
        sha = file_sha256(pdf)
        if sha != original["sha256"] or pdf.stat().st_size != original["size_bytes"]:
            raise ValueError("PDF registry integrity failed: " + name)
        info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                              text=True, timeout=15, check=True).stdout
        m = re.search(r"(?m)^Pages:\s+(\d+)\s*$", info)
        if not m:
            raise ValueError("PDF page count missing: " + name)
        pages = int(m.group(1))
        c = papers[name]
        meta = c["paper_meta"]
        uid = meta["paper_uid"]
        if (meta.get("source_sha256") != sha or
            meta.get("source_file") != name or
            meta.get("source_ref") != "sha256:" + sha):
            raise ValueError("paper-level source identity mismatch: " + name)
        chunks = c.get("chunks") or []
        if not chunks or len(chunks) != c.get("chunk_count"):
            raise ValueError("chunk count invalid: " + name)
        for item in chunks:
            faults = chunk_errors(item, paper_uid=uid, pdf_name=name,
                                  sha=sha, pages=pages)
            if faults:
                raise ValueError(name + ": " + ", ".join(faults))
            key = item["chunk_id"]
            if key in ids:
                raise ValueError("duplicate global chunk ID " + key)
            ids.add(key)
        first = next((z for z in chunks if z.get("metadata", {}).get("page_start")), None)
        check = page_witness(pdf, first)
        if check["status"] != "PASS":
            raise ValueError("page token witness failed: " + name)
        sources.append(dict(pdf_name=name, paper_uid=uid, source_sha256=sha,
                            pdf_pages=pages, chunk_count=len(chunks),
                            witness=check))
    for family, filename in [
        ("catalog", "paper.catalog-record.v1.jsonl"),
        ("review", "paper.review-record.v1.jsonl"),
    ]:
        rows = [json.loads(line) for line in
                (corpus / family / filename).read_text(encoding="utf-8").splitlines()
                if line.strip()]
        if len(rows) != len(uids) or {r["paper_uid"] for r in rows} != uids:
            raise ValueError("canonical "+family+" projection does not match paper UIDs")
    return dict(schema="paper-kb.pdf-lineage.qa.v1", corpus=corpus.name,
                manifest_sha256=file_sha256(manifest_file),
                pdf_manifestations=len(sources),
                pdf_sha_verified=len(sources),
                page_anchor_witness_pass=len(sources),
                chunk_count=sum(x["chunk_count"] for x in sources),
                paper_uids_unique=len(uids),
                chunk_ids_unique=len(ids),
                catalog_review_matches=True,
                sections_not_validated=True,
                scientific_reading_not_claimed=True,
                sources=sources)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="tesis-cited")
    ap.add_argument("--max-files", type=int, default=19)
    ap.add_argument("--receipt", type=Path)
    args = ap.parse_args()
    corpus = ROOT / "corpora" / args.corpus
    result = verify(corpus, max_files=args.max_files)
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.receipt is not None:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(output, encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k!="sources"}, indent=2))
    print("PDF_LINEAGE_PASS")


if __name__ == "__main__":
    main()
