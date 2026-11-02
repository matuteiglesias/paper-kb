# Bounded PDF-first chunking for RP-019 72-source selection (2026-10-28)

## Purpose / approval

User approved selecting approximately 60–80 PDFs **from the 140 paper curation** for chunking, without indiscriminately indexing the remaining ~60 PDFs. Research Group selects the explicit W-ID × Pursue Line set. Paper KB produces derived source-bound chunks. Source PDF bytes, source manifests, and original Paper KB datasets are never rewritten.

The approved selection resides at `~/repos/research-group-ops/results/VRG-019/CHUNK72_20261028/approved_chunk72.json`. The independent Research Group post-QA is written to `chunk72_postqa.json`. This file is informational, not a new shared contract.

## Extraction regime and tradeoffs

The tool `scripts/build_registered_pdf_page_chunks.py` executes a **bounded, deterministic Poppler PDF-first** pipeline:

1. Read one existing immutable Paper KB source manifest, and match each approved W-ID exactly to the registered PDF filename, SHA-256 and file size.
2. Refuse existing chunk sets, catalogs or review products rather than silently replacing earlier production and invalidating consumer IDs.
3. Use `pdfinfo` for exact physical-page count and `pdftotext -layout` to obtain selectable text. If PDF content has embedded form-feed characters that falsely split page sequences, extract each **physical page** with `pdftotext -f N -l N`, replacing embedded form feeds with whitespace, without inventing more pages.
4. Normalize whitespace on each original page; split around **1,600 characters at word boundaries**, with no overlap and no dropped nonempty source text from successfully extractable pages. Verify the concatenation of chunks reproduces every normalized page exactly.
5. Mint a producer-owned deterministic `paper_uid = oa_<OpenAlex Work ID>_<source SHA prefix>` identifying the *manifestation*, **not** claiming equivalence of preprint and version-of-record. The `chunk_id` depends on full source SHA, page, position and text. Every chunk has PDF source SHA, page start/end and page-normalized character range.
6. Write governed `chunk_set` artifacts with existing Paper KB writer, then existing catalog and review projections. Stage all products off to the side inside the corpus. Promote with rollback on failed **strict corpus doctor**. Verify the original manifest and all source hashes after successful promotion.
7. The Research Group independently checks all 72 PDF source hashes, all chunk IDs and paper UIDs, per-page text continuity, first physical-page text witness and 1:1 catalog/review projections.

**No GROBID service, OCR, embeddings, Chroma writes, scientific reading/refereeing, section semantics, or Research Umbrella C-state promotions.** Header paths intentionally remain blank for page-derived chunks. A printed page boundary is not a logical section; tables/figures and equations may be flattened by Poppler. This pipeline is for **textual retrieval and page anchoring**, not a substitute for close technical reading.

## Bounded execution

Five previously registered corpora, each with empty derivative directories, receive a subset of the 72 approved PDFs (4 + 4 + 17 + 22 + 25). Each corpus executes at most 32 PDFs / 120 MB of selected input / 240 pages per source / 9,500 chunks. No source acquisitions.

```bash
cd ~/Documents/paper-kb
python3 scripts/build_registered_pdf_page_chunks.py \
  --plan ~/repos/research-group-ops/results/VRG-019/CHUNK72_20261028/approved_chunk72.json \
  --corpus rg019-openalex-broad2-20261028
# The output is only a dry-run. --apply with explicit --receipt is necessary.
python3 -m pytest -q tests/test_pdf_first_page_chunk_batch.py
```

The process deliberately **does not rerun idempotently** once derivatives exist: already-produced identities should be verified and reused, not reminted or overwritten. The source PDF and the output chunk set are privacy- and copyright-sensitive and must not be pushed to Git. The Git revision contains only the parser, sanitized tests and architecture description.

The producer layer is Paper KB; Research Group owns source selection and scientific decisions. No change was made to KB Contracts' registered interoperability contracts. A future section-aware GROBID upgrade would require an explicit source/version and consumer reindex plan.
