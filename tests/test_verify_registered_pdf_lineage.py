"""Synthetic fail-closed tests for PDF→paper→page provenance guards."""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from verify_registered_pdf_lineage import chunk_errors


def entry():
    sha = "a" * 64
    return {"chunk_id": "paper_1_p001_000_abcd", "paper_uid": "paper_1",
            "document_id": "paper_1", "text": "Synthetic public data",
            "source_file": "a.pdf", "source_ref": "sha256:" + sha,
            "metadata": {"source_sha256": sha, "page_start": 1, "page_end": 1}}


def call(chunk):
    return chunk_errors(chunk, paper_uid="paper_1",
                        pdf_name="a.pdf", sha="a" * 64, pages=3)


def test_valid_pdf_page_witness_links():
    assert not call(entry())


def test_invalid_pdf_sha_is_rejected():
    x = entry()
    x["source_ref"] = "sha256:" + "b" * 64
    assert any("source reference" in e for e in call(x))


def test_invalid_page_span_is_rejected():
    x = entry()
    x["metadata"]["page_start"] = 4
    assert any("span" in e for e in call(x))


def test_invalid_paper_uid_is_rejected():
    x = entry()
    x["paper_uid"] = "a_different_paper"
    assert any("paper/document" in e for e in call(x))
