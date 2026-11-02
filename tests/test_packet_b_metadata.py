"""Bounded Packet B contracts: no provider network or real corpus changes."""
from __future__ import annotations

import argparse
import hashlib
import json
import urllib.parse
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from scripts import tse_chronicle as tse


def _work(id: str, doi: str | None = None) -> dict:
    return {"id": id, "doi": doi, "authorships": [], "title": "Synthetic", "publication_year": 2024}


def _page(results, count, cursor):
    return json.dumps({"meta": {"count": count, "next_cursor": cursor}, "results": results}).encode()


def test_openalex_complete_cursor_paging_and_secret_not_in_paths(tmp_path, monkeypatch):
    calls = []
    def fetch(url, cache=None, *, headers=None, **kwargs):
        calls.append((url, cache, headers))
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert params["per-page"] == ["100"]
        return _page([_work("W1"), _work("W2")], 3, "NEXT") if params["cursor"] == ["*"] else _page([_work("W3")], 3, None)
    monkeypatch.setattr(tse, "get", fetch)
    works, receipt = tse.openalex_work_pages(tmp_path, api_key="SECRET-TEST-KEY")
    assert [w["id"] for w in works] == ["W1", "W2", "W3"]
    assert receipt == {"expected": 3, "received": 3, "pages": 2, "status": "COMPLETE"}
    assert len(calls) == 2
    assert all(c[2]["Authorization"] == "Bearer SECRET-TEST-KEY" for c in calls)
    assert all("SECRET-TEST-KEY" not in c[0] + str(c[1]) for c in calls)


def test_openalex_missing_key_or_bad_cursor_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(tse, "get", lambda *args, **kwargs: _page([_work("W1")], 2, "NEXT"))
    with pytest.raises(RuntimeError, match="OPENALEX_API_KEY"):
        tse.openalex_work_pages(tmp_path, api_key=None)
    # The malformed provider sends a different work on page two but repeats
    # the continuation cursor, so the cursor-loop check is the first failure.
    calls = 0
    def repeating(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _page([_work(f"W{calls}")], 4, "NEXT")
    monkeypatch.setattr(tse, "get", repeating)
    with pytest.raises(RuntimeError, match="repeated cursor"):
        tse.openalex_work_pages(tmp_path, api_key="test")


def test_openalex_inconsistent_total_or_duplicate_id_fails(tmp_path, monkeypatch):
    calls = 0
    def fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return _page([_work("W1")], 3, "NEXT") if calls == 1 else _page([_work("W1")], 3, None)
    monkeypatch.setattr(tse, "get", fetch)
    with pytest.raises(RuntimeError, match="duplicated"):
        tse.openalex_work_pages(tmp_path, api_key="test")


def test_normalized_doi_matches_url_bare_and_prefix():
    assert tse.normalized_doi("https://doi.org/10.1234/AbC") == "10.1234/abc"
    assert tse.normalized_doi("doi:10.1234/ABC") == "10.1234/abc"
    assert tse.normalized_doi("10.1234/abc") == "10.1234/abc"
    assert tse.normalized_doi("not-a-doi") is None


def test_get_retries_transient_http_and_caches_only_complete_response(tmp_path, monkeypatch):
    attempts = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"ok":true}'
    def fetch(request, timeout):
        attempts.append(request)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "rate-limited", {}, None)
        return Response()
    monkeypatch.setattr(tse.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(tse.time, "sleep", lambda seconds: None)
    cache = tmp_path / "reply.json"
    assert tse.get("https://api.openalex.org/test", cache) == b'{"ok":true}'
    assert len(attempts) == 2
    assert tse.get("https://api.openalex.org/test", cache) == b'{"ok":true}'
    assert len(attempts) == 2


def test_failed_refresh_preserves_accepted_snapshot(tmp_path, monkeypatch):
    old = tmp_path / "acquisition"
    old.mkdir()
    (old / "run_manifest.json").write_text('{"historic":"accepted"}')
    old_hash = hashlib.sha256((old / "run_manifest.json").read_bytes()).hexdigest()
    monkeypatch.setattr(tse, "OUT", tmp_path)
    monkeypatch.setattr(tse, "get", lambda *args, **kwargs: b"<html></html>")
    monkeypatch.setattr(tse, "parse_first_party_page", lambda year, raw: [{"year": year}])
    monkeypatch.setattr(tse, "openalex_work_pages", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("test failure")))
    args = argparse.Namespace(max_crossref=0, sleep=0, max_openalex_pages=10)
    with pytest.raises(RuntimeError, match="previous accepted snapshot preserved"):
        tse.acquire(args)
    assert hashlib.sha256((old / "run_manifest.json").read_bytes()).hexdigest() == old_hash
    assert json.loads((tmp_path / "acquisition_attempt.json").read_text())["core"] == "FAILED"


def test_crossref_optional_failure_marks_enrichment_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(tse, "OUT", tmp_path)
    monkeypatch.setattr(tse, "get", lambda *args, **kwargs:
        b"<html/>" if "publications." in args[0] else (_ for _ in ()).throw(OSError("synthetic crossref outage")))
    monkeypatch.setattr(tse, "parse_first_party_page", lambda year, raw: [{"year": year}])
    monkeypatch.setattr(tse, "openalex_work_pages", lambda *args, **kwargs:
        ([_work("W1", "https://doi.org/10.1234/a")],
         {"expected": 1, "received": 1, "pages": 1, "status": "COMPLETE"}))
    args = argparse.Namespace(max_crossref=1, sleep=0, max_openalex_pages=10)
    result = tse.acquire(args)
    assert result["quality"]["core"] == "COMPLETE"
    assert result["quality"]["crossref_enrichment"] == "PARTIAL_FAILURE"
    assert result["quality"]["crossref_failed"] == 1
    assert tse.ensure_complete_acquisition(tmp_path / "acquisition")["schema_version"] == 2


def test_unverified_legacy_acquisition_rejected_before_build(tmp_path):
    old = tmp_path / "acquisition"
    old.mkdir()
    (old / "run_manifest.json").write_text('{"schema_version":1,"files":{}}')
    with pytest.raises(RuntimeError, match="unverified"):
        tse.ensure_complete_acquisition(old)


def test_crosswalk_ambiguous_doi_keeps_both_source_manifestations(tmp_path):
    paths = []
    for i in [1, 2]:
        corpus = tmp_path / f"corpus{i}" / "chunk_sets"
        corpus.mkdir(parents=True)
        path = corpus / f"p{i}.chunk_set.json"
        path.write_text(json.dumps({"paper_meta": {
            "paper_uid": f"uid-{i}", "paper_id": f"legacy-{i}", "title": "A Work",
            "year": 2024, "doi": "10.1234/A",
            "source_sha256": f"sha-{i}",
        }}))
        paths.append(path)
    records = [{"chronicle_id": "tse-1", "title": "A Work", "year": 2024,
                "doi": "https://doi.org/10.1234/a", "paper_uid": None,
                "paper_kb_state": "CHRONICLE_METADATA_ONLY"}]
    links = tse.link_paper_kb(records, chunk_set_paths=paths)
    assert len(links) == 2
    assert {x["paper_uid"] for x in links} == {"uid-1", "uid-2"}
    assert {x["source_sha256"] for x in links} == {"sha-1", "sha-2"}
    assert {x["match_status"] for x in links} == {"AMBIGUOUS"}
    assert all(x["relationship"] == "bibliographic_association" and not x["verified_manifestation"] for x in links)
    assert records[0]["paper_uid"] is None
    assert records[0]["paper_kb_state"] == "BIBLIOGRAPHIC_ASSOCIATION_AMBIGUOUS"


def test_crosswalk_title_year_remains_candidate_only(tmp_path):
    path = tmp_path / "corpus" / "chunk_sets" / "a.chunk_set.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"paper_meta": {"paper_uid": "uid-a", "title": "A Work", "year": 2024}}))
    rows = [{"chronicle_id": "tse-1", "title": "A Work", "year": 2024,
             "doi": None, "paper_uid": None}]
    links = tse.link_paper_kb(rows, chunk_set_paths=[path])
    assert len(links) == 1 and links[0]["match_basis"] == "title_year_candidate"
    assert not links[0]["verified_manifestation"]
    assert rows[0]["paper_uid"] is None


def test_reconcile_keeps_title_year_only_provider_records_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(tse, "OUT", tmp_path)
    folder = tmp_path / "acquisition"
    folder.mkdir()
    tse.write_jsonl(folder / "tse_first_party.jsonl", [{
        "provider": "tse_first_party", "provider_record_id": "fp-1",
        "title": "Same Title", "year": 2024, "authors": [{"name": "Ada Sample"}],
        "source_url": "https://example.invalid/fp", "institution_claim": {"claim_kind": "first_party_listing"},
    }])
    tse.write_jsonl(folder / "openalex.jsonl", [{
        "id": "https://openalex.org/W123",
        "title": "Same Title", "publication_year": 2024, "doi": None,
        "authorships": [{"author": {"display_name": "Ada Sample", "id": "https://openalex.org/A1"}}],
    }])
    tse.write_jsonl(folder / "crossref.jsonl", [])
    records = tse.reconcile()
    assert len(records) == 2
    assert len({r["chronicle_id"] for r in records}) == 2
    assert {r["membership_evidence"] for r in records} == {
        "first_party_listing", "provider_affiliation_metadata",
    }
    assert all(r["paper_uid"] is None for r in records)


def test_reconcile_crossref_doi_url_equivalence_and_field_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(tse, "OUT", tmp_path)
    folder = tmp_path / "acquisition"
    folder.mkdir()
    tse.write_jsonl(folder / "tse_first_party.jsonl", [{
        "provider": "tse_first_party", "provider_record_id": "fp-2",
        "title": "A DOI Work", "year": 2024,
        "doi": "10.1234/ex", "authors": [{"name": "Ada Sample"}],
        "institution_claim": {"claim_kind": "first_party_listing"},
    }])
    tse.write_jsonl(folder / "openalex.jsonl", [{
        "id": "https://openalex.org/W2", "title": "A DOI Work",
        "publication_year": 2024, "doi": "https://doi.org/10.1234/EX",
        "authorships": [{"author": {"display_name": "Ada Sample"}}],
        "primary_location": {"source": {"display_name": "Example Journal"}},
    }])
    tse.write_jsonl(folder / "crossref.jsonl", [{
        "DOI": "10.1234/ex", "abstract": "<p>Crossref abstract</p>",
    }])
    records = tse.reconcile()
    assert len(records) == 1
    record = records[0]
    assert record["normalized_doi"] == "10.1234/ex"
    assert record["abstract"] == "Crossref abstract"
    assert record["field_sources"]["title"]["provider"] == "tse_first_party"
    assert record["field_sources"]["abstract"]["provider"] == "crossref"
    assert record["field_sources"]["venue_or_display"]["provider"] == "openalex"


def test_incomplete_first_party_year_does_not_promote_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(tse, "OUT", tmp_path)
    monkeypatch.setattr(tse, "get", lambda *args, **kwargs: b"<html/>")
    monkeypatch.setattr(
        tse, "parse_first_party_page",
        lambda year, raw: [] if year == 2020 else [{"year": year}],
    )
    monkeypatch.setattr(
        tse, "openalex_work_pages",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("OpenAlex must not run with incomplete first-party coverage")
        ),
    )
    with pytest.raises(RuntimeError, match="Incomplete TSE acquisition"):
        tse.acquire(argparse.Namespace(max_crossref=0, sleep=0, max_openalex_pages=10))
    assert not (tmp_path / "acquisition").exists()
    diagnostic = json.loads((tmp_path / "acquisition_attempt.json").read_text())
    assert diagnostic["core"] == "FAILED"
    assert 2020 not in diagnostic["first_party_years_completed"]


def test_complete_manifest_detects_checksum_tampering(tmp_path):
    folder = tmp_path / "acquisition"
    folder.mkdir()
    original = b"safe"
    (folder / "openalex.jsonl").write_bytes(original)
    (folder / "run_manifest.json").write_text(json.dumps({
        "schema_version": 2, "quality": {"core": "COMPLETE"},
        "files": {"openalex.jsonl": hashlib.sha256(original).hexdigest()},
    }))
    assert tse.ensure_complete_acquisition(folder)["quality"]["core"] == "COMPLETE"
    (folder / "openalex.jsonl").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="checksum"):
        tse.ensure_complete_acquisition(folder)


def test_openalex_reports_missing_expected_count(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tse, "get",
        lambda *args, **kwargs: json.dumps({
            "meta": {"next_cursor": None}, "results": [_work("W1")],
        }).encode(),
    )
    with pytest.raises(RuntimeError, match="count absent"):
        tse.openalex_work_pages(tmp_path)


def test_crosswalk_exposes_external_claim_identity(tmp_path):
    path = tmp_path / "c" / "chunk_sets" / "a.chunk_set.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "paper_meta": {"paper_uid": "local-manifestation",
                       "doi": "doi:10.1234/EXTERNAL", "source_sha256": "pdf-bytes"},
    }))
    records = [{
        "chronicle_id": "chronicle-a",
        "doi": "https://doi.org/10.1234/external",
        "sources": [{"provider": "openalex", "provider_record_id": "W123"}],
        "paper_uid": None,
    }]
    links = tse.link_paper_kb(records, chunk_set_paths=[path])
    assert links[0]["external_records"] == [{
        "provider": "openalex", "provider_record_id": "W123",
    }]
    assert links[0]["candidate_count"] == 1
    assert links[0]["source_sha256"] == "pdf-bytes"
    assert records[0]["paper_uid"] is None


def test_title_year_does_not_override_conflicting_doi(tmp_path):
    local = tmp_path / "c" / "chunk_sets" / "a.chunk_set.json"
    local.parent.mkdir(parents=True)
    local.write_text(json.dumps({
        "paper_meta": {
            "paper_uid": "local-a", "title": "Shared Title", "year": 2024,
            "doi": "10.1234/local",
        },
    }))
    rows = [{"chronicle_id": "remote", "title": "Shared Title",
             "year": 2024, "doi": "10.1234/different", "paper_uid": None}]
    links = tse.link_paper_kb(rows, chunk_set_paths=[local])
    assert links == []
    assert rows[0]["paper_uid"] is None


def test_annual_first_party_listing_does_not_invent_publication_day():
    html = b'<p><a href="/id/eprint/123">Synthetic Publication</a> ' \
           b'<span class="person_name">Ada Example</span></p>'
    records = tse.parse_first_party_page(2024, html)
    assert len(records) == 1
    assert records[0]["year"] == 2024
    assert records[0]["published_date"] is None
    assert records[0]["provider"] == "tse_first_party"
