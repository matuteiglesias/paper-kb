"""GROBID provenance contract for future TEI production (fully synthetic)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, patch

from pipeline.adapter.grobid_ingest import generate_teis_from_pdfs, processing_receipt


def test_receipt_distinguishes_consolidation_and_exact_source_bytes(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    tei = tmp_path / "output.tei.xml"
    disabled = processing_receipt(pdf, tei, "<TEI/>", consolidate_header=False,
                                  version="test-build")
    enabled = processing_receipt(pdf, tei, "<TEI/>", consolidate_header=True,
                                 version=None, extracted_doi="10.1234/test")
    assert disabled["input_pdf_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert disabled["tei_sha256"] == hashlib.sha256(b"<TEI/>").hexdigest()
    assert disabled["consolidate_header"] == "0"
    assert disabled["external_enrichment"] == "DISABLED"
    assert enabled["consolidate_header"] == "1"
    assert enabled["external_enrichment"] == "POSSIBLE_NOT_VERIFIED"
    assert enabled["field_level_metadata_origin"] == "NOT_OBSERVABLE"
    assert enabled["grobid_version_status"] == "UNKNOWN"
    assert enabled["observed_tei_doi"] == "10.1234/test"
    assert "paper_uid" not in enabled


def test_grobid_runner_writes_receipt_and_protects_existing_tei(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    pdf = source / "sample.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    out = tmp_path / "tei"
    tei = '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body/></text></TEI>'
    with patch("pipeline.adapter.grobid_ingest.post_pdf_to_grobid", return_value=tei), \
         patch("pipeline.adapter.grobid_ingest.grobid_version", return_value="0.9.0-test"), \
         patch("pipeline.adapter.grobid_ingest.parse_tei_text", return_value={
             "title": "Sample Title", "doi": "10.1234/synthetic",
         }):
        result = generate_teis_from_pdfs(source, out, consolidate_header=False)
        assert result["n_success"] == 1
        success = result["successes"][0]
        receipt = json.loads(Path(success["receipt"]).read_text())
        assert receipt["input_pdf_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
        assert receipt["consolidate_header"] == "0"
        assert receipt["grobid_version"] == "0.9.0-test"
        assert receipt["tei_sha256"] == hashlib.sha256(
            Path(success["tei"]).read_bytes()
        ).hexdigest()
        old = Path(success["tei"]).read_bytes()
        again = generate_teis_from_pdfs(source, out, consolidate_header=False)
    assert again["n_success"] == 0
    assert again["n_failures"] == 1
    assert Path(success["tei"]).read_bytes() == old
