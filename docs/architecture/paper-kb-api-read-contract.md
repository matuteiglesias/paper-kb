# Paper KB HTTP read contract — Packet A

Status: implemented, 2026-11-02. Authority: `backend/app` read/query service.
Canonical producer contracts and existing corpus identity algorithms are unchanged.

## Identities

- `paper_uid`: producer-owned, canonical paper/source-manifestation identity when
  present in governed artifacts. It is **not** inferred from the route, title, DOI,
  or external bibliographic matches.
- `paper_id`: source/legacy identity and the existing routing handle.
- `chunk_id`: the producer-owned passage identifier, retained verbatim.

Both `GET /api/papers/{paper_id}/chunks` and
`GET /api/papers/{paper_id}/chunks/{chunk_id}` return `paper_id` and nullable
`paper_uid` per chunk. A missing chunk UID may be filled from the accepted
storage adapter's matching paper-level record; conflicting identities cause an
explicit error rather than relabeling the source.

`POST /api/search` returns `id`, `chunk_id`, `paper_id`, nullable
`paper_uid`, `text`, `score` and `meta` in every hit. Explicit adapter
identity wins over compatibility metadata only when they agree; conflicts are
rejected, not repaired through guessing. Search remains lexical (token overlap),
not vector-semantic retrieval.

## Paper listing

`GET /api/papers` supports the following optional parameters:

| Parameter | Semantics |
| --- | --- |
| `q` | case-insensitive substring of title, paper IDs, authors, abstract, venue, or DOI |
| `year` | exact publication year |
| `venue` | case-insensitive substring of venue |
| `tag` | case-insensitive exact tag membership |
| `offset` | 0-based nonnegative offset, default 0 |
| `limit` | 1..1000 page size, default 200 |
| `status` | unsupported: nonempty values return HTTP 422 |

Filters are AND-composed, followed by deterministic ordering by casefolded
`title` and `paper_id`, then pagination. The response retains `papers` and
adds `total` (count **after filtering, before pagination**).

An unpopulated metadata field never generates an inferred value or a
synthetic filter match. This endpoint is a read of the adapter's current
accepted view; it does not promise an atomic snapshot across multiple HTTP
requests. A consumer needing a full list should detect unexpected changes
while paginating and retry rather than silently accepting partial results.

## Failure semantics

- Valid query with zero matches: HTTP 200 with an empty result.
- Invalid query or unsupported filter: HTTP 4xx.
- Search backend unavailable: HTTP 503, not an empty successful search.
- Malformed/conflicting identities in returned read records: HTTP 500.
- Legacy paper IDs and existing JSON properties remain supported.

The service-level global paper cache has been removed. Read caching belongs to
the selected adapter; switching the adapter must never reuse another corpus's
papers.

These changes do **not** authorize exposing the local API externally. The
`/_dev/seed` and summary-generation routes still require independent
deployment/access-control review before any untrusted access.

## Verification

Synthetic-only regression suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider \
  tests/test_api_truthfulness.py tests/test_api_v1_contract.py \
  tests/test_read_model_identity.py tests/test_chunk_set_storage_adapter.py \
  tests/test_component_boundaries.py
```

The frontend uses a small additive type overlay in `frontend/src/api/types.ts`
because its old `generated.ts` predates the current OpenAPI document. When
frontend dependencies are available, regenerate that file from the live
`/openapi.json` schema using the existing generation toolchain and remove the
temporary overlays after comparing the generated shapes.
