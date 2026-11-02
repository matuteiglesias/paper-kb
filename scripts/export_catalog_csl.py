#!/usr/bin/env python3
"""Read-only, provenance-preserving CSL JSON export of paper.catalog-record@1.

The canonical catalog is not a Zotero library. This is a one-way, explicitly
lossy interchange export; author strings remain literal, and unknown genres
are not inferred from venue/title/DOI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from pipeline.contracts.catalog_record import validate_catalog_record_dict
from pipeline.corpus import resolve_corpus_paths

CSL_TYPES = frozenset({
    "article", "article-journal", "article-magazine", "article-newspaper",
    "book", "chapter", "dataset", "document", "manuscript",
    "paper-conference", "post", "report", "thesis", "webpage",
})
_DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
_DOI_URL = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:)", re.I)


def _atomic_json(path: Path, value: Any, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"Refusing to replace existing export: {path} (use --force)")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _issued_date(record: dict[str, Any]) -> dict[str, Any] | None:
    given = record.get("date")
    year = record.get("year")
    if given:
        match = _DATE.fullmatch(given)
        if not match:
            raise ValueError(f"Unsupported publication date precision for {record['paper_uid']}")
        parts = [int(x) for x in match.groups() if x is not None]
        from datetime import date
        try:
            date(parts[0], parts[1] if len(parts) > 1 else 1,
                 parts[2] if len(parts) > 2 else 1)
        except ValueError as exc:
            raise ValueError(f"Invalid publication date for {record['paper_uid']}") from exc
        if year is not None and year != parts[0]:
            raise ValueError(f"Conflicting publication year/date for {record['paper_uid']}")
        return {"date-parts": [parts]}
    if year is not None:
        return {"date-parts": [[year]]}
    return None


def as_csl(record: dict[str, Any], *, item_type: str = "document",
           include_abstract: bool = False) -> dict[str, Any]:
    """Map only supported source fields; never synthesize names or genres."""
    validate_catalog_record_dict(record)
    if item_type not in CSL_TYPES:
        raise ValueError(f"Unsupported CSL type: {item_type}")
    csl: dict[str, Any] = {
        "id": record["paper_uid"],
        "type": item_type,
        "title": record["title"],
    }
    if record["authors"]:
        csl["author"] = [{"literal": author, "parse-names": False}
                         for author in record["authors"]]
    if date := _issued_date(record):
        csl["issued"] = date
    if record.get("venue"):
        csl["container-title"] = record["venue"]
    if doi := record.get("doi"):
        csl["DOI"] = _DOI_URL.sub("", doi.strip())
    if record.get("source_url"):
        csl["URL"] = record["source_url"]
    if include_abstract and record.get("abstract"):
        csl["abstract"] = record["abstract"]
    return csl


def read_catalog(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing approved catalog JSONL: {path}")
    records = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        validate_catalog_record_dict(row)
        if row["paper_uid"] in seen:
            raise ValueError(f"Duplicate paper_uid at catalog line {line_number}")
        seen.add(row["paper_uid"])
        records.append(row)
    return sorted(records, key=lambda x: x["paper_uid"])


def export_csl(catalog_path: Path, output: Path, *, type_overrides: dict[str, str] | None = None,
               include_abstract: bool = False, force: bool = False) -> dict[str, Any]:
    records = read_catalog(catalog_path)
    overrides = type_overrides or {}
    known_ids = {row["paper_uid"] for row in records}
    if unknown := set(overrides) - known_ids:
        raise ValueError(f"Type overrides refer to unknown paper_uid values: {len(unknown)}")
    result = [as_csl(row, item_type=overrides.get(row["paper_uid"], "document"),
                     include_abstract=include_abstract) for row in records]
    receipt_path = Path(str(output) + ".receipt.json")
    if not force and (output.exists() or receipt_path.exists()):
        raise FileExistsError("CSL export or receipt already exists; use --force")
    # Receipt holds only aggregate conversion information and file digests,
    # not paper text, full authors, or local source paths.
    receipt = {
        "schema_version": 1,
        "format": "CSL JSON bibliography array",
        "mode": "one-way-import-no-zotero-account",
        "catalog_sha256": hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        "records": len(result),
        "literal_authors": sum(len(row["authors"]) for row in records),
        "unknown_genres_exported_as_document": len(records) - len(overrides),
        "explicit_type_overrides": len(overrides),
        "abstracts_included": include_abstract,
        "fields_omitted": ["paper_id", "arxiv_id", "repec_id", "tags"],
        "warnings": [
            "Personal author display strings are exported literally; given/family not inferred.",
            "CSL item IDs are local producer paper_uid, not Zotero database keys.",
            "Unspecified item types are generic document, not inferred article/report types.",
        ],
    }
    _atomic_json(output, result, force=force)
    receipt["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    _atomic_json(receipt_path, receipt, force=force)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--corpus", help="Existing named Paper KB corpus")
    source.add_argument("--catalog-jsonl", type=Path, help="Existing producer catalog projection")
    parser.add_argument("--out", type=Path, required=True, help="New CSL .json output path")
    parser.add_argument("--type-overrides", type=Path, help="Reviewed JSON {paper_uid: CSL type}")
    parser.add_argument("--include-abstract", action="store_true",
                        help="Explicitly include potentially rights-sensitive abstracts")
    parser.add_argument("--force", action="store_true", help="Explicitly replace export/receipt")
    args = parser.parse_args()
    catalog = (resolve_corpus_paths(args.corpus).catalog / "paper.catalog-record.v1.jsonl"
               if args.corpus else args.catalog_jsonl)
    overrides = (json.loads(args.type_overrides.read_text(encoding="utf-8"))
                 if args.type_overrides else None)
    if overrides is not None and (not isinstance(overrides, dict)
                                 or any(not isinstance(k, str) or not isinstance(v, str)
                                        for k, v in overrides.items())):
        parser.error("--type-overrides must contain a JSON string-to-string object")
    result = export_csl(catalog, args.out, type_overrides=overrides,
                        include_abstract=args.include_abstract, force=args.force)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
