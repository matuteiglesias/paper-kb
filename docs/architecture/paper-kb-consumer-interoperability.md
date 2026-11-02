# Paper KB consumer interoperability — Zotero/CSL and retrieval frameworks

Status: implemented locally, 2026-11-02. These are **optional, read-only
consumer adapters**, not additional producer contracts or source authorities.

## Architecture

Source authority remains `chunk_set@1` and producer-owned
`paper.catalog-record@1`. No source PDF, paper_uid, chunk_id, catalog record,
corpus membership, or review-record contract is changed by these adapters.

```text
accepted chunk_set@1 -> paper.catalog-record@1 -> CSL JSON -> Zotero File > Import
                 |
                 +-> retrieval bridge JSONL -> LlamaIndex or Haystack (opt-in)
```

A CSL import is **not** a two-way sync: Zotero may assign internal database
item keys and interpret some CSL creator fields differently. No Zotero API key,
account, server, or attachment is needed to generate the import file.

## 1. CSL JSON from an approved catalog

From repository root, with an existing accepted catalog:

```bash
make export-csl CORPUS=my-corpus OUT=/tmp/my-papers.csl.json
# Equivalent:
python3 -m scripts.export_catalog_csl --corpus my-corpus --out /tmp/my-papers.csl.json
```

Then use Zotero's **File > Import** and select the generated CSL JSON.
The exporter does **not** run Zotero or alter any existing Zotero library.

The format is a standard CSL JSON array. Each item uses the canonical
`paper_uid` as its CSL interchange `id`, not the Zotero database item key.
Existing author display strings are preserved as CSL
`{"literal": "...", "parse-names": false}`, never arbitrarily split into
given/family components. DOI URL prefixes are stripped only for CSL formatting.
Supported known dates are serialized at their actual precision
(year / year-month / year-month-day). Missing dates remain absent.

The producer catalog currently has **no authoritative publication type**.
Unspecified items are therefore exported as CSL `document`, not guessed
`article-journal` or `report`. To classify reviewed records explicitly,
supply a local JSON file mapping `paper_uid` to a CSL type:

```json
{
  "paper_uid_for_journal_article": "article-journal",
  "paper_uid_for_working_paper": "report"
}
```

```bash
make export-csl CORPUS=my-corpus OUT=/tmp/papers.csl.json TYPE_OVERRIDES=/tmp/reviewed-types.json
```

For privacy and rights hygiene, abstracts are omitted unless the caller adds
`INCLUDE_ABSTRACT=1` (or CLI `--include-abstract`). Original catalog records
also contain tags and external IDs without CSL equivalents; those are not
invented as citation fields. A separate `<out>.receipt.json` records input and
output SHA-256s, author literal counts, type overrides and known losses.
Exports refuse overwrite unless `EXPORT_FORCE=1` / `--force` is explicit.

The export is deterministic with stable record order and no network access.
Synthetic outputs were validated once against the Citation Style Language
project's published `csl-data.json` JSON Schema (schema URL below).
A live Zotero GUI round-trip was **not** performed; real import and creator
display remain a separate optional acceptance test.

## 2. Retrieval bridge for LlamaIndex and Haystack

```bash
make export-retrieval CORPUS=my-corpus OUT=/tmp/my-chunks.jsonl ACK_TEXT_EXPORT=1
# Equivalent:
python3 -m scripts.export_retrieval_documents \
  --corpus my-corpus --out /tmp/my-chunks.jsonl --acknowledge-text-export
```

This operation explicitly **copies extracted full text** and therefore
requires an acknowledgment. Do not commit the resulting export to Git or
publish it without a separate content-rights/privacy decision. It writes
JSONL plus a `<out>.receipt.json` with digest and source-artifact revisions;
it never creates an index, vector collection, embeddings, or external writes.
A default max of 10,000 chunks applies. The entire requested selection is
exported or the operation fails; it never silently emits the first N records.

Each JSONL record has:

```json
{
  "id": "pkb1-chunk-<reversible encoding>",
  "source_id": "pkb1-source-<reversible encoding>",
  "text": "exact producer chunk text",
  "metadata": {
    "corpus": "my-corpus",
    "paper_uid": "opaque producer paper ID",
    "chunk_id": "opaque producer chunk ID",
    "paper_id": "optional legacy identifier",
    "source_sha256": "optional registered PDF SHA-256",
    "artifact_sha256": "SHA-256 of exact accepted chunk_set artifact",
    "text_sha256": "SHA-256 of exact exported chunk text",
    "chunk_index": 0
  }
}
```

The `pkb1-*` IDs encode either `[corpus, paper_uid]` or
`[corpus, paper_uid, chunk_id]` using canonical JSON + URL-safe Base64.
`scoped_id()` and `decode_scoped_id()` form a reversible pair.
This namespace prevents collisions across corpora, and **does not replace**
the original producer IDs. Revision hashes are independent from stable IDs,
allowing downstream consumers to recognize stale indexed content. Changes
to a producer chunk ID are treated as identity changes and require a
consumer-side reindex/delete decision.

The exporter also preserves available physical page spans, page character
offsets, section/header paths and parser name. A conflicting chunk, parent,
or source PDF identity fails closed. It does not invent missing PDF lineage or
claim that a bibliographic work ID proves source-manifestation equality.

## 3. Native Python objects, only when installed

For consumers that use the corresponding framework:

```python
import json
from pathlib import Path
from scripts.export_retrieval_documents import (
    make_llamaindex_nodes,
    make_haystack_documents,
)

records = [
    json.loads(line)
    for line in Path("/tmp/my-chunks.jsonl").read_text().splitlines()
    if line.strip()
]

nodes = make_llamaindex_nodes(records)            # needs llama-index-core
documents = make_haystack_documents(records)      # needs haystack-ai
# Pass chosen nodes/documents to a separately authorized consumer index.
```

The LlamaIndex adapter supplies an explicit `TextNode.id_` and
`NodeRelationship.SOURCE` reference to the scoped paper source ID.
This is **a reference**, not a claim that a complete source Document object
has been created. The Haystack adapter supplies explicit `Document.id`,
`Document.content`, and `Document.meta`. Neither relies on autogenerated
framework IDs. Both imports are lazy; Paper KB does not add dependencies.

In the current workstation, Haystack is not installed and a direct import of
LlamaIndex raises `EOFError`. Factory behavior is tested with synthetic
stand-in classes; **actual installed-framework execution is not yet verified**.
The portable JSONL output does not depend on those environments.

## Validation

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_catalog_csl_interop.py tests/test_retrieval_interop.py
```

Synthetic tests include: DOI and date conversion, literal author names, empty
working-paper metadata, explicit CSL types, duplicate IDs, multi-corpus
collisions, reversible scoped IDs, artifact/text revisions, page/PDF witness
retention, conflicting provenance, overwrite protection, and no-network CLI
export. External import into Zotero or real framework indexing is deliberately
outside these tests.

References:
- Zotero formats: https://www.zotero.org/support/dev/data_formats
- Official CSL data schema: https://resource.citationstyles.org/schema/v1.0/input/json/csl-data.json
- LlamaIndex nodes: https://developers.llamaindex.ai/python/framework/module_guides/loading/documents_and_nodes/usage_nodes/
- Haystack Document Store: https://docs.haystack.deepset.ai/docs/document-store
