# Paper KB Packet B — metadata integration and provenance contracts

Status: implemented in code, 2026-11-02. Owner: Paper KB repository. This is
an **institution-specific TSE integration plus future GROBID processing receipts**,
not a new corpus authority, persistent identity registry, or generic ingestion
framework.

## B1: TSE acquisition completeness

`scripts/tse_chronicle.py acquire` continues to cover the intentionally frozen
`2010-01-01` to `2026-09-13` window, rather than silently using today's date.

- OpenAlex works use `per-page=100` with cursor pagination. An
  `OPENALEX_API_KEY` from the operator's environment is supplied as an
  `Authorization: Bearer` request header, never in URLs, cache filenames,
  receipts, or logs. Following a deep cursor without a key fails explicitly.
- The complete result count, unique work IDs, page budget and cursor progress
  are checked. Missing/inconsistent counts, duplicate IDs, empty partial pages,
  repeated cursors, or exhausted page budget reject the acquisition.
- HTTP 429 and transient 5xx/network failures use a small bounded retry and
  backoff. HTTP 401/403 remain failures, not empty successful results.
- All 17 first-party annual pages must parse at least one entry successfully. The live 2026 archive may change after the frozen OpenAlex through-date; the manifest records its retrieval time, marks first-party day-level cutoff as **unverified**, and never invents January 1 as an actual publication date for a year-only record.
- Core acquisition is staged in a temporary sibling directory. A failure
  writes a sanitized `artifacts/institution-chronicle/tse/acquisition_attempt.json`
  diagnostic and leaves the prior accepted acquisition intact. Only a verified
  complete run replaces the accepted `acquisition/` snapshot.
- Crossref responses must contain the DOI requested; mismatched provider responses are recorded as enrichment failures instead of silently accepted metadata.
- The accepted run manifest v2 records independent `quality.core` and
  `quality.crossref_enrichment` states. Optional Crossref enrichment uses
  `COMPLETE`, `BOUNDED_COMPLETE`, `PARTIAL_FAILURE` or `NOT_REQUESTED`,
  with eligible/requested/failed counts. `CROSSREF_MAILTO` is optional.
- `build` and `qa` require the accepted manifest's core completeness and
  verify acquisition file hashes. Historical v1 manifests are **unverified**:
  prior reports remain historical evidence, but future re-builds from those
  unverified files fail until a new accepted acquisition is produced.

No live acquisition or historical artifact regeneration was performed during
Packet B implementation. A new live refresh requires a valid OpenAlex API key,
network access, and the operator's intentional decision to spend API quota.

## B2: Bibliographic identity associations

Two distinct notions are preserved:

1. **Bibliographic association:** normalized DOI or exact title+year candidate
   points from a TSE Chronicle record to possible governed Paper KB papers.
2. **Source manifestation identity:** producer-owned `paper_uid` and, when
   registered, the exact PDF's `source_sha256`.

The Chronicle does **not** use bibliographic equality to assert that different
PDF manifestations, preprints, or parsing revisions are interchangeable.

- `normalized_doi()` handles bare DOI, `doi:`, and doi.org forms for
  comparison, while keeping raw provider strings in the original claims.
- First-party records and OpenAlex records with identical title/year but no
  shared DOI remain distinct; title/year is only candidate evidence.
- The existing TSE reconciliation records `field_sources` for title, authors,
  year, publication date, DOI, abstract, venue and URL. Crossref abstract
  enrichment is specifically attributed to Crossref.
- `link_paper_kb()` returns **all** matching candidates, including multiple
  distinct PDF manifestations sharing a DOI. Each assertion states its
  `match_basis`, `match_status`, `relationship`,
  `verified_manifestation=false`, originating corpus and available source
  hash. No Chronicle record receives a canonical `paper_uid` merely by
  matching metadata.
- `paper_kb_links.jsonl` is the small, non-authoritative derived crosswalk.
  A link is not a source registration, corpus promotion, or permission to merge
  existing `paper_uid` values.

Existing already-generated TSE Chronicle artifacts are untouched; their
historical count of links and prior QA results are not retroactively rewritten.

## B3: GROBID consolidation receipts

The `pipeline/adapter/grobid_ingest.py` API continues to support the existing
`consolidate_header=True` default (mode `1`) and the explicit
`--no-consolidate-header` switch (mode `0`).

GROBID offers four header consolidation modes: `0` (disabled), `1`
(enrich/correct metadata), `2` (DOI only), and `3` (match using extracted DOI
only). Paper KB deliberately retains the existing 0/1 interface for now.
Mode 0 is recommended for strict source-only experiments; it does not change
already-produced TEI or canonical IDs.

Newly generated TEI receives a sidecar
`<tei_dir>/receipts/<TEI basename>.provenance.json` containing:

- original PDF basename + SHA-256 of its exact bytes;
- TEI basename + SHA-256 of its serialized content;
- requested `consolidate_header` mode;
- best-effort GROBID service revision from `/api/version`, with
  explicit `UNKNOWN` when unavailable;
- permitted external enrichment state, and a **field-level origin UNKNOWN**
  indicator; observed DOI is not asserted to be from the source PDF;
- processing timestamp.

A second run on the same PDF/configuration does not silently overwrite the
prior TEI/receipt; `--force` is an explicit replacement operation. Other
sources with equal titles can get distinct TEI filenames.

The sidecar describes **new** GROBID processing. It cannot retroactively
prove that historical TEI files originated from the registered PDF.
The TEI producer's existing `make_paper_uid` precedence is intentionally
unchanged: changing it would require a separate corpus identity migration and
consumer-reindex plan.

## Verification (synthetic, no network)

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_packet_b_metadata.py tests/test_packet_b_grobid_receipts.py \
  tests/test_tse_chronicle.py tests/test_grobid_consolidation_policy.py
```

Existing architecture and catalog/review source contracts must continue to
pass. No real PDF or accepted Chronicle snapshot is part of this test.

Provider references:
- OpenAlex paging: https://help.openalex.org/api/paging/
- OpenAlex auth: https://help.openalex.org/api/authentication/
- GROBID API: https://grobid.readthedocs.io/en/latest/Grobid-service/
- Crossref REST practice: https://www.crossref.org/documentation/retrieve-metadata/rest-api/tips-for-using-the-crossref-rest-api/

B4 Zotero/CSL export and LlamaIndex/Haystack consumer adapters are deliberately
**out of scope**.
