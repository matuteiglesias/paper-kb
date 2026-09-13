# Second-institution readiness

The reusable seam is intentionally small:

1. an institution spec with a date window and identifiers;
2. a first-party source adapter that emits source claims;
3. bounded external enrichment snapshots;
4. DOI/explicit identifier reconciliation;
5. a Chronicle projection and static renderer.

Adding a second institution would still require a source-specific first-party adapter and a reviewed topic vocabulary. It would not require another repository, database, vector index, or Paper KB identity system. Paper KB promotion remains optional and follows the existing governed intake.

What remains institution-specific in this implementation is the Toulouse Capitole archive URL/parser and the TSE topic vocabulary. The reconciliation, provenance, author projection, QA, and renderer shape are now reusable conventions, but have not been extracted into a generic framework.
