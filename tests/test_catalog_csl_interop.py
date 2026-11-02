"""CSL/Zotero interoperability is one-way, synthetic, no account or corpus writes."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

from scripts.export_catalog_csl import as_csl, export_csl, read_catalog


def paper(uid="uid-1", **kwargs):
    return {
        "schema_id": "paper.catalog-record", "schema_version": 1,
        "paper_uid": uid, "paper_id": "legacy", "title": "A synthetic paper",
        "authors": ["María del Río", "Li Wei"], "abstract": "Synthetic abstract",
        "date": "2024-03", "year": 2024, "venue": "Journal of Experiments",
        "doi": "https://doi.org/10.1234/EXPERIMENT", "arxiv_id": None,
        "repec_id": None, "tags": [], "source_url": "https://example.invalid/article",
        **kwargs,
    }


def _jsonl(path, records):
    path.write_text("".join(json.dumps(x) + "\n" for x in records), encoding="utf-8")


def test_csl_authors_preserve_literal_names_and_no_abstract_by_default():
    result = as_csl(paper())
    assert result["id"] == "uid-1" and result["type"] == "document"
    assert result["author"] == [
        {"literal": "María del Río", "parse-names": False},
        {"literal": "Li Wei", "parse-names": False},
    ]
    assert result["issued"] == {"date-parts": [[2024, 3]]}
    assert result["container-title"] == "Journal of Experiments"
    assert result["DOI"] == "10.1234/EXPERIMENT"
    assert "abstract" not in result
    assert "arxiv_id" not in result


def test_three_bibliographic_cases_emit_csl_and_receipt(tmp_path):
    original = paper()
    working = {
        **paper("uid-2"), "title": "Undated working paper",
        "authors": [], "date": None, "year": None, "venue": None,
        "doi": None, "source_url": None,
    }
    ambiguity = {
        **paper("uid-3"), "title": "Unsplit or institutional author",
        "authors": ["Van der Meer", "Ministerio de Ciencia"], "year": 2022,
        "date": None, "doi": None, "venue": None,
    }
    inp = tmp_path / "catalog.jsonl"
    out = tmp_path / "library.csl.json"
    _jsonl(inp, [ambiguity, working, original])
    receipt = export_csl(inp, out, type_overrides={
        "uid-1": "article-journal", "uid-2": "report",
    })
    rendered = json.loads(out.read_text())
    assert [item["id"] for item in rendered] == ["uid-1", "uid-2", "uid-3"]
    assert [item["type"] for item in rendered] == [
        "article-journal", "report", "document",
    ]
    assert "issued" not in rendered[1]
    assert rendered[2]["issued"] == {"date-parts": [[2022]]}
    assert rendered[2]["author"][0]["literal"] == "Van der Meer"
    assert receipt["records"] == 3
    assert receipt["unknown_genres_exported_as_document"] == 1
    assert receipt["literal_authors"] == 4
    assert receipt["output_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert json.loads((tmp_path / "library.csl.json.receipt.json").read_text()) == receipt
    with pytest.raises(FileExistsError):
        export_csl(inp, out)
    assert export_csl(inp, out, force=True)["records"] == 3


def test_explicit_abstract_opt_in():
    item = as_csl(paper(), include_abstract=True)
    assert item["abstract"] == "Synthetic abstract"


@pytest.mark.parametrize("changed", [
    {"date": "2023-06-15", "year": 2024},
    {"date": "2024-02-30"},
    {"date": "not-a-real-date"},
])
def test_invalid_date_is_not_silently_guessed(changed):
    row = paper()
    row.update(changed)
    with pytest.raises(ValueError):
        as_csl(row)


def test_unknown_type_and_duplicate_identity_rejected(tmp_path):
    with pytest.raises(ValueError):
        as_csl(paper(), item_type="unproven-artifact-type")
    path = tmp_path / "catalog.jsonl"
    _jsonl(path, [paper(), paper()])
    with pytest.raises(ValueError, match="Duplicate paper_uid"):
        read_catalog(path)


def test_type_override_requires_existing_uid(tmp_path):
    inp = tmp_path / "catalog.jsonl"
    _jsonl(inp, [paper()])
    with pytest.raises(ValueError, match="unknown paper_uid"):
        export_csl(inp, tmp_path / "out.json", type_overrides={"missing": "report"})
    assert not (tmp_path / "out.json").exists()


def test_cli_works_without_zotero_account_or_network(tmp_path):
    inp = tmp_path / "catalog.jsonl"
    out = tmp_path / "bibliography.json"
    _jsonl(inp, [paper()])
    run = subprocess.run(
        [sys.executable, "-m", "scripts.export_catalog_csl",
         "--catalog-jsonl", str(inp), "--out", str(out)],
        text=True, capture_output=True,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(out.read_text())[0]["id"] == "uid-1"
    assert json.loads(run.stdout)["records"] == 1
