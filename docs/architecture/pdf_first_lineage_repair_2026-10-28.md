# Registered PDF-first lineage repair — `tesis-cited` (2026-10-28)

## Decision and authority

**Producer:** Paper KB owns PDF source registration, per-manifestation `paper_uid`, stable document identity, paper metadata, PDF-backed chunks, provenance and catalog/review projections. **Shared:** `kb-contracts` owns only registered interoperability contracts and the preservation/opacity of producer-defined references. This repair does **not** alter shared schema, derivation algorithm or pinned release. **Research Group:** RP-019 owns bibliographic coverage, citations to source assets, source-to-question learning, and audits; it does not reassign producer paper/chunk identities or ingest copies as a second authority. **Consumers:** Knowledge Inspect and experiences read producer artifacts and do not mint replacement IDs.

**Lean model:** `paper_uid` identifies a Paper KB paper/registered source manifestation in its corpus. `source-manifest.json` registers a particular **PDF byte identity** (SHA-256). Each `chunk_id` identifies a bounded text fragment derived from that PDF; the chunk must link to paper UID, PDF SHA, and physical page. Semantic study/learning attaches to the paper/source with chunk/page witnesses, not to an anonymous chunk as if it were a distinct paper. Distinct PDF versions remain separate manifestations until an explicit version-equivalence decision; shared Knowledge Contracts never silently infer version aliases.

## Diagnosis

- Pre-repair RP-019 `LIT-LIB-01`: 80 registered PDFs (SHA checks 80/80), **61** direct PDF→chunk witnesses and **19** `tesis-cited` entries with TEI chunks but unverified PDF lineage.
- Original `tesis-cited`: 19 registered PDFs, 19 XMLs, 19 TEI-derived chunk sets / 1,909 chunks, 19 historical `xmls/failures/*.fail.json` for GROBID connection refused. Old `corpus-doctor --strict` accepted 19 schema-valid TEI chunk sets but could not certify their PDF provenance; empty `header_path` warnings are separate.
- All 19 TEI titles were matched one-to-one with 19 registered PDFs, each title token set appearing on the PDF first two physical pages (overlap 1.00). **Title match is not proof of byte-identical historic PDF→TEI derivation**: old TEI artifacts' original GROBID provenance remains unknown.
- Root cause: legacy TEI chunk-set source references pointed to `.tei.xml`, not to registered PDF SHA; failed historical GROBID run receipts coexisted. The earlier projection was correct to flag UNKNOWN instead of claiming absent chunks.

## Applied bounded remediation

- User authorized rerunning current producer standards on old inputs while preserving PDFs and not requiring archaeology-level compatibility.
- Exact 19 original PDF bytes and `source-manifest.json` retained unchanged: manifest SHA-256 `0d04ac9a1d91e1433fbf9686c03b9b4ebf80a5a5d7800f62b2af53ea2e3f5c23`; source size 83,788,324 bytes.
- `scripts/rebuild_registered_pdf_chunks.py` verified hashes, file set, PDF title witnesses, maximal 19 files/120 MB/250 pages; staged new PDF-first `chunk_set` products with `poppler pdftotext -layout` and page spans; used Paper KB's existing governed writer and catalog/review projections; only then swapped generated outputs with rollback on strict doctor failure. No GROBID service or API was launched.
- Old generated `chunk_sets`, `xmls` (including old failures), `catalog`, `review` preserved in private, gitignored `corpora/tesis-cited/private_migration_history/pdf_lineage_20261028/`, plus a local metadata-only migration receipt. Never commit these rights-sensitive derivatives.
- New results: **19/19** PDF SHA-linked chunk sets and **1,472** page-bounded chunks; 19/19 preserved `paper_uid`; 19 catalog + 19 review records; 0 active XML failures; strict Paper KB doctor 19 pass/0 fail, 0 fatal errors. Fewer chunks are due to different segmentation, not fewer PDFs. The original PDFs remain source truth. The page parser lacks TEI section headers (1,472 null `header_path` warnings) and suppresses page text shorter than 50 characters / trailing segments under 30 characters. This is a **known text-coverage limitation of the pilot**, not evidence of verbatim full-text coverage; the original PDFs and old derivatives are intact.
- Independent `scripts/verify_registered_pdf_lineage.py` checks *all* 19 PDF hashes, every chunk source SHA, paper UID and page span, global chunk IDs, all catalog/review UIDs, plus one physical page token witness per PDF. Result **19/19 PASS**, 1,472 unique chunk IDs; no scientific reading or semantic source authority is claimed.
- Research Group's **post-repair projection** separately recovered **80/80** manifested PDF→chunk sources, **0** outstanding TEI/PDF lineage gaps, and **11/11** cross-corpus PDF/page witnesses. The historical LIT-LIB-01 audit remains an unchanged pre-repair record.

## Reproduction, with bounded risks

Safe read-only verification:

```bash
cd ~/Documents/paper-kb
make corpus-validate CORPUS=tesis-cited
python3 scripts/verify_registered_pdf_lineage.py --corpus tesis-cited --max-files 19
make architecture-check
make read-model-identity
python3 -m pytest -q tests/test_registered_pdf_rebuild.py tests/test_verify_registered_pdf_lineage.py
```

`--apply` on the one-time `rebuild_registered_pdf_chunks.py` was executed once. It **fails closed if migration backup already exists**; it must not be used as a generic future rerun/overwrite command. To regenerate further, specify a new bounded source/parser version, decide whether existing `chunk_id` consumers can be refreshed, and stage a reviewed migration. Do not delete backups or original source PDFs in routine cleanup.

## Remaining scope / follow-ups

1. **Section-aware parsing is not restored**: no validated GROBID TEI headers. If needed, introduce a separately reviewed parser source-bound to the registered PDF and compare against page-only extraction.
2. **Short text tails can be omitted by the current page-chunk pilot**. PDFs are intact; if exact text coverage matters for retrieval, improve the parser with byte/text coverage witnesses and generate a new versioned derived set. Do not silently hand-edit generated chunks.
3. **Other 13 legacy no-manifest folders** were outside this specific 80-manifestation audit. Don't claim a global Paper KB archival clean bill beyond registered PDFs.
4. **Consumer reindex:** downstream Chroma/vector stores or snapshots may still contain historical `chunk_id` values. Paper-level `paper_uid` was preserved; reindex/version-pin any chunk-level consumer before assuming its old IDs resolve. No production index or external snapshot was rebuilt in this packet.
5. **Semantic publication matching:** one intellectual paper may have two distinct PDF manifestations (e.g. K+S precursor/accepted manuscript). Do not automatically merge by DOI or title.
