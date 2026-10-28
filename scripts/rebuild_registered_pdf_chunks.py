#!/usr/bin/env python3
"""One-time bounded PDF-first repair of a registered Paper KB corpus.

Only --apply mutates generated products. Keeps original PDF bytes/manifest fixed,
archives old TEI derivatives, and rolls back if the strict corpus doctor fails.
The local crosswalk carries title/metadata continuity, NOT a claim that the old
TEI derived from the registered PDF. This script does not require GROBID.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.projections.catalog_records import export_catalog_records
from pipeline.projections.review_records import export_review_records
from pipeline.writers.chunk_set_writer import write_chunk_set_artifact


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tokens(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s.lower())
    return [w for w in re.findall(r"[a-z0-9]+", s) if len(w) > 3]


def title_witness(title: str, first_pages_text: str) -> float:
    words = tokens(title)
    page = set(tokens(first_pages_text))
    return sum(w in page for w in words) / len(words) if words else 0.0


def page_chunks(*, pages: list[str], uid: str, paper_id: str,
                filename: str, source_sha: str, max_chunks: int = 5000) -> list[dict]:
    """Deterministic source-bound page chunks, independent of legacy TEI IDs."""
    result: list[dict] = []
    for pg, raw in enumerate(pages, 1):
        value = " ".join(raw.split())
        if len(value) < 50:
            continue
        for i, offset in enumerate(range(0, len(value), 1500)):
            part = value[offset:offset + 1500]
            if len(part) < 30:
                continue
            digest = hashlib.sha256(
                f"{source_sha}:{pg}:{i}:{part}".encode("utf-8")
            ).hexdigest()[:16]
            result.append({
                "chunk_id": f"{uid}_p{pg:03d}_{i:03d}_{digest}",
                "paper_uid": uid, "paper_id": paper_id, "document_id": uid,
                "chunk_index": len(result), "text": part,
                "source_file": filename, "source_ref": "sha256:" + source_sha,
                "metadata": {
                    "page_start": pg, "page_end": pg, "source_sha256": source_sha,
                    "parser": "poppler-pdftotext-layout-page-bounded-v1",
                },
            })
            if len(result) > max_chunks:
                raise ValueError("chunk budget exceeded")
    if not result:
        raise ValueError("PDF produced no usable page chunks")
    return result


def pdf_pages(pdf: Path, *, max_pages: int) -> list[str]:
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True,
                          text=True, timeout=20, check=True).stdout
    m = re.search(r"(?m)^Pages:\s+(\d+)\s*$", info)
    if not m:
        raise ValueError(f"no pdfinfo page count: {pdf.name}")
    count = int(m.group(1))
    if not 1 <= count <= max_pages:
        raise ValueError(f"page budget exceeded for {pdf.name}: {count}")
    output = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True, text=True, timeout=110, check=True,
    ).stdout.split("\f")
    if len(output) == count + 1 and not output[-1].strip():
        output.pop()
    if len(output) != count:
        raise ValueError(f"extracted pages {len(output)} != {count}: {pdf.name}")
    return output


def old_paper_meta(corpus: Path) -> dict[str, dict]:
    old: dict[str, dict] = {}
    for cp in sorted((corpus / "chunk_sets").glob("*.chunk_set.json")):
        obj = json.loads(cp.read_text(encoding="utf-8"))
        meta = obj.get("paper_meta") or {}
        source = Path(meta.get("source_file", "")).name
        uid = meta.get("paper_uid")
        if not source.endswith(".tei.xml") or not uid or source in old:
            raise ValueError(f"not one unique old TEI identity: {cp.name}")
        if not isinstance(meta.get("authors", []), list):
            raise ValueError(f"bad author list: {cp.name}")
        old[source] = meta
    return old


def plan(corpus: Path, crosswalk: Path, *, max_files: int, max_bytes: int,
         max_pages: int) -> tuple[list[dict], dict]:
    if corpus.is_symlink() or any((corpus / n).is_symlink() for n in
                                  ("pdfs", "chunk_sets", "catalog", "review", "xmls")):
        raise ValueError("refusing symlink corpus target")
    manifest_path = corpus / "source-manifest.json"
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    cross = json.loads(crosswalk.read_text(encoding="utf-8"))
    assert cross.get("schema") == "paper-kb.local-migration-crosswalk.v1"
    assert m.get("corpus_id") == corpus.name
    files = m["files"]
    if not 1 <= len(files) <= max_files:
        raise ValueError(f"file budget exceeded: {len(files)}")
    named = {f["registered_filename"]: f for f in files}
    matched = {x["pdf_filename"]: x["tei_filename"] for x in cross["relations"]}
    if len(named) != len(files) or len(matched) != len(files) or set(named) != set(matched):
        raise ValueError("crosswalk must be 1:1 with entire source manifest")
    if len(set(matched.values())) != len(matched):
        raise ValueError("duplicate TEI mapping")
    pdf_inventory = {p.name for p in (corpus / "pdfs").glob("*.pdf")}
    if pdf_inventory != set(named):
        raise ValueError("unregistered or missing PDFs")
    old = old_paper_meta(corpus)
    if set(old) != set(matched.values()):
        raise ValueError("TEI chunk set inventory does not match crosswalk")
    result = []
    total_bytes = 0
    for name in sorted(named):
        f = named[name]
        pdf = corpus / "pdfs" / name
        declared = f["sha256"]
        if pdf.stat().st_size != f["size_bytes"] or sha256(pdf) != declared:
            raise ValueError("PDF bytes differ from registered source: " + name)
        total_bytes += pdf.stat().st_size
        if total_bytes > max_bytes:
            raise ValueError("total source byte budget exceeded")
        pages = pdf_pages(pdf, max_pages=max_pages)
        meta = old[matched[name]]
        coverage = title_witness(meta.get("title", ""), " ".join(pages[:2])[:12000])
        if coverage < 0.75:
            raise ValueError(f"paper title mismatch for {name}, overlap={coverage}")
        result.append({
            "pdf_filename": name, "pdf_sha256": declared,
            "pages": pages, "page_count": len(pages),
            "old_tei_filename": matched[name], "paper_meta": meta,
            "title_token_overlap": round(coverage, 3),
        })
    return result, {
        "corpus": corpus.name, "source_manifest_sha256": sha256(manifest_path),
        "crosswalk_sha256": sha256(crosswalk), "pdf_count": len(result),
        "source_bytes": total_bytes,
        "max_files": max_files, "max_source_bytes": max_bytes,
        "max_pages_per_pdf": max_pages,
    }


def build(stage: Path, rows: list[dict]) -> list[dict]:
    (stage / "chunk_sets").mkdir()
    (stage / "catalog").mkdir()
    (stage / "review").mkdir()
    report = []
    for row in rows:
        meta = dict(row["paper_meta"])
        uid = meta["paper_uid"]
        meta["paper_id"] = meta.get("paper_id") or uid
        meta["source_file"] = row["pdf_filename"]
        meta["source_ref"] = "sha256:" + row["pdf_sha256"]
        meta["source_sha256"] = row["pdf_sha256"]
        meta["source_kind"] = "registered_pdf_exact_bytes"
        meta["parser"] = "poppler-pdftotext-layout-page-bounded-v1"
        meta["metadata_witness"] = "old_tei_title_and_authors_pdf_first_pages_checked"
        # Never claim the old TEI was created from this PDF.
        chunks = page_chunks(
            pages=row["pages"], uid=uid, paper_id=meta["paper_id"],
            filename=row["pdf_filename"], source_sha=row["pdf_sha256"],
        )
        cp = write_chunk_set_artifact(
            chunks, source_items=[row["pdf_filename"]], run_id=uid,
            out_dir=stage / "chunk_sets", entrypoint="registered_pdf_page_rebuild_v1",
            paper_meta=meta,
        )
        written = json.loads(cp.read_text(encoding="utf-8"))
        if (written.get("chunk_count") != len(chunks) or
            any(c.get("paper_uid") != uid or
                c.get("metadata", {}).get("source_sha256") != row["pdf_sha256"]
                for c in written["chunks"])):
            raise ValueError(f"bad generated source identity: {uid}")
        report.append({
            "pdf": row["pdf_filename"], "paper_uid": uid,
            "source_sha256": row["pdf_sha256"],
            "page_count": row["page_count"], "chunks": len(chunks),
            "title_token_overlap": row["title_token_overlap"],
            "chunk_set_sha256": sha256(cp),
        })
    catalog = export_catalog_records(
        chunk_set_dir=stage / "chunk_sets",
        out_path=stage / "catalog/paper.catalog-record.v1.jsonl")
    review = export_review_records(
        chunk_set_dir=stage / "chunk_sets",
        out_path=stage / "review/paper.review-record.v1.jsonl")
    if len(report) != len({x["paper_uid"] for x in report}):
        raise ValueError("duplicate paper UID in rebuilt sources")
    return report


def promote(corpus: Path, stage: Path, *, receipt: dict) -> None:
    backup = corpus / "private_migration_history" / "pdf_lineage_20261028"
    if backup.exists():
        raise FileExistsError(f"migration already exists: {backup}")
    backup.parent.mkdir(exist_ok=True)
    backup.mkdir()
    names = ("chunk_sets", "catalog", "review", "xmls")
    swapped = []
    try:
        for name in names:
            target = corpus / name
            old = backup / name
            if target.exists():
                target.rename(old)
            if name == "xmls":
                target.mkdir()  # keep old TEI and failure logs archived, no active false failures
            else:
                (stage / name).rename(target)
            swapped.append(name)
        test = subprocess.run(
            [sys.executable, "-m", "pipeline.adapter.manager", "doctor",
             "--corpus", corpus.name, "--strict", "--json"],
            cwd=ROOT, text=True, capture_output=True, timeout=50,
        )
        if test.returncode:
            raise RuntimeError(f"strict doctor failed: {test.stdout[-1200:]} {test.stderr[-300:]}")
        payload = json.loads(test.stdout)
        if (payload.get("chunk_set_validation", {}).get("n_fail") != 0 or
            payload.get("n_failures") != 0 or
            payload.get("chunk_set_validation", {}).get("n_pass") != receipt["pdf_count"]):
            raise RuntimeError("strict doctor returned unexpected QA counts")
        receipt["doctor"] = {
            "n_pass": payload["chunk_set_validation"]["n_pass"],
            "n_fail": payload["chunk_set_validation"]["n_fail"],
            "n_failures": payload["n_failures"],
            "n_pdfs": payload["n_pdfs"], "n_xmls": payload["n_xmls"],
            "null_header_path_count": payload.get("null_header_path_count"),
        }
        (backup / "migration_receipt.json").write_text(
            json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
        )
    except Exception:
        for name in reversed(swapped):
            current = corpus / name
            if current.is_dir():
                current.rename(stage / ("failed_" + name))
            prior = backup / name
            if prior.exists():
                prior.rename(current)
        raise


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", default="tesis-cited")
    p.add_argument("--crosswalk", type=Path)
    p.add_argument("--apply", action="store_true", help="stage and replace only generated derivatives")
    p.add_argument("--max-files", type=int, default=19)
    p.add_argument("--max-bytes", type=int, default=120_000_000)
    p.add_argument("--max-pages", type=int, default=250)
    a = p.parse_args()
    corpus = ROOT / "corpora" / a.corpus
    crosswalk = a.crosswalk or corpus / "private_pdf_tei_crosswalk_20261028.json"
    rows, receipt = plan(corpus, crosswalk,
                         max_files=a.max_files, max_bytes=a.max_bytes,
                         max_pages=a.max_pages)
    receipt["source_preservation"] = "original_pdf_bytes_and_manifest_unmodified"
    receipt["source_pdf_sha256s"] = {r["pdf_filename"]: r["pdf_sha256"] for r in rows}
    if not a.apply:
        print(json.dumps({k: v for k, v in receipt.items()
                          if k != "source_pdf_sha256s"}, indent=2))
        print("DRY_RUN_PASS; apply required for derivative regeneration")
        return
    with tempfile.TemporaryDirectory(prefix=".pdf_rebuild_stage_", dir=corpus) as temp:
        stage = Path(temp)
        report = build(stage, rows)
        receipt["papers"] = report
        receipt["chunk_count"] = sum(r["chunks"] for r in report)
        promote(corpus, stage, receipt=receipt)
    # Read the original manifesto and source bytes AGAIN; no silent mutation.
    for row in rows:
        assert sha256(corpus / "pdfs" / row["pdf_filename"]) == row["pdf_sha256"]
    print(json.dumps({k: receipt[k] for k in ("corpus", "pdf_count",
                                               "chunk_count", "doctor")}, indent=2))
    print("PDF_FIRST_REBUILD_PASS")

if __name__ == "__main__":
    main()
