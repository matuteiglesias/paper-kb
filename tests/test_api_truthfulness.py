"""Consumer-facing contract proofs for Paper KB Packet A.

All fixtures are synthetic and isolated; no corpus directories or external services.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import services
from backend.app.main import app, get_storage
from backend.app.storage_adapter import ChunkSetStorageAdapter


@pytest.fixture
def storage(tmp_path: Path):
    root = tmp_path / "chunk_sets"
    root.mkdir()
    fixtures = [
        ("legacy-a", "uid-a", "Growth and Trade", 2024, "Journal of Economics",
         ["macro", "theory"], "economic growth and trade"),
        ("legacy-b", "uid-b", "Institutions and Welfare", 2025, "Policy Review",
         ["institutions"], "institutional welfare research"),
    ]
    for i, (pid, uid, title, year, venue, tags, passage) in enumerate(fixtures):
        payload = {
            "artifact_family": "chunk_bus", "artifact_kind": "chunk_set",
            "schema_version": 1, "run_id": f"synthetic-{i}",
            "producer": "paper-kb", "entrypoint": "contract-fixture",
            "source_items": [f"synthetic-{i}.xml"], "chunk_count": 1,
            "paper_meta": {
                "paper_id": pid, "paper_uid": uid, "title": title,
                "authors": ["Ada Example" if i == 0 else "Bob Example"],
                "year": year, "venue": venue, "tags": tags,
            },
            "chunks": [{
                "chunk_id": f"chunk-{i}", "paper_id": pid, "paper_uid": uid,
                "text": passage, "chunk_index": 0, "char_len": len(passage),
                "metadata": {}, "pages": [i + 1, i + 1],
            }],
        }
        (root / f"synthetic-{i}.chunk_set.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
    result = ChunkSetStorageAdapter(str(root))
    result.load_caches()
    return result


@pytest.fixture
def client(storage):
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_storage, None)


def test_search_returns_actual_paper_and_canonical_uid(client):
    response = client.post("/api/search", json={"q": "growth", "k": 5})
    assert response.status_code == 200
    assert response.json()["capability"] == "lexical"
    hit = response.json()["hits"][0]
    assert hit["id"] == hit["chunk_id"] == "chunk-0"
    assert hit["paper_id"] == "legacy-a"
    assert hit["paper_uid"] == "uid-a"
    assert hit["text"] == "economic growth and trade"


def test_search_scoping_preserves_source_identity(client):
    response = client.post(
        "/api/search", json={"q": "welfare", "paper_id": "legacy-b"}
    )
    assert response.status_code == 200
    hits = response.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["paper_id"] == "legacy-b"
    assert hits[0]["paper_uid"] == "uid-b"


def test_search_empty_is_success(client):
    response = client.post("/api/search", json={"q": "nothing_matches"})
    assert response.status_code == 200
    assert response.json()["hits"] == []


def test_chunk_list_and_detail_preserve_source_identity(client):
    listing = client.get("/api/papers/legacy-a/chunks")
    assert listing.status_code == 200
    assert listing.json()["paper_id"] == "legacy-a"
    chunk = listing.json()["chunks"][0]
    assert chunk["chunk_id"] == "chunk-0"
    assert chunk["paper_id"] == "legacy-a"
    assert chunk["paper_uid"] == "uid-a"
    detail = client.get("/api/papers/legacy-a/chunks/chunk-0")
    assert detail.status_code == 200
    assert detail.json()["paper_id"] == chunk["paper_id"]
    assert detail.json()["paper_uid"] == chunk["paper_uid"]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"year": 2025}, ["legacy-b"]),
        ({"venue": "journal"}, ["legacy-a"]),
        ({"tag": "MACRO"}, ["legacy-a"]),
        ({"q": "welfare"}, ["legacy-b"]),
        ({"q": "ada"}, ["legacy-a"]),
        ({"q": "uid-b"}, ["legacy-b"]),
        ({"year": 2024, "tag": "theory"}, ["legacy-a"]),
        ({"year": 2025, "tag": "macro"}, []),
    ],
)
def test_paper_filters_are_applied_before_pagination(client, params, expected):
    response = client.get("/api/papers", params=params)
    assert response.status_code == 200
    assert [p["paper_id"] for p in response.json()["papers"]] == expected
    assert response.json()["total"] == len(expected)


def test_stable_order_and_total(client):
    first = client.get("/api/papers", params={"offset": 0, "limit": 1})
    second = client.get("/api/papers", params={"offset": 1, "limit": 1})
    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == second.json()["total"] == 2
    assert [p["paper_id"] for p in first.json()["papers"]] == ["legacy-a"]
    assert [p["paper_id"] for p in second.json()["papers"]] == ["legacy-b"]


@pytest.mark.parametrize("params", [
    {"status": "draft"}, {"offset": -1}, {"limit": 0}, {"limit": -1},
    {"limit": 10001},
])
def test_unsupported_or_invalid_filters_are_explicit(client, params):
    assert client.get("/api/papers", params=params).status_code == 422


def test_distinct_storage_instances_do_not_share_papers(storage, tmp_path):
    one = services.list_papers(storage)
    assert {p.paper_id for p in one} == {"legacy-a", "legacy-b"}

    from backend.app.storage_adapter import ChunkSetStorageAdapter

    other = ChunkSetStorageAdapter(str(tmp_path / "empty"))
    assert services.list_papers(other) == []
    assert {p.paper_id for p in services.list_papers(storage)} == {
        "legacy-a", "legacy-b"
    }


def test_search_backend_failure_is_not_empty_success():
    class BrokenStorage:
        def semantic_search(self, **kwargs):
            raise OSError("synthetic unavailable")

    app.dependency_overrides[get_storage] = lambda: BrokenStorage()
    try:
        with TestClient(app) as c:
            response = c.post("/api/search", json={"q": "growth"})
        assert response.status_code == 503
        assert response.json()["detail"] == "search storage unavailable"
    finally:
        app.dependency_overrides.pop(get_storage, None)


def test_search_hit_with_conflicting_source_identity_is_not_silently_misattributed():
    class ConflictedStorage:
        def semantic_search(self, **kwargs):
            return [{
                "id": "c1", "text": "synthetic", "score": 0.5,
                "paper_id": "correct-paper", "paper_uid": "uid-a",
                "meta": {"paper_id": "different-paper"},
            }]

    app.dependency_overrides[get_storage] = lambda: ConflictedStorage()
    try:
        with TestClient(app) as c:
            response = c.post("/api/search", json={"q": "synthetic"})
        assert response.status_code >= 400
    finally:
        app.dependency_overrides.pop(get_storage, None)


def test_optional_canonical_uid_is_not_invented():
    class LegacyStorage:
        def semantic_search(self, **kwargs):
            return [{
                "id": "c", "text": "synthetic", "paper_id": "legacy",
                "score": 0.3, "meta": {},
            }]

    app.dependency_overrides[get_storage] = lambda: LegacyStorage()
    try:
        with TestClient(app) as c:
            response = c.post("/api/search", json={"q": "synthetic"})
        assert response.status_code == 200
        hit = response.json()["hits"][0]
        assert hit["paper_id"] == "legacy"
        assert hit["paper_uid"] is None
    finally:
        app.dependency_overrides.pop(get_storage, None)


def test_missing_chunk_uid_is_recovered_only_from_verified_parent(client, storage):
    # The governed paper record supplies identity even when a legacy chunk omits it.
    original = storage._chunk_index[("legacy-a", "chunk-0")]
    original_uid = original.pop("paper_uid")
    try:
        detail = client.get("/api/papers/legacy-a/chunks/chunk-0")
        assert detail.status_code == 200
        assert detail.json()["paper_uid"] == "uid-a"
    finally:
        original["paper_uid"] = original_uid


def test_search_recovers_parent_uid_from_confirmed_paper():
    class LegacyHitWithParent:
        def semantic_search(self, **kwargs):
            return [{"id": "c", "paper_id": "legacy", "text": "synthetic",
                     "meta": {}, "score": 0.5}]
        def get_paper(self, paper_id):
            return {"paper_id": paper_id, "paper_uid": "canonical"}

    app.dependency_overrides[get_storage] = lambda: LegacyHitWithParent()
    try:
        with TestClient(app) as c:
            response = c.post("/api/search", json={"q": "synthetic"})
        assert response.status_code == 200
        assert response.json()["hits"][0]["paper_uid"] == "canonical"
    finally:
        app.dependency_overrides.pop(get_storage, None)


def test_chunk_conflicting_canonical_uid_is_rejected(client, storage):
    raw = storage._chunk_index[("legacy-a", "chunk-0")]
    old = raw["paper_uid"]
    raw["paper_uid"] = "wrong-uid"
    try:
        response = client.get("/api/papers/legacy-a/chunks/chunk-0")
        assert response.status_code == 500
        assert response.json()["detail"] == "invalid chunk read contract"
    finally:
        raw["paper_uid"] = old


@pytest.mark.parametrize("k", [0, -1, 201])
def test_search_invalid_k_is_validation_error(client, k):
    assert client.post("/api/search", json={"q": "growth", "k": k}).status_code == 422


def test_search_scope_rejects_wrong_origin():
    class WrongScope:
        def semantic_search(self, **kwargs):
            return [{"id": "c", "paper_id": "another-paper", "text": "synthetic"}]

    app.dependency_overrides[get_storage] = lambda: WrongScope()
    try:
        with TestClient(app) as c:
            response = c.post("/api/search", json={
                "q": "synthetic", "paper_id": "requested-paper"
            })
        assert response.status_code == 500
    finally:
        app.dependency_overrides.pop(get_storage, None)


def test_scoped_hit_without_source_identity_is_rejected():
    class UnattributedStorage:
        def semantic_search(self, **kwargs):
            return [{"id": "c", "text": "synthetic"}]

    app.dependency_overrides[get_storage] = lambda: UnattributedStorage()
    try:
        with TestClient(app) as c:
            response = c.post("/api/search", json={
                "q": "synthetic", "paper_id": "requested-paper"
            })
        assert response.status_code == 500
    finally:
        app.dependency_overrides.pop(get_storage, None)


def test_openapi_advertises_additive_read_contract():
    spec = app.openapi()
    schemas = spec["components"]["schemas"]
    assert {"paper_id", "paper_uid"} <= set(schemas["ChunkResponse"]["properties"])
    assert {"paper_id", "paper_uid", "chunk_id"} <= set(
        schemas["SearchHit"]["properties"]
    )
    assert "total" in schemas["PapersList"]["properties"]
    parameters = {
        p["name"]: p
        for p in spec["paths"]["/api/papers"]["get"]["parameters"]
    }
    assert {"q", "year", "venue", "tag", "status", "offset", "limit"} <= set(parameters)
    assert parameters["offset"]["schema"]["minimum"] == 0
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["maximum"] == 1000
