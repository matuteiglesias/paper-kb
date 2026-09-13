# TSE Chronicle implementation report

## Result

Generated a real static Research Center Chronicle for Toulouse School of Economics covering 2010-01-01 through 2026-09-13.

- 10,178 reconciled Chronicle records
- 4,741 records with first-party TSE listing evidence
- 5,437 external-affiliation-only records retained after a first-party author-roster overlap guard
- 3,555 abstracts
- 9,275 display-level authors
- 1,726 working papers
- 17 annual pages (2010–2026)
- 0 exact matches to existing governed Paper KB chunk sets

The first-party archive produced 4,750 rows. OpenAlex produced 11,065 raw rows for institution `https://openalex.org/I4210092408`; 5,449 passed the author-roster overlap gate before reconciliation. Fourteen successful Crossref DOI responses were retained as enrichment. A summarized Crossref partial-enrichment failure is retained because further requests encountered rate limiting/timeouts.

## Authorities and architecture

- First-party listing: Toulouse Capitole Publications, TSE division, one bounded page per year.
- External discovery/enrichment: OpenAlex institution-filtered works.
- DOI enrichment: Crossref, bounded and cached.
- Canonical Paper KB authority: unchanged. External metadata did not become `paper.catalog-record@1` or `paper.review-record@1`.
- Renderer: local static Markdown + HTML, because the Chronicle includes metadata-only records and Abstract Scroller’s governed review-record input would be dishonest for those rows.

The verified institution identifiers are OpenAlex `I4210092408` and ROR `00ff5f522`, recorded with their resolution evidence in `config/chronicle/tse_institution.json`.

## Identity and coverage

DOI is the strongest cross-provider key; TSE working-paper number is retained separately; otherwise provider identity is preserved. Exact title/year matching is used only to connect a non-DOI OpenAlex row to one first-party row. Title similarity is not silently merged.

Working-paper/article relationships are not invented where the sources do not expose an explicit relation. They remain separate discoverable records.

OpenAlex affiliation data was demonstrably noisy: one raw result had a music dissertation and a “Toulouse School of Graduate” affiliation while carrying the TSE institution ID. The raw snapshot is preserved; the Chronicle projection requires first-party author-name overlap for external-only rows. This reduces noise but can miss new TSE authors absent from the first-party archive, which is an explicit coverage gap.

## Paper KB promotion

No existing governed chunk-set record matched the reconciled TSE records by DOI or unique exact title. Therefore all Chronicle rows remain `CHRONICLE_METADATA_ONLY`. No PDF was downloaded, no canonical Paper KB `paper_uid` was minted, and no external catalog/review records were fabricated.

## Products

The generated artifact tree is under `artifacts/institution-chronicle/tse/` and includes acquisition snapshots, reconciliation, papers/authors/topics JSONL, annual Markdown pages, topic chronology, latest page, coverage notes, Paper KB promotion report, QA, and `rendered/index.html`.

## Tests and rerun

- `python3 -m py_compile scripts/tse_chronicle.py`
- `PYTHONPATH=. pytest -q tests/test_tse_chronicle.py` — 4 passed
- `python3 scripts/tse_chronicle.py qa` — pass
- unchanged-input rebuild: `papers.jsonl` and `rendered/index.html` byte-identical on the second build (`d85f0f…` and `5fbd439…` respectively)
- `refresh.json` on the unchanged rebuild: `new=0`, `changed=0`, `removed=0`, `unchanged=10178`
- source snapshots are cached and hashed in `acquisition/run_manifest.json`
- existing Paper KB dirty work was preserved; no unrelated files were reset, cleaned, stashed, or rewritten

## Deliberately not built

No institutional database, crawler framework, search engine, embeddings, vector store, author-identity resolver, new Paper KB identity path, backend, SPA, or Abstract Scroller compatibility shim was added.

## Remaining gaps

The first-party archive may not be historically exhaustive; external affiliation coverage is noisy; Crossref enrichment is partial; topic tagging is deliberately lexical and reviewable rather than semantic; and metadata-only works have not been promoted into Paper KB without an approved governed source document.
