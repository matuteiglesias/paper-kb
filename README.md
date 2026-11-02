# Paper KB

Paper KB is a modular paper-knowledge monorepo. The durable center is the governed paper corpus; API, paper-specific derivations, paper-domain projections and the workbench are consumers or projections of that corpus rather than alternate sources of truth.

Start with:
- [`docs/architecture/paper-kb-component-boundaries.md`](docs/architecture/paper-kb-component-boundaries.md) — component authority and dependency direction;
- [`docs/architecture/paper-kb-api-read-contract.md`](docs/architecture/paper-kb-api-read-contract.md) — truthful read/query API semantics and identity preservation;
- [`docs/architecture/component-manifest.json`](docs/architecture/component-manifest.json) — machine-readable component map and enforced rules;
- [`pipeline/OWNERSHIP.md`](pipeline/OWNERSHIP.md) — corpus-pipeline ownership;
- [`docs/contracts/paper-review-record.md`](docs/contracts/paper-review-record.md) — producer-owned review contract;
- [`docs/contracts/paper-catalog-record.md`](docs/contracts/paper-catalog-record.md) — producer-owned catalog/bibliography contract.

## Register an existing local PDF directory

Approved local PDF folders can be turned into named Paper KB corpus inputs without manually recreating the corpus layout.

Preview first:

```bash
make corpus-register-dry-run CORPUS=fcv-literature SOURCE_DIR=/absolute/path/to/pdfs
```

Register the exact byte set:

```bash
make corpus-register CORPUS=fcv-literature SOURCE_DIR=/absolute/path/to/pdfs
```

Registration recursively discovers PDFs, fails closed on duplicate basenames, hashes every input, copies the verified bytes into `corpora/<name>/pdfs/`, and writes `corpora/<name>/source-manifest.json`. The manifest records relative source names, sizes and SHA-256 hashes but deliberately does not record the absolute local source path.

Re-registering the same byte set is idempotent. If the input set changes, registration fails rather than mixing new source bytes with stale TEI/chunk outputs. After review, explicitly replace the snapshot with:

```bash
make corpus-register CORPUS=fcv-literature SOURCE_DIR=/absolute/path/to/pdfs REPLACE=1
```

`REPLACE=1` clears the old XML/chunk/chunk-set/review/catalog derivatives because they no longer correspond to the registered source snapshot. Raw and generated corpus bytes are gitignored by default; the source manifest remains reviewable but must not be committed when filenames or corpus membership are rights-sensitive.

Registration does not call GROBID or parse papers. Once a corpus is registered, the existing stages remain available independently, or the full standard sequence can be invoked with:

```bash
make corpus-build CORPUS=fcv-literature
```

`corpus-build` performs the existing doctor → GROBID → parse → strict validation → review/catalog projection sequence. It therefore requires the normal GROBID/runtime prerequisites and should only be run for an explicitly approved corpus.

## Operator Make targets

Run all commands from repo root.

```bash
make corpus-register-dry-run CORPUS=my-corpus SOURCE_DIR=/path/to/pdfs
make corpus-register CORPUS=my-corpus SOURCE_DIR=/path/to/pdfs
make corpus-build CORPUS=my-corpus

make corpus-doctor CORPUS=tesislcd
make corpus-grobid CORPUS=tesislcd MAX_FILES=2
make corpus-parse CORPUS=tesislcd
make corpus-validate CORPUS=tesislcd

make architecture-check
make read-model-identity
make contract-review-record
make contract-catalog-record

make export-review-records CORPUS=tesislcd
make export-catalog-records CORPUS=tesislcd
make api-corpus CORPUS=tesislcd PORT=9000
make kill-port PORT=9000
make frontend-dev PORT=9000
```

`architecture-check` enforces the highest-value modular-monorepo dependency boundaries without attempting to freeze every internal import. `read-model-identity` proves that canonical `paper_uid` survives `chunk_set -> storage/read model -> PaperMeta` while legacy `paper_id` remains available.

Paper KB now exposes two sibling producer-owned projections directly from governed `chunk_set` artifacts:

- `paper.review-record@1` for bounded paper-review consumers;
- `paper.catalog-record@1` for bibliography/catalog consumers that need author-aware navigation without making a consumer the paper-metadata authority.

Both projections preserve canonical `paper_uid`, fail closed on duplicate identity and do not depend on backend runtime state. Catalog projection copies only metadata already present in `paper_meta`; it does not infer missing authors, dates or venues.

Compatibility remains explicit rather than silent:

```bash
make export-review-csv CORPUS=tesislcd
make export-review CORPUS=tesislcd   # deprecated alias
```

The CSV field layout is supported for existing consumers but is not the authority for paper-review semantics.

Paper-specific summaries and LLM implementation remain an explicit derivation component inside this monorepo. No repository split is implied by adding another projection.

Canonical API port is `9000`. `api-corpus` preflights the port. Legacy placeholder targets remain explicit `legacy-*` failures rather than aliases to modern behavior.
