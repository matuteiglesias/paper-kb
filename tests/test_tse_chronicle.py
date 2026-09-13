import json
from pathlib import Path

from scripts.tse_chronicle import author_overlap_key, reconstruct_abstract, sha


def test_openalex_abstract_reconstruction_is_deterministic():
    inv = {"world": [1], "Hello": [0], "again": [2]}
    assert reconstruct_abstract(inv) == "Hello world again"


def test_author_overlap_normalizes_order_and_accents():
    assert author_overlap_key("Saint-Paul, Gilles") == author_overlap_key("Gilles Saint‐Paul")


def test_chronicle_hash_is_key_order_independent():
    assert sha({"b": 2, "a": 1}) == sha({"a": 1, "b": 2})


def test_real_manifest_and_year_coverage_exist():
    root = Path(__file__).resolve().parents[1] / "artifacts" / "institution-chronicle" / "tse"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["coverage"]["from_date"] == "2010-01-01"
    assert manifest["coverage"]["through_date"] == "2026-09-13"
    assert all((root / "years" / f"{year}.md").exists() for year in range(2010, 2027))
