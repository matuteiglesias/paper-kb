# TSE Chronicle discovery

The Chronicle uses the Toulouse Capitole Publications TSE division archive as a bounded first-party listing surface, OpenAlex institution-filtered works as external discovery/enrichment, and Crossref DOI records only where an OpenAlex abstract is absent.

The verified OpenAlex institution is `https://openalex.org/I4210092408` with ROR `https://ror.org/00ff5f522`. An OpenAlex affiliation assertion is retained as `provider_affiliation_metadata`, never as first-party membership.

`paper.catalog-record@1` cannot be used for every Chronicle item. Paper KB catalog records require a governed `chunk_set` and canonical `paper_uid`; external metadata-only records therefore remain Chronicle records. Following Packet B, exact normalized DOI and title/year produce **non-authoritative bibliographic candidate associations** (possibly multiple), not verified source-manifestation links. The existing producer `paper_uid` remains untouched.

Metadata-only publications can become governed Paper KB papers only partially: they need a legitimate source-document promotion through the normal Paper KB intake first. They cannot be minted as governed papers from Crossref, OpenAlex, or the archive HTML alone.

Renderer: a small static Markdown + HTML renderer, because Abstract Scroller consumes governed review records and the Chronicle intentionally includes metadata-only records.

Minimal seam: one bounded TSE acquisition/reconciliation/rendering script under `scripts/tse_chronicle.py`. It does not alter canonical Paper KB identity, emits no external catalog/review records, and leaves any future full-text promotion to the normal Paper KB intake. See [`Packet B metadata provenance`](../architecture/packet-b-metadata-provenance.md) for current acquisition-completeness, non-authoritative crosswalk, and GROBID receipt semantics. Historic v1 acquisition manifests are not treated as proof of complete coverage for future builds.
