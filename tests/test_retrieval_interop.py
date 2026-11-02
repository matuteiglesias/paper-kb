"""Portable retrieval projection tests; no framework install or vector index."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from scripts.export_retrieval_documents import (
    RetrievalContractError, decode_scoped_id, make_haystack_documents,
    make_llamaindex_nodes, read_governed_chunks, scoped_id, write_export,
)


def chunkset(path, *, uid="producer-paper", chunk_id="c:1",
             text="alpha\nbeta", source_hash="source-pdf-sha",
             chunk_uid=None, paper_id="legacy-id", chunk_source_hash=None):
    payload = {
        "artifact_family": "chunk_bus", "artifact_kind": "chunk_set",
        "schema_version": 1, "producer": "paper-kb", "run_id": "r1",
        "entrypoint": "synthetic", "source_items": ["synthetic.pdf"],
        "paper_meta": {
            "paper_uid": uid, "paper_id": paper_id, "source_sha256": source_hash,
            "title": "Synthetic study",
        },
        "chunks": [{
            "chunk_id": chunk_id, "paper_id": paper_id, "text": text,
            "chunk_index": 0, "pages": [1, 2],
            "metadata": {"header_path": ["Introduction"]},
            **({"paper_uid": chunk_uid} if chunk_uid is not None else {}),
            **({"source_sha256": chunk_source_hash} if chunk_source_hash is not None else {}),
        }],
        "chunk_count": 1,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_identity_namespace_is_reversible_and_collision_safe():
    one = scoped_id("chunk", ("corpus:A", "paper/ä", "c:1"))
    two = scoped_id("chunk", ("corpus:B", "paper/ä", "c:1"))
    three = scoped_id("chunk", ("corpus:A", "paper/ä", "c:2"))
    assert len({one, two, three}) == 3
    assert decode_scoped_id(one) == ("chunk", ("corpus:A", "paper/ä", "c:1"))
    assert decode_scoped_id(scoped_id("source", ("c", "p"))) == (
        "source", ("c", "p")
    )
    with pytest.raises(RetrievalContractError):
        decode_scoped_id(one + "tampered")


def test_artifact_and_text_revisions_are_independent_from_stable_identity(tmp_path):
    path = chunkset(tmp_path / "r1.chunk_set.json")
    rows = read_governed_chunks([path], corpus="pilot")
    assert len(rows) == 1
    row = rows[0]
    assert row["text"] == "alpha\nbeta"
    assert row["metadata"]["paper_uid"] == "producer-paper"
    assert row["metadata"]["chunk_id"] == "c:1"
    assert row["metadata"]["paper_id"] == "legacy-id"
    assert row["metadata"]["pages"] == [1, 2]
    assert row["metadata"]["header_path"] == ["Introduction"]
    assert row["metadata"]["source_sha256"] == "source-pdf-sha"
    assert row["metadata"]["text_sha256"] == hashlib.sha256(
        b"alpha\nbeta"
    ).hexdigest()
    assert row["metadata"]["artifact_sha256"] == hashlib.sha256(
        path.read_bytes()
    ).hexdigest()
    before = row["id"]
    old_revision = row["metadata"]["artifact_sha256"]
    chunkset(path, text="changed extraction; same source and chunk ID")
    second = read_governed_chunks([path], corpus="pilot")[0]
    assert second["id"] == before
    assert second["source_id"] == row["source_id"]
    assert second["metadata"]["artifact_sha256"] != old_revision
    assert second["metadata"]["text_sha256"] != row["metadata"]["text_sha256"]


def test_two_corpora_same_producer_ids_do_not_collide(tmp_path):
    path1 = chunkset(tmp_path / "a.chunk_set.json")
    path2 = chunkset(tmp_path / "b.chunk_set.json")
    a = read_governed_chunks([path1], corpus="A")
    b = read_governed_chunks([path2], corpus="B")
    assert a[0]["id"] != b[0]["id"]
    assert a[0]["metadata"]["chunk_id"] == b[0]["metadata"]["chunk_id"]


def test_duplicate_canonical_chunk_in_selected_corpus_rejected(tmp_path):
    a = chunkset(tmp_path / "a.chunk_set.json")
    b = chunkset(tmp_path / "b.chunk_set.json")
    with pytest.raises(RetrievalContractError, match="Duplicate chunk identity"):
        read_governed_chunks([a, b], corpus="same")


@pytest.mark.parametrize("kwargs, error", [
    ({"chunk_uid": "a-wrong-source"}, "conflicting paper_uid"),
    ({"chunk_source_hash": "wrong-sha"}, "source hash conflicts"),
    ({"text": " "}, "Missing chunk text"),
])
def test_invalid_governed_input_rejected(tmp_path, kwargs, error):
    path = chunkset(tmp_path / "bad.chunk_set.json", **kwargs)
    with pytest.raises(RetrievalContractError, match=error):
        read_governed_chunks([path], corpus="pilot")


def test_bounded_export_refuses_silent_partial_results(tmp_path):
    path = chunkset(tmp_path / "p.chunk_set.json")
    with pytest.raises(RetrievalContractError, match="max_chunks"):
        read_governed_chunks([path], corpus="c", max_chunks=0)
    with pytest.raises(RetrievalContractError, match="No accepted"):
        read_governed_chunks([], corpus="c")


def test_jsonl_export_has_receipt_and_requires_explicit_overwrite(tmp_path):
    path = chunkset(tmp_path / "p.chunk_set.json")
    records = read_governed_chunks([path], corpus="pilot")
    out = tmp_path / "bridge.jsonl"
    receipt = write_export(records, out)
    assert receipt["records"] == 1
    row = json.loads(out.read_text().strip())
    assert row == records[0]
    assert receipt["output_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert receipt["input_artifact_sha256"] == [hashlib.sha256(path.read_bytes()).hexdigest()]
    assert json.loads((tmp_path / "bridge.jsonl.receipt.json").read_text()) == receipt
    with pytest.raises(FileExistsError):
        write_export(records, out)


def test_llamaindex_node_factory_uses_explicit_id_and_source_relationship(monkeypatch):
    fake = types.ModuleType("llama_index.core.schema")
    class Node:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    fake.TextNode = Node
    fake.NodeRelationship = types.SimpleNamespace(SOURCE="SOURCE")
    fake.RelatedNodeInfo = lambda **kwargs: kwargs
    modules = {
        "llama_index": types.ModuleType("llama_index"),
        "llama_index.core": types.ModuleType("llama_index.core"),
        "llama_index.core.schema": fake,
    }
    with monkeypatch.context() as m:
        for name, mod in modules.items():
            m.setitem(sys.modules, name, mod)
        source = scoped_id("source", ("c", "uid"))
        data = [{
            "id": scoped_id("chunk", ("c", "uid", "c1")),
            "source_id": source, "text": "Synthetic content",
            "metadata": {"paper_uid": "uid", "source_sha256": "known"},
        }]
        node = make_llamaindex_nodes(data)[0]
    assert node.id_ == data[0]["id"]
    assert node.text == "Synthetic content"
    assert node.metadata["paper_uid"] == "uid"
    assert node.relationships["SOURCE"] == {"node_id": source}


def test_haystack_factory_never_generates_its_own_ids(monkeypatch):
    fake = types.ModuleType("haystack")
    class Document:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
    fake.Document = Document
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "haystack", fake)
        record = {
            "id": "stable-key", "text": "synthetic",
            "metadata": {"chunk_id": "chunk", "paper_uid": "paper"},
        }
        document = make_haystack_documents([record])[0]
    assert document.id == "stable-key"
    assert document.content == "synthetic"
    assert document.meta == record["metadata"]


def test_cli_requires_explicit_text_export_approval(tmp_path):
    directory = tmp_path / "chunk_sets"
    directory.mkdir()
    chunkset(directory / "a.chunk_set.json")
    out = tmp_path / "external.jsonl"
    args = [
        sys.executable, "-m", "scripts.export_retrieval_documents",
        "--chunk-set-dir", str(directory), "--corpus-name", "pilot",
        "--out", str(out),
    ]
    refused = subprocess.run(args, capture_output=True, text=True)
    assert refused.returncode != 0
    assert not out.exists()
    allowed = subprocess.run(args + ["--acknowledge-text-export"],
                             capture_output=True, text=True)
    assert allowed.returncode == 0, allowed.stderr
    assert json.loads(out.read_text().strip())["metadata"]["paper_uid"] == "producer-paper"

def test_pdf_first_page_and_parser_witnesses_survive_adapter(tmp_path):
    path = chunkset(tmp_path / "pdf.chunk_set.json")
    artifact = json.loads(path.read_text())
    artifact["chunks"][0].pop("pages")
    artifact["chunks"][0]["metadata"] = {
        "source_sha256": "source-pdf-sha",
        "page_start": 4, "page_end": 4,
        "page_char_start": 0, "page_char_end": 10,
        "parser": "poppler-layout-page-bounded-lossless-normalized-v1",
    }
    path.write_text(json.dumps(artifact), encoding="utf-8")
    record = read_governed_chunks([path], corpus="paper-corpus")[0]
    meta = record["metadata"]
    assert meta["page_start"] == meta["page_end"] == 4
    assert meta["page_char_start"] == 0
    assert meta["page_char_end"] == 10
    assert meta["parser"] == "poppler-layout-page-bounded-lossless-normalized-v1"
    assert meta["source_sha256"] == "source-pdf-sha"


def test_nested_pdf_hash_conflict_is_rejected(tmp_path):
    path = chunkset(tmp_path / "bad.chunk_set.json")
    artifact = json.loads(path.read_text())
    artifact["chunks"][0]["metadata"]["source_sha256"] = "different-pdf-sha"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(RetrievalContractError, match="source hash conflicts"):
        read_governed_chunks([path], corpus="c")


def test_absent_paper_level_legacy_id_preserves_chunk_id(tmp_path):
    path = chunkset(tmp_path / "legacy.chunk_set.json")
    artifact = json.loads(path.read_text())
    del artifact["paper_meta"]["paper_id"]
    path.write_text(json.dumps(artifact), encoding="utf-8")
    rec = read_governed_chunks([path], corpus="c")[0]
    assert rec["metadata"]["paper_id"] == "legacy-id"


def test_invalid_scope_id_rejected():
    with pytest.raises(RetrievalContractError):
        decode_scoped_id("pkb1-chunk-malformed===")
