"""Halal Maps coverage check (run by .github/workflows/coverage-check.yml).

Given a sample of halal places a shopper would expect to find (name + lat/lng),
counts how many appear in Foursquare Open Source Places and OpenStreetMap, and
whether each source marks them as halal. Also counts every place in the area
that each source marks as halal. Writes a Markdown summary and a JSON report.

Usage: python coverage_check.py SAMPLE_JSON "south,west,north,east" OUT_DIR
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import duckdb

BUCKET = "fsq-os-places-us-east-1"
FILLER = r"\b(the|restaurant|restaurants|grill|grille|cafe|kitchen|halal|food|foods|and|bar|llc|inc|co|authentic|cuisine)\b"


def key(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.split(" - ")[0].split("|")[0].split("(")[0]
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(FILLER, " ", s)
    return " ".join(s.split()) or (name or "").lower()


def similar(a: str, b: str) -> float:
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return 0.0
    if ka in kb or kb in ka:
        return 1.0
    return difflib.SequenceMatcher(None, ka, kb).ratio()


def metres(lat1, lng1, lat2, lng2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def best_match(place: dict, candidates: list[dict]) -> dict | None:
    best = None
    for c in candidates:
        if c.get("lat") is None:
            continue
        d = metres(place["lat"], place["lng"], c["lat"], c["lng"])
        if d > 250:
            continue
        s = similar(place["name"], c["name"])
        if s < 0.6:
            continue
        score = s - d / 2500
        if best is None or score > best["_score"]:
            best = {**c, "_score": score, "_dist": round(d)}
    return best


def _list(prefix: str) -> tuple[list[str], list[str], str]:
    url = f"https://{BUCKET}.s3.amazonaws.com/?list-type=2&prefix={urllib.parse.quote(prefix)}&delimiter=/"
    body = urllib.request.urlopen(url, timeout=60).read()
    root = ET.fromstring(body)
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    prefixes = [p.text for p in root.findall(".//s3:CommonPrefixes/s3:Prefix", ns)]
    keys = [k.text for k in root.findall(".//s3:Contents/s3:Key", ns)]
    return prefixes, keys, body[:400].decode("utf-8", "replace")


def latest_fsq_release() -> str:
    """Newest release folder, e.g. release/dt=2026-10-07."""
    tried = []
    for prefix in ["release/", ""]:
        try:
            prefixes, keys, head = _list(prefix)
        except Exception as exc:  # noqa: BLE001
            tried.append(f"{prefix!r}: {exc}")
            continue
        found = sorted({m.group(0) for p in prefixes + keys for m in [re.search(r"release/dt=\d{4}-\d{2}-\d{2}", p)] if m})
        if found:
            return found[-1]
        tried.append(f"{prefix!r}: {prefixes[:5]} {keys[:5]} {head}")
    raise RuntimeError("no releases listed; " + " | ".join(tried))


def fsq_places(bbox: tuple[float, float, float, float]) -> tuple[list[dict], str]:
    release = latest_fsq_release()
    s, w, n, e = bbox
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-east-1';")
    path = f"s3://{BUCKET}/{release}/places/parquet/*.parquet"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}')").fetchall()}
    want = [c for c in ["fsq_place_id", "name", "latitude", "longitude", "locality", "tel", "website",
                        "fsq_category_ids", "fsq_category_labels", "date_closed", "date_refreshed"] if c in cols]
    rows = con.execute(
        f"""SELECT {', '.join(want)} FROM read_parquet('{path}')
            WHERE latitude BETWEEN {s} AND {n} AND longitude BETWEEN {w} AND {e}"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(zip(want, r))
        labels = [l for l in (d.get("fsq_category_labels") or []) if l]
        out.append({
            "source_id": d.get("fsq_place_id"), "name": d.get("name") or "", "lat": d.get("latitude"), "lng": d.get("longitude"),
            "city": d.get("locality"), "categories": labels, "closed": d.get("date_closed"),
            "refreshed": str(d.get("date_refreshed") or ""),
            "halal_category": any("halal" in l.lower() for l in labels),
            "halal_name": "halal" in (d.get("name") or "").lower(),
        })
    return out, release


def osm_places(bbox) -> list[dict]:
    import time
    s, w, n, e = bbox
    q = f'[out:json][timeout:120];nwr["diet:halal"~"yes|only"]({s},{w},{n},{e});out tags center;'
    data, errors = None, []
    for server in ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
                   "https://overpass.private.coffee/api/interpreter", "https://overpass-api.de/api/interpreter"]:
        try:
            req = urllib.request.Request(server, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": "tayyib-coverage-check/1.0"})
            data = json.load(urllib.request.urlopen(req, timeout=180))
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{server}: {exc}")
            time.sleep(10)
    if data is None:
        raise RuntimeError("; ".join(errors))
    out = []
    for el in data.get("elements", []):
        t = el.get("tags", {})
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lng = el.get("lon") or (el.get("center") or {}).get("lon")
        out.append({"source_id": f"{el['type']}/{el['id']}", "name": t.get("name", ""), "lat": lat, "lng": lng,
                    "diet_halal": t.get("diet:halal"), "kind": t.get("amenity") or t.get("shop"), "city": t.get("addr:city")})
    return out


def main():
    sample = json.load(open(sys.argv[1]))
    bbox = tuple(float(x) for x in sys.argv[2].split(","))
    out_dir = sys.argv[3]
    os.makedirs(out_dir, exist_ok=True)
    report: dict = {"sample_size": len(sample), "bbox": bbox}

    try:
        fsq, release = fsq_places(bbox)
        report["fsq_release"] = release
        open_fsq = [p for p in fsq if not p["closed"]]
        report["fsq_places_in_area"] = len(open_fsq)
        report["fsq_halal_category_in_area"] = sum(p["halal_category"] for p in open_fsq)
        report["fsq_halal_name_in_area"] = sum(p["halal_name"] for p in open_fsq)
        report["fsq_halal_any_in_area"] = sum(p["halal_category"] or p["halal_name"] for p in open_fsq)
    except Exception as exc:  # noqa: BLE001
        fsq, report["fsq_error"] = [], f"{type(exc).__name__}: {exc}"
    try:
        osm = osm_places(bbox)
        report["osm_halal_in_area"] = len(osm)
    except Exception as exc:  # noqa: BLE001
        osm, report["osm_error"] = [], f"{type(exc).__name__}: {exc}"

    rows = []
    for p in sample:
        f = best_match(p, fsq)
        o = best_match(p, osm)
        rows.append({
            "name": p["name"], "city": p.get("city"), "evidence": p.get("evidence"),
            "fsq_found": bool(f), "fsq_name": f and f["name"], "fsq_categories": f and f["categories"],
            "fsq_marks_halal": bool(f and (f["halal_category"] or f["halal_name"])),
            "fsq_halal_category": bool(f and f["halal_category"]), "fsq_closed": f and f["closed"],
            "fsq_refreshed": f and f["refreshed"], "fsq_distance_m": f and f["_dist"],
            "osm_found": bool(o), "osm_name": o and o["name"], "osm_distance_m": o and o["_dist"],
        })
    report["rows"] = rows
    n = len(rows)
    report["fsq_found"] = sum(r["fsq_found"] for r in rows)
    report["fsq_marks_halal"] = sum(r["fsq_marks_halal"] for r in rows)
    report["fsq_halal_category"] = sum(r["fsq_halal_category"] for r in rows)
    report["osm_found"] = sum(r["osm_found"] for r in rows)
    report["either_marks_halal"] = sum(r["fsq_marks_halal"] or r["osm_found"] for r in rows)
    json.dump(report, open(os.path.join(out_dir, "report.json"), "w"), indent=1, default=str)

    md = [f"# Halal Maps coverage check ({n} sample places)", ""]
    for k in ["fsq_release", "fsq_error", "osm_error"]:
        if k in report:
            md.append(f"- {k}: {report[k]}")
    md += [
        f"- In Foursquare at all: {report['fsq_found']}/{n}",
        f"- Foursquare marks halal (category or name): {report['fsq_marks_halal']}/{n} (category alone: {report['fsq_halal_category']})",
        f"- OpenStreetMap tagged halal: {report['osm_found']}/{n}",
        f"- Shown as halal by either: {report['either_marks_halal']}/{n}",
        f"- Whole area: Foursquare open places {report.get('fsq_places_in_area')}, halal category {report.get('fsq_halal_category_in_area')}, "
        f"halal in name {report.get('fsq_halal_name_in_area')}, either {report.get('fsq_halal_any_in_area')}; OSM halal-tagged {report.get('osm_halal_in_area')}",
        "", "| Place | City | In FSQ | FSQ halal | FSQ categories | Closed | OSM halal |", "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(f"| {r['name'][:40]} | {r['city']} | {'yes' if r['fsq_found'] else 'no'} | {'yes' if r['fsq_marks_halal'] else 'no'} | "
                  f"{', '.join(r['fsq_categories'] or [])[:80]} | {r['fsq_closed'] or ''} | {'yes' if r['osm_found'] else 'no'} |")
    text = "\n".join(md)
    open(os.path.join(out_dir, "summary.md"), "w").write(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(text)
    print(text)


if __name__ == "__main__":
    main()
