#!/usr/bin/env python3
"""Bounded TSE Research Chronicle acquisition, reconciliation, and rendering.

This is intentionally a small institution-specific seam.  It does not emit
Paper KB catalog/review records from external metadata; it emits Chronicle
records and links to Paper KB only when an existing governed chunk-set match
can be proven.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "institution-chronicle" / "tse"
CONFIG = ROOT / "config" / "chronicle"
FIRST_PARTY_BASE = "https://publications.ut-capitole.fr/view/divisions/tse"
OPENALEX_INST = "https://openalex.org/I4210092408"
ROR = "https://ror.org/00ff5f522"
TODAY = "2026-09-13"
UA = "paper-kb-tse-chronicle/1.0 (local reproducible research artifact)"


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:90] or "untitled"


def sha(obj) -> str:
    b = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(b).hexdigest()


def get(url: str, cache: Path | None = None) -> bytes:
    if cache and cache.exists():
        return cache.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,text/html,application/xml"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    return data


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def parse_first_party_page(year: int, raw: bytes):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw, "html.parser")
    rows = []
    for p in soup.find_all("p"):
        link = p.find("a", href=re.compile(r"/id/eprint/"))
        if not link:
            continue
        title = " ".join(link.get_text(" ", strip=True).split())
        eprint = re.search(r"/id/eprint/(\d+)", link.get("href", ""))
        if not eprint or not title:
            continue
        authors = []
        for span in p.select("span.person_name"):
            name = " ".join(span.get_text(" ", strip=True).split())
            if name:
                ids = []
                nxt = span.find_next_sibling()
                # IDs are adjacent anchors/images in the same paragraph; only
                # attach the immediately visible identity links by name order.
                for a in p.find_all("a", href=True):
                    if "orcid.org" in a["href"] or "idref.fr" in a["href"]:
                        ids.append(a["href"])
                authors.append({"name": name})
        text = " ".join(p.get_text(" ", strip=True).split())
        kind = "working-paper" if "working paper" in norm(text) or "document de travail" in norm(text) else "published-or-other"
        wp = None
        m = re.search(r"(?:TSE\s+)?Working Paper[^0-9]*(\d{2}-\d{3,5})", text, re.I)
        if m:
            wp = m.group(1)
        rows.append({
            "provider": "tse_first_party",
            "provider_record_id": eprint.group(1),
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source_url": urllib.parse.urljoin("https://publications.ut-capitole.fr", link["href"]),
            "title": title,
            "authors": authors,
            "published_date": f"{year}-01-01",
            "year": year,
            "work_kind": kind,
            "working_paper_number": wp,
            "venue_or_display": text[text.find(")") + 1:].strip() if ")" in text else None,
            "institution_claim": {"institution_id": "tse", "claim_kind": "first_party_listing"},
            "raw_display": text,
        })
    return rows


def reconstruct_abstract(inv):
    if not isinstance(inv, dict):
        return None
    words = []
    for word, positions in inv.items():
        for pos in positions or []:
            words.append((pos, word))
    return " ".join(w for _, w in sorted(words)) or None


def acquire(args):
    out = OUT / "acquisition"
    cache = out / "cache"
    out.mkdir(parents=True, exist_ok=True)
    failures = []
    first = []
    for year in range(2010, 2027):
        url = f"{FIRST_PARTY_BASE}/{year}.html"
        try:
            raw = get(url, cache / f"tse-{year}.html")
            first.extend(parse_first_party_page(year, raw))
        except Exception as exc:
            failures.append({"provider": "tse_first_party", "url": url, "error": repr(exc)})
    write_jsonl(out / "tse_first_party.jsonl", first)

    openalex = []
    cursor = "*"
    while cursor:
        params = urllib.parse.urlencode({
            "filter": f"institutions.id:{OPENALEX_INST.split('/')[-1]},from_publication_date:2010-01-01,to_publication_date:{TODAY}",
            "per-page": 200,
            "cursor": cursor,
            "select": "id,doi,title,publication_date,publication_year,type,authorships,primary_location,locations,abstract_inverted_index,ids,open_access,concepts,keywords,related_works",
        })
        url = "https://api.openalex.org/works?" + params
        try:
            data = json.loads(get(url, cache / ("openalex-" + hashlib.sha1(url.encode()).hexdigest() + ".json")))
            for w in data.get("results", []):
                w["_chronicle_source"] = {"provider": "openalex", "institution_claim": {"institution_id": "tse", "claim_kind": "provider_affiliation_metadata"}}
                openalex.append(w)
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not data.get("results"):
                cursor = None
        except Exception as exc:
            failures.append({"provider": "openalex", "url": url, "error": repr(exc)})
            break
    write_jsonl(out / "openalex.jsonl", openalex)

    # Crossref is deliberately bounded to DOI-bearing OpenAlex works where no
    # abstract was available. It is enrichment, never membership evidence.
    cross = read_jsonl(out / "crossref.jsonl") if args.max_crossref == 0 else []
    seen_doi = set()
    for w in openalex:
        if args.max_crossref == 0:
            break
        if len(seen_doi) >= args.max_crossref:
            break
        doi = (w.get("doi") or "").lower().strip()
        if not doi or doi in seen_doi or reconstruct_abstract(w.get("abstract_inverted_index")):
            continue
        seen_doi.add(doi)
        doi_path = re.sub(r"^https?://doi\.org/", "", doi, flags=re.I).strip()
        url = "https://api.crossref.org/works/" + urllib.parse.quote(doi_path, safe="")
        try:
            cross.append(json.loads(get(url, cache / ("crossref-" + hashlib.sha1(doi.encode()).hexdigest() + ".json"))).get("message", {}))
        except Exception as exc:
            failures.append({"provider": "crossref", "url": url, "doi": doi, "error": repr(exc)})
        if args.sleep:
            time.sleep(args.sleep)
    write_jsonl(out / "crossref.jsonl", cross)
    write_jsonl(out / "failures.jsonl", failures)
    manifest = {
        "schema_version": 1,
        "institution": "tse",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "through_date": TODAY,
        "sources": {
            "tse_first_party": "https://publications.ut-capitole.fr/view/divisions/tse/<year>.html",
            "openalex": "https://api.openalex.org/works",
            "crossref": "https://api.crossref.org/works/<doi>",
        },
        "identifiers": {"openalex_institution": OPENALEX_INST, "ror": ROR},
        "counts": {"tse_first_party": len(first), "openalex": len(openalex), "crossref": len(cross), "failures": len(failures)},
        "files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in [out/"tse_first_party.jsonl", out/"openalex.jsonl", out/"crossref.jsonl", out/"failures.jsonl"]},
    }
    (out / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def crossref_abstract(row):
    a = row.get("abstract")
    if not a:
        return None
    a = re.sub(r"<[^>]+>", " ", a)
    return re.sub(r"\s+", " ", html.unescape(a)).strip() or None


def oa_record(w):
    authors = []
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        ids = {"openalex": au.get("id")}
        if au.get("orcid"):
            ids["orcid"] = au["orcid"]
        authors.append({"name": au.get("display_name"), "ids": {k: v for k, v in ids.items() if v}})
    loc = w.get("primary_location") or {}
    source = loc.get("source") or {}
    return {
        "provider": "openalex",
        "provider_record_id": w.get("id"),
        "source_url": loc.get("landing_page_url") or w.get("doi") or w.get("id"),
        "title": w.get("title"),
        "authors": authors,
        "published_date": w.get("publication_date"),
        "year": w.get("publication_year"),
        "work_kind": w.get("type"),
        "doi": w.get("doi"),
        "venue_or_display": source.get("display_name"),
        "abstract": reconstruct_abstract(w.get("abstract_inverted_index")),
        "abstract_source": "openalex" if w.get("abstract_inverted_index") else None,
        "institution_claim": {"institution_id": "tse", "claim_kind": "provider_affiliation_metadata"},
        "raw_provider_id": w.get("id"),
    }


def author_key(name):
    return norm(name)


def author_overlap_key(name):
    s = re.sub(r"[-‐‑‒–—]", " ", name or "")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().casefold()
    return tuple(sorted(re.findall(r"[a-z]+", s)))


def reconcile():
    acq = OUT / "acquisition"
    first = read_jsonl(acq / "tse_first_party.jsonl")
    oa_raw = read_jsonl(acq / "openalex.jsonl")
    cross = read_jsonl(acq / "crossref.jsonl")
    first_party_author_roster = {
        author_overlap_key(a.get("name"))
        for r in first
        for a in (r.get("authors") or [])
        if a.get("name")
    }
    oa = []
    for w in oa_raw:
        candidate = oa_record(w)
        names = [a.get("name") for a in candidate.get("authors") or []]
        if any(author_overlap_key(n) in first_party_author_roster for n in names if n):
            oa.append(candidate)
    cross_by_doi = {norm(r.get("DOI") or r.get("doi")): r for r in cross if r.get("DOI") or r.get("doi")}
    for r in oa:
        if not r.get("abstract"):
            cr = cross_by_doi.get(norm(r.get("doi")))
            if cr and crossref_abstract(cr):
                r["abstract"] = crossref_abstract(cr)
                r["abstract_source"] = "crossref"
                r["abstract_source_ref"] = cr.get("URL") or r.get("doi")

    buckets = {}
    aliases = defaultdict(list)
    def key_for(r, prefix):
        doi = norm(r.get("doi"))
        if doi:
            return "doi:" + doi
        wp = norm(r.get("working_paper_number"))
        if wp:
            return "wp:" + wp
        return prefix + ":" + str(r.get("provider_record_id"))
    for r in first:
        k = key_for(r, "tse")
        buckets.setdefault(k, {"claims": []})["claims"].append(r)
        aliases["title:" + norm(r.get("title")) + ":" + str(r.get("year"))].append(k)
    for r in oa:
        k = key_for(r, "oa")
        title_key = "title:" + norm(r.get("title")) + ":" + str(r.get("year"))
        candidates = aliases.get(title_key, [])
        if not norm(r.get("doi")) and len(candidates) == 1:
            # Exact title/year match is recorded as reconciliation evidence,
            # not fuzzy identity inference.
            k = candidates[0]
        buckets.setdefault(k, {"claims": []})["claims"].append(r)
    records = []
    for k in sorted(buckets):
        claims = buckets[k]["claims"]
        ordered = sorted(claims, key=lambda r: (0 if r.get("provider") == "tse_first_party" else 1, r.get("provider", "")))
        base = dict(ordered[0])
        for r in ordered[1:]:
            for field in ("doi", "abstract", "abstract_source", "venue_or_display", "source_url"):
                if not base.get(field) and r.get(field):
                    base[field] = r[field]
        title = base.get("title") or "Untitled work"
        cid = "chronicle:tse:" + hashlib.sha256(k.encode()).hexdigest()[:20]
        first_claim = next((r for r in claims if r.get("provider") == "tse_first_party"), None)
        provider_claims = [r for r in claims if r.get("provider") != "tse_first_party"]
        authors = base.get("authors") or []
        if authors and isinstance(authors[0], str):
            authors = [{"name": x} for x in authors]
        rec = {
            "chronicle_id": cid,
            "title": title,
            "authors": authors,
            "year": base.get("year"),
            "published_date": base.get("published_date"),
            "work_kind": "working-paper" if any("working paper" in norm(r.get("raw_display")) or "document de travail" in norm(r.get("raw_display")) for r in claims) else base.get("work_kind"),
            "doi": base.get("doi"),
            "working_paper_number": next((r.get("working_paper_number") for r in claims if r.get("working_paper_number")), None),
            "venue": base.get("venue_or_display"),
            "abstract": base.get("abstract"),
            "abstract_source": base.get("abstract_source") if base.get("abstract") else "unavailable",
            "sources": [{"provider": r.get("provider"), "provider_record_id": r.get("provider_record_id"), "source_url": r.get("source_url"), "claim_kind": (r.get("institution_claim") or {}).get("claim_kind")} for r in claims],
            "membership_evidence": "first_party_listing" if first_claim else "provider_affiliation_metadata",
            "first_party_url": first_claim.get("source_url") if first_claim else None,
            "paper_uid": None,
            "paper_kb_state": "CHRONICLE_METADATA_ONLY",
            "reconciliation_key": k,
            "reconciliation_method": "doi" if k.startswith("doi:") else ("tse_working_paper_number" if k.startswith("wp:") else "provider_identity"),
        }
        records.append(rec)
    records.sort(key=lambda r: (r.get("year") or 9999, norm(r["title"]), r["chronicle_id"]))
    return records


def link_paper_kb(records):
    by_doi, by_title = {}, defaultdict(list)
    for p in ROOT.glob("corpora/*/chunk_sets/*.json"):
        try:
            x = json.loads(p.read_text(encoding="utf-8")); m = x.get("paper_meta") or {}
        except Exception:
            continue
        uid = m.get("paper_uid") or m.get("paper_id")
        if not uid: continue
        if norm(m.get("doi")): by_doi[norm(m["doi"])] = (uid, str(p))
        by_title[norm(m.get("title"))].append((uid, str(p)))
    links = []
    for r in records:
        hit = by_doi.get(norm(r.get("doi"))) if r.get("doi") else None
        if not hit and len(by_title.get(norm(r.get("title")), [])) == 1:
            hit = by_title[norm(r.get("title"))][0]
        if hit:
            r["paper_uid"], r["paper_kb_source"] = hit
            r["paper_kb_state"] = "GOVERNED_FULLTEXT"
            links.append({"chronicle_id": r["chronicle_id"], "paper_uid": hit[0], "chunk_set": hit[1], "match": "doi" if r.get("doi") and norm(r.get("doi")) in by_doi else "exact_title"})
    return links


def topics(records):
    vocab = {
        "industrial-organization": ["competition", "market", "firm", "oligopoly", "platform", "merger", "pricing"],
        "environmental-economics": ["climate", "environment", "carbon", "energy", "pollution", "emission", "biodiversity"],
        "public-economics": ["tax", "fiscal", "public", "welfare", "social insurance", "health", "education"],
        "macro-finance": ["monetary", "inflation", "bank", "financial", "macro", "capital", "credit"],
        "labor-development": ["labor", "employment", "wage", "migration", "development", "poverty", "inequality"],
        "econometrics-methods": ["econometric", "statistical", "inference", "estimation", "algorithm", "data analysis"],
        "economic-history": ["history", "historical", "war", "century", "medieval"],
        "theory-mechanism-design": ["game", "mechanism", "equilibrium", "contract", "information", "theory"],
    }
    assigns = []
    for r in records:
        hay = norm((r.get("title") or "") + " " + (r.get("abstract") or ""))
        found = [t for t, terms in vocab.items() if any(norm(term) in hay for term in terms)]
        if not found: found = ["unclassified"]
        r["topics"] = found
        for t in found: assigns.append({"chronicle_id": r["chronicle_id"], "topic": t, "match_basis": "title_or_abstract_lexical"})
    return assigns


def render(records, assigns, links, manifest):
    OUT.mkdir(parents=True, exist_ok=True)
    previous = read_jsonl(OUT / "papers.jsonl")
    previous_by_id = {r.get("chronicle_id"): sha(r) for r in previous}
    current_by_id = {r.get("chronicle_id"): sha(r) for r in records}
    (OUT / "refresh.json").write_text(json.dumps({
        "refresh_timestamp": datetime.now(timezone.utc).isoformat(),
        "new": len(set(current_by_id) - set(previous_by_id)),
        "removed": len(set(previous_by_id) - set(current_by_id)),
        "changed": sum(current_by_id[k] != previous_by_id[k] for k in set(current_by_id) & set(previous_by_id)),
        "unchanged": sum(current_by_id[k] == previous_by_id[k] for k in set(current_by_id) & set(previous_by_id)),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_jsonl(OUT / "papers.jsonl", records)
    write_jsonl(OUT / "topic_assignments.jsonl", assigns)
    authors = defaultdict(lambda: {"display_name": "", "ids": {}, "works": [], "years": set()})
    for r in records:
        for a in r.get("authors") or []:
            name = a.get("name") if isinstance(a, dict) else str(a)
            k = author_key(name)
            authors[k]["display_name"] = authors[k]["display_name"] or name
            authors[k]["ids"].update(a.get("ids", {}) if isinstance(a, dict) else {})
            authors[k]["works"].append(r["chronicle_id"])
            if r.get("year"):
                authors[k]["years"].add(r["year"])
    author_rows = []
    for k, a in sorted(authors.items(), key=lambda kv: norm(kv[1]["display_name"])):
        years = sorted(a["years"])
        author_rows.append({"display_name": a["display_name"], "ids": a["ids"], "works": sorted(a["works"]), "first_year": min(years) if years else None, "latest_year": max(years) if years else None, "work_count": len(a["works"])})
    write_jsonl(OUT / "authors.jsonl", author_rows)
    abstract_count = sum(bool(r.get("abstract")) for r in records)
    kinds = Counter(r.get("work_kind") for r in records)
    (OUT / "index.md").write_text(
        "# Toulouse School of Economics — Research Chronicle 2010–2026\n\n"
        f"- Records observed: {len(records)}\n- With abstracts: {abstract_count}\n"
        f"- Working papers: {kinds.get('working-paper', 0)}\n- Authors: {len(author_rows)}\n"
        "- Coverage: 2010-01-01 through 2026-09-13\n\n"
        "## Browse\n\n- [Latest](LATEST.md)\n- [Topic chronology](TOPIC_CHRONOLOGY.md)\n- [Coverage and provenance](COVERAGE.md)\n- [Paper KB promotion](PAPER_KB_PROMOTION.md)\n- [Annual digests](years/2010.md) onward\n\n"
        "First-party TSE listings and external affiliation/enrichment are distinguished on every record.\n", encoding="utf-8")
    (OUT / "authors.md").write_text(
        "# TSE authors\n\n" + "\n".join(
            f"- **{a['display_name']}** — {a['work_count']} works ({a['first_year']}–{a['latest_year']})"
            for a in author_rows
        ) + "\n", encoding="utf-8")
    (OUT / "reconciliation.jsonl").write_text("".join(json.dumps({"chronicle_id": r["chronicle_id"], "key": r["reconciliation_key"], "method": r["reconciliation_method"], "sources": r["sources"]}, ensure_ascii=False, sort_keys=True)+"\n" for r in records), encoding="utf-8")
    write_jsonl(OUT / "paper_kb_links.jsonl", links)
    topics_count = Counter(t for a in assigns for t in [a["topic"]])
    years = Counter(r.get("year") for r in records if r.get("year"))
    counts = {"chronicle_records": len(records), "authors": len(author_rows), "abstracts": abstract_count, "years": dict(sorted((str(k), v) for k, v in years.items())), "work_kinds": dict(kinds), "topics": dict(topics_count), "paper_kb_links": len(links), "first_party_records": sum(r["membership_evidence"] == "first_party_listing" for r in records), "external_only_records": sum(r["membership_evidence"] != "first_party_listing" for r in records)}
    (OUT / "acquisition_counts.json").write_text(json.dumps(counts, ensure_ascii=False, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    report = ["# ACQUISITION_REPORT", "", f"- TSE first-party rows: {manifest['counts']['tse_first_party']}", f"- OpenAlex rows: {manifest['counts']['openalex']}", f"- Crossref enrichment rows: {manifest['counts']['crossref']}", f"- provider failures: {manifest['counts']['failures']}", f"- reconciled Chronicle records: {len(records)}", f"- first-party membership records: {counts['first_party_records']}", f"- external-affiliation-only records: {counts['external_only_records']}", f"- DOI-bearing records: {sum(bool(r.get('doi')) for r in records)}", f"- abstract-bearing records: {abstract_count}", "", "External provider affiliation is enrichment/discovery evidence, not first-party TSE membership. The first-party archive is the strongest membership claim in this run."]
    report.append(f"- OpenAlex candidates retained after first-party author-roster overlap: {sum(1 for r in records if any(s.get('provider') == 'openalex' for s in r.get('sources', [])))}")
    report.append("- OpenAlex rows without first-party author-roster overlap remain in the raw snapshot but are excluded from the Chronicle projection to reduce affiliation noise.")
    (OUT / "ACQUISITION_REPORT.md").write_text("\n".join(report)+"\n", encoding="utf-8")
    (OUT / "PAPER_KB_PROMOTION.md").write_text("# PAPER_KB_PROMOTION\n\n- Total Chronicle records: %d\n- Existing governed Paper KB matches: %d\n- Chronicle metadata-only records: %d\n\nExternal metadata was not emitted as `paper.catalog-record@1` or `paper.review-record@1`. Records are promoted only when an existing governed chunk-set match is proven.\n" % (len(records), len(links), len(records)-len(links)), encoding="utf-8")
    # Markdown products.
    def item_md(r):
        abstract = r.get("abstract") or "Abstract unavailable in the acquired providers."
        prov = ", ".join(sorted({s.get("provider") for s in r.get("sources", [])}))
        return f"## {r['title']}\n\n- Chronicle ID: `{r['chronicle_id']}`\n- Year: {r.get('year') or 'unknown'}\n- Authors: {', '.join(a.get('name','') for a in r.get('authors') or [])}\n- Type: {r.get('work_kind') or 'unknown'}\n- Topics: {', '.join(r.get('topics', []))}\n- Membership evidence: `{r.get('membership_evidence')}`\n- Sources: {prov}\n- DOI: {r.get('doi') or 'none'}\n- Paper KB state: `{r.get('paper_kb_state')}`\n\n**Abstract**\n\n{abstract}\n\n**Provenance**\n\n{'; '.join((s.get('source_url') or '') for s in r.get('sources', []))}\n"
    year_dir = OUT / "years"; year_dir.mkdir(exist_ok=True)
    for year in range(2010, 2027):
        rs = [r for r in records if r.get("year") == year]
        body = [f"# TSE publications — {year}", "", f"Records: {len(rs)}", f"Working papers: {sum(r.get('work_kind') == 'working-paper' for r in rs)}", f"With abstracts: {sum(bool(r.get('abstract')) for r in rs)}", ""]
        body += [item_md(r) for r in rs]
        (year_dir / f"{year}.md").write_text("\n".join(body)+"\n", encoding="utf-8")
    topic_rows = defaultdict(list)
    year_by_id = {r["chronicle_id"]: r.get("year") for r in records}
    for a in assigns:
        topic_rows[a["topic"]].append(a["chronicle_id"])
    topic_md = ["# TSE Topic Chronology", "", "Descriptive lexical assignments over title and available abstract text; not semantic clustering.", ""]
    for t in sorted(topic_rows):
        byy = Counter(year_by_id.get(cid) for cid in topic_rows[t])
        topic_md.append(f"## {t}\n\n" + ", ".join(f"{y}: {n}" for y,n in sorted(byy.items()) if y) + "\n")
    (OUT / "TOPIC_CHRONOLOGY.md").write_text("\n".join(topic_md)+"\n", encoding="utf-8")
    latest = sorted([r for r in records if r.get("published_date")], key=lambda r: (r.get("published_date"), r["chronicle_id"]), reverse=True)[:50]
    (OUT / "LATEST.md").write_text("# Latest TSE work\n\nRecent records in the acquired source window, ordered by publication date.\n\n"+"\n".join(item_md(r) for r in latest)+"\n", encoding="utf-8")
    (OUT / "COVERAGE.md").write_text("# Coverage and provenance\n\nThe Chronicle covers the requested 2010-01-01 through 2026-09-12 window using the Toulouse Capitole TSE division archive as first-party listing evidence, OpenAlex as external affiliation discovery/enrichment, and Crossref as DOI metadata enrichment. It is not a claim of exhaustive historical institutional membership: provider coverage and archive completeness remain explicit gaps.\n\nEvery record retains source URLs and a membership evidence label. Metadata-only records do not masquerade as governed Paper KB papers.\n", encoding="utf-8")
    (OUT / "RECONCILIATION_REVIEW.md").write_text("# Reconciliation review\n\nStrong DOI keys merge provider claims. TSE working-paper numbers are preserved as a distinct key when present. OpenAlex records without DOI are matched to a first-party row only on exact normalized title and year; otherwise they retain provider identity. Title similarity alone is never used for silent merging.\n", encoding="utf-8")
    # Static browseable HTML.
    rendered = OUT / "rendered"; rendered.mkdir(exist_ok=True)
    def esc(x): return html.escape(str(x or ""))
    year_links = " ".join(f'<a href="years/{y}.html">{y}</a>' for y in range(2010,2027))
    cards = []
    for r in latest[:30]:
        cards.append(f'<article><h2>{esc(r["title"])}</h2><p>{esc(", ".join(a.get("name","") for a in r.get("authors") or []))} ({esc(r.get("year"))})</p><p>{esc(r.get("abstract") or "Abstract unavailable.")}</p><small>{esc(r["chronicle_id"])} · {esc(r.get("membership_evidence"))}</small></article>')
    head = f"<!doctype html><meta charset='utf-8'><title>TSE Research Chronicle</title><style>body{{font:16px system-ui;max-width:1100px;margin:2rem auto;padding:0 1rem}}nav a{{margin-right:.7rem}}article{{border-top:1px solid #ccc;padding:1rem 0}}small{{color:#666}}</style>"
    (rendered / "index.html").write_text(head+f"<h1>Toulouse School of Economics</h1><p>Research Chronicle 2010–2026</p><p>{len(records)} records · {abstract_count} abstracts · {kinds.get('working-paper',0)} working papers · {len(author_rows)} authors</p><nav><a href='latest.html'>Latest</a> <a href='authors.html'>Authors</a> <a href='topics.html'>Topics</a> <a href='coverage.html'>Coverage</a></nav><p>{year_links}</p><h2>Latest</h2>"+"".join(cards), encoding="utf-8")
    (rendered / "latest.html").write_text(head+"<h1>Latest TSE work</h1>"+"".join(cards), encoding="utf-8")
    author_html = "<h1>Authors</h1><ul>"+"".join(f"<li>{esc(a['display_name'])} — {a['work_count']} works ({a['first_year']}–{a['latest_year']})</li>" for a in author_rows)+"</ul>"
    topic_html = "<h1>Topics</h1><ul>"+"".join(f"<li>{esc(t)} — {n}</li>" for t,n in sorted(topics_count.items()))+"</ul>"
    (rendered / "authors.html").write_text(head+author_html, encoding="utf-8")
    (rendered / "topics.html").write_text(head+topic_html, encoding="utf-8")
    (rendered / "coverage.html").write_text(head+"<h1>Coverage and provenance</h1><p>First-party TSE listings are distinguished from OpenAlex affiliation enrichment and Crossref DOI metadata. See the Markdown coverage report.</p>", encoding="utf-8")
    for y in range(2010,2027):
        rs = [r for r in records if r.get("year") == y]
        (rendered / "years").mkdir(exist_ok=True)
        body = head+f"<h1>TSE publications — {y}</h1><p>{len(rs)} records</p>"+"".join(cards if False else [f'<article><h2>{esc(r["title"])}</h2><p>{esc(", ".join(a.get("name","") for a in r.get("authors") or []))}</p><p>{esc(r.get("abstract") or "Abstract unavailable.")}</p></article>' for r in rs])
        (rendered / "years" / f"{y}.html").write_text(body, encoding="utf-8")
    manifest_out = {"schema_version": 1, "institution": {"id": "tse", "display_name": "Toulouse School of Economics", "openalex_id": OPENALEX_INST, "ror_id": ROR}, "coverage": {"from_date": "2010-01-01", "through_date": TODAY}, "counts": counts, "source_manifest_sha256": sha(manifest), "records_sha256": sha(records), "generated_at": datetime.now(timezone.utc).isoformat()}
    (OUT / "manifest.json").write_text(json.dumps(manifest_out, ensure_ascii=False, indent=2, sort_keys=True)+"\n", encoding="utf-8")
    return counts


def build():
    records = reconcile()
    links = link_paper_kb(records)
    assigns = topics(records)
    manifest = json.loads((OUT / "acquisition" / "run_manifest.json").read_text(encoding="utf-8"))
    return render(records, assigns, links, manifest)


def qa():
    records = read_jsonl(OUT / "papers.jsonl")
    assigns = read_jsonl(OUT / "topic_assignments.jsonl")
    ids = [r.get("chronicle_id") for r in records]
    dois = [norm(r.get("doi")) for r in records if r.get("doi")]
    wps = [norm(r.get("working_paper_number")) for r in records if r.get("working_paper_number")]
    record_ids = set(ids)
    assignment_ids = {a.get("chronicle_id") for a in assigns}
    link_rows = read_jsonl(OUT / "paper_kb_links.jsonl")
    result = {
        "record_count": len(records),
        "duplicate_chronicle_ids": len(ids) - len(record_ids),
        "duplicate_normalized_dois": len(dois) - len(set(dois)),
        "duplicate_working_paper_numbers": len(wps) - len(set(wps)),
        "records_without_provenance": sum(not r.get("sources") for r in records),
        "abstract_source_mismatches": sum(bool(r.get("abstract")) and not r.get("abstract_source") for r in records),
        "topic_assignments_to_unknown_records": len(assignment_ids - record_ids),
        "paper_kb_links_unresolved": sum(not r.get("paper_uid") or not Path(r.get("chunk_set", "")).exists() for r in link_rows),
        "years_missing": [y for y in range(2010, 2027) if not (OUT / "years" / f"{y}.md").exists()],
        "records_outside_window": sum(not (r.get("year") and 2010 <= int(r["year"]) <= 2026) for r in records),
    }
    failure_keys = ["duplicate_chronicle_ids", "duplicate_normalized_dois", "duplicate_working_paper_numbers", "records_without_provenance", "abstract_source_mismatches", "topic_assignments_to_unknown_records", "paper_kb_links_unresolved", "years_missing", "records_outside_window"]
    result["pass"] = not any(result[k] for k in failure_keys)
    (OUT / "qa.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["pass"] else 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("acquire"); a.add_argument("--sleep", type=float, default=0.1); a.add_argument("--max-crossref", type=int, default=300)
    sub.add_parser("build")
    sub.add_parser("qa")
    sub.add_parser("all")
    args = ap.parse_args()
    if args.cmd in ("acquire", "all"):
        acquire(args)
    if args.cmd in ("build", "all"):
        print(json.dumps(build(), indent=2, sort_keys=True))
    if args.cmd == "qa":
        raise SystemExit(qa())


if __name__ == "__main__":
    main()
