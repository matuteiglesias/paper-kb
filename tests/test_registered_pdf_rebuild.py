"""Synthetic, source-free tests for the bounded PDF lineage migration."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rebuild_registered_pdf_chunks import page_chunks, title_witness


def test_page_chunks_keep_paper_anchor_and_source_sha() -> None:
    sha = hashlib.sha256(b"synthetic source").hexdigest()
    text = ("Synthetic economic estimation with public measures. " * 46).strip()
    kwargs = dict(pages=[text, "", text], uid="paper_synthetic",
                  paper_id="original_ref", filename="test.pdf", source_sha=sha)
    out = page_chunks(**kwargs)
    again = page_chunks(**kwargs)
    assert out == again
    assert {x["document_id"] for x in out} == {"paper_synthetic"}
    assert {x["paper_uid"] for x in out} == {"paper_synthetic"}
    assert {x["source_ref"] for x in out} == {"sha256:" + sha}
    assert {x["metadata"]["page_start"] for x in out} == {1, 3}
    assert len({x["chunk_id"] for x in out}) == len(out)


def test_changed_pdf_bytes_changes_chunk_ids() -> None:
    data = ["Synthetic methodology and evidence. " * 100]
    common = dict(pages=data, uid="paper_one", paper_id="p1", filename="one.pdf")
    left = page_chunks(source_sha="a" * 64, **common)
    right = page_chunks(source_sha="b" * 64, **common)
    assert {x["chunk_id"] for x in left}.isdisjoint(
        {x["chunk_id"] for x in right}
    )


def test_title_check_rejects_different_document() -> None:
    assert title_witness(
        "Granular Growth in Firms", "GRANULAR GROWTH IN FIRMS; examples and results"
    ) == 1.0
    assert title_witness(
        "Granular Growth in Firms", "Some unrelated Bayesian probability text"
    ) < 0.75


def test_fails_closed_on_empty_and_overlong() -> None:
    try:
        page_chunks(pages=["", "\n"], uid="paper_1", paper_id="id",
                    filename="x.pdf", source_sha="a" * 64)
    except ValueError as exc:
        assert "no usable" in str(exc)
    else:
        raise AssertionError("empty input was accepted")

    try:
        page_chunks(pages=["Synthetic page fragment " * 500], uid="paper_1",
                    paper_id="id", filename="x.pdf", source_sha="a" * 64,
                    max_chunks=1)
    except ValueError as exc:
        assert "budget" in str(exc)
    else:
        raise AssertionError("chunk count exceeded without failure")
