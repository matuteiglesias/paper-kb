#!/usr/bin/env python3
"""Read-only governed chunk_set -> retrieval JSONL / optional framework objects.

No embeddings, indexes, vector-store writes or corpus mutation. Consumers own
their indexes; Paper KB owns the opaque paper_uid/chunk_id and exact source text.
"""
from __future__ import annotations

import argparse
import binascii
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from pipeline.corpus import resolve_corpus_paths


class RetrievalContractError(ValueError):
    pass


def scoped_id(kind: str, parts: tuple[str, ...]) -> str:
    """Versioned, reversible global ID, not a replacement producer identifier."""
    if kind not in ("source", "chunk") or len(parts) != (2 if kind == "source" else 3):
        raise RetrievalContractError("Invalid scoped identifier arguments")
    if any(not isinstance(part, str) or not part for part in parts):
        raise RetrievalContractError("Scoped identifier components must be nonempty")
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"pkb1-{kind}-" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_scoped_id(value: str) -> tuple[str, tuple[str, ...]]:
    try:
        header, kind, encoded = value.split("-", 2)
        if header != "pkb1" or kind not in ("source", "chunk"):
            raise ValueError("invalid namespace")
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        parts = json.loads(payload)
        if not isinstance(parts, list):
            raise ValueError("invalid components")
        parts = tuple(parts)
        if scoped_id(kind, parts) != value:
            raise ValueError("non-canonical ID")
        return kind, parts
    except (ValueError, TypeError, UnicodeError, KeyError, binascii.Error) as exc:
        raise RetrievalContractError("Invalid retrieval identifier") from exc


def read_governed_chunks(paths: Iterable[Path], *, corpus: str,
                         max_chunks: int = 10000) -> list[dict[str, Any]]:
    """Fail closed on identity collisions or inconsistent source evidence."""
    files = sorted((Path(path) for path in paths), key=str)
    if not files:
        raise RetrievalContractError("No accepted chunk_set artifacts supplied")
    if not corpus or not corpus.strip() or max_chunks < 1:
        raise RetrievalContractError("Corpus namespace and positive max_chunks required")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in files:
        content = path.read_bytes()
        artifact_sha = hashlib.sha256(content).hexdigest()
        payload = json.loads(content)
        if (not isinstance(payload, dict)
                or payload.get("artifact_family") != "chunk_bus"
                or payload.get("artifact_kind") != "chunk_set"
                or payload.get("schema_version") != 1):
            raise RetrievalContractError("Unsupported chunk_set artifact")
        paper = payload.get("paper_meta")
        if not isinstance(paper, dict) or not isinstance(paper.get("paper_uid"), str) or not paper["paper_uid"]:
            raise RetrievalContractError("Canonical paper_uid missing in source artifact")
        uid = paper["paper_uid"]
        pid = paper.get("paper_id")
        source_sha = paper.get("source_sha256")
        chunks = payload.get("chunks")
        if not isinstance(chunks, list) or payload.get("chunk_count") != len(chunks):
            raise RetrievalContractError("Chunk set count mismatch or invalid chunks")
        source_id = scoped_id("source", (corpus, uid))
        local_legacy_id = pid
        for row in chunks:
            if not isinstance(row, dict):
                raise RetrievalContractError("Chunk is not an object")
            row_meta = row.get("metadata") or row.get("meta") or {}
            if not isinstance(row_meta, dict):
                raise RetrievalContractError("Invalid chunk metadata")
            legacy_id = row.get("paper_id") or pid
            if local_legacy_id is not None and legacy_id != local_legacy_id:
                raise RetrievalContractError("Inconsistent paper_id in source artifact")
            local_legacy_id = legacy_id
            chunk_id = row.get("chunk_id")
            if not isinstance(chunk_id, str) or not chunk_id:
                raise RetrievalContractError("Chunk is missing its producer ID")
            if row.get("paper_uid") not in (None, uid):
                raise RetrievalContractError("Chunk has conflicting paper_uid")
            if pid and row.get("paper_id") not in (None, pid):
                raise RetrievalContractError("Chunk has conflicting paper_id")
            candidate_source_hashes = {
                digest for digest in (source_sha, row.get("source_sha256"),
                                      row_meta.get("source_sha256")) if digest
            }
            if len(candidate_source_hashes) > 1:
                raise RetrievalContractError("Chunk source hash conflicts with paper")
            accepted_source_sha = next(iter(candidate_source_hashes), None)
            source_ref = row.get("source_ref")
            if (accepted_source_sha and isinstance(source_ref, str)
                    and source_ref.startswith("sha256:")
                    and source_ref.removeprefix("sha256:") != accepted_source_sha):
                raise RetrievalContractError("Source reference conflicts with registered PDF hash")
            text = row.get("text")
            if not isinstance(text, str) or not text.strip():
                raise RetrievalContractError("Missing chunk text")
            index = row.get("chunk_index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise RetrievalContractError("Invalid chunk index")
            record_id = scoped_id("chunk", (corpus, uid, chunk_id))
            if record_id in seen:
                raise RetrievalContractError("Duplicate chunk identity in export selection")
            seen.add(record_id)
            pages = row.get("pages", row_meta.get("pages"))
            header_path = row.get("header_path", row_meta.get("header_path"))
            meta = {
                "corpus": corpus, "paper_uid": uid, "chunk_id": chunk_id,
                "source_id": source_id,
                "artifact_sha256": artifact_sha,
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "chunk_index": index,
            }
            # Producer IDs and revision hashes are carried independently.
            if legacy_id is not None:
                meta["paper_id"] = legacy_id
            if accepted_source_sha is not None:
                meta["source_sha256"] = accepted_source_sha
            if payload.get("run_id") is not None:
                meta["run_id"] = payload["run_id"]
            if pages is not None:
                meta["pages"] = pages
            if header_path is not None:
                meta["header_path"] = header_path
            # PDF-first sources preserve physical pages and normalized source spans
            # inside chunk metadata, not necessarily in the generic pages field.
            for witness in ("page_start", "page_end", "page_char_start",
                            "page_char_end", "parser"):
                if row_meta.get(witness) is not None:
                    meta[witness] = row_meta[witness]
            records.append({
                "id": record_id, "source_id": source_id,
                "text": text, "metadata": meta,
            })
            if len(records) > max_chunks:
                raise RetrievalContractError(
                    f"Export exceeds max_chunks={max_chunks}; explicit larger budget required"
                )
    return sorted(records, key=lambda row: (
        row["metadata"]["paper_uid"], row["metadata"]["chunk_index"],
        row["metadata"]["chunk_id"],
    ))


def make_llamaindex_nodes(records: Iterable[dict[str, Any]]) -> list[Any]:
    """Optional native objects. Never instantiate a new indexing pipeline."""
    try:
        from llama_index.core.schema import NodeRelationship, RelatedNodeInfo, TextNode
    except Exception as exc:
        raise RuntimeError("LlamaIndex runtime unavailable; use portable JSONL export") from exc
    return [
        TextNode(
            id_=row["id"], text=row["text"], metadata=dict(row["metadata"]),
            relationships={
                NodeRelationship.SOURCE: RelatedNodeInfo(node_id=row["source_id"])
            },
        )
        for row in records
    ]


def make_haystack_documents(records: Iterable[dict[str, Any]]) -> list[Any]:
    """Optional native Haystack objects; caller controls indexing/upsert."""
    try:
        from haystack import Document
    except Exception as exc:
        raise RuntimeError("Haystack runtime unavailable; use portable JSONL export") from exc
    return [
        Document(id=row["id"], content=row["text"], meta=dict(row["metadata"]))
        for row in records
    ]


def write_export(records: list[dict[str, Any]], out: Path, *,
                 force: bool = False) -> dict[str, Any]:
    receipt_path = Path(str(out) + ".receipt.json")
    if not force and (out.exists() or receipt_path.exists()):
        raise FileExistsError("Retrieval export or receipt already exists; use --force")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        os.replace(temp, out)
    finally:
        Path(temp).unlink(missing_ok=True)
    receipt = {
        "schema_version": 1,
        "format": "Paper KB retrieval bridge JSONL",
        "records": len(records),
        "output_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "corpora": sorted({row["metadata"]["corpus"] for row in records}),
        "unique_papers": len({row["source_id"] for row in records}),
        "input_artifact_sha256": sorted({row["metadata"]["artifact_sha256"] for row in records}),
        "note": "Derived consumer representation; not a canonical source or automatic index.",
    }
    from scripts.export_catalog_csl import _atomic_json
    _atomic_json(receipt_path, receipt, force=force)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--corpus", help="Name of accepted Paper KB corpus")
    source.add_argument("--chunk-set-dir", type=Path, help="Explicit accepted chunk_set directory")
    parser.add_argument("--corpus-name", help="Namespace with --chunk-set-dir")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-chunks", type=int, default=10000)
    parser.add_argument("--acknowledge-text-export", action="store_true",
                        help="Explicitly permit copying chunk text outside governed corpus")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.acknowledge_text_export:
        parser.error("Explicit --acknowledge-text-export required for full-text output")
    if args.corpus:
        if args.corpus_name:
            parser.error("--corpus-name only applies with --chunk-set-dir")
        corpus = args.corpus
        directory = resolve_corpus_paths(corpus).chunk_sets
    else:
        if not args.corpus_name:
            parser.error("--corpus-name required with --chunk-set-dir")
        corpus, directory = args.corpus_name, args.chunk_set_dir
    if not directory.is_dir():
        raise FileNotFoundError(f"Chunk set directory does not exist: {directory}")
    records = read_governed_chunks(directory.glob("*.chunk_set.json"),
                                   corpus=corpus, max_chunks=args.max_chunks)
    print(json.dumps(write_export(records, args.out, force=args.force),
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
