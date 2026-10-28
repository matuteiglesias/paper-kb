# Incremental PDF-first page chunking — Research Group RP-019 (28 October 2026)

## Purpose

Append new, explicitly selected Paper KB PDF sources to an **already populated corpus** without altering previously minted paper IDs, existing chunk bytes, or existing catalog and review records. The earlier `build_registered_pdf_page_chunks.py` intentionally refuses non-empty target derivative directories; it is retained unchanged. The new additive tool is `scripts/append_registered_pdf_page_chunks.py`.

## Selection and authority

The Research Group selection `~/repos/research-group-ops/results/VRG-019/CHUNK_REMAINDER_20261028/approved_remaining59.json` identified 59 sources (28+28+2+1) with exact OpenAlex WID, source manifest corpus, filename and SHA-256. Another selected PDF W7162095184 had already been chunked by the older producer as `paper_941aa51c21` (122 chunks). That existing UID **must be reused**, not reminted into the newer UID convention.

**Paper KB owns source and derivative identities.** This task makes no Research Umbrella scientific decisions, no new source registrations, no background inference and no embeddings.

## Safety contract

1. **Preflight**: require existing, non-empty chunk sets and catalog/review; verify unique old paper UIDs and their alignment to existing projections. Reject a source already in `source_items`. Check the new PDF's file size and full SHA against the immutable Paper KB source manifest.
2. **Stage** in a private temporary directory in the corpus: copy the existing chunk sets without modifying their bytes; create the new page-anchored chunks with the prior Poppler parser.
3. **Project** the union through Paper KB canonical catalog and review exporters. Require all existing catalog/review JSON rows to compare **equal by UID** to their pre-append content. Verify existing chunk-set SHA-256 unchanged, not merely the filename.
4. **Promote with rollback**: swap only `chunk_sets`, `catalog` and `review` directories in one bounded corpus transaction. On strict `corpus doctor` failure, restore all previous derivative directories. After PASS, re-verify the source manifest SHA, old chunk IDs/bytes and each new PDF SHA.
5. **Fail closed on repeat**: rerunning the command for an already chunked source raises FileExistsError, protecting consumer identity. No public PDFs/text committed to Git; source and chunk files remain private local artifacts.

Four corpus operations produced 59 new chunk sets and 4,322 new chunks without modifying the earlier 72 new-style source chunks. Including 122 chunks already linked to one legacy PDF, the 132 selected, physically available PDFs have 9,847 chunks. Six corpora (four changed, one old-wave unchanged, one reused historical) passed strict doctor checks.

## Tests / inspection

```bash
cd ~/Documents/paper-kb
python3 -m pytest -q tests/test_append_registered_pdf_page_chunks.py tests/test_pdf_first_page_chunk_batch.py tests/test_registered_pdf_rebuild.py tests/test_verify_registered_pdf_lineage.py
make architecture-check
make read-model-identity
python3 -B ~/repos/research-group-ops/results/VRG-019/CHUNK_REMAINDER_20261028/verify_all132.py
```

The Research Group's `all132_chunks_manifest.tsv` crosswalk identifies the **one original chunk-set identity** per selected PDF by source SHA, paper UID, current chunk-set SHA and PL. It does not imply that source reading has reached F3/F4 or that Paper KB has a vector search index. Page-first text can flatten equations, tables or layout. No OCR or section-aware parsing was performed.
