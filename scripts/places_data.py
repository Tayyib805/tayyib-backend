"""Build the monthly open-data place files for Halal Maps (run by places-data.yml).

Reads places_areas.json and writes one file per area plus index.json.

Overture Maps areas ("areas") -> <scope>.json, two kinds of items:
- halal: true  -> Overture marks it halal (category or name). Goes on the map as "Listed".
- halal: false -> a place to eat in an often-halal cuisine. Becomes a candidate
                  for a person to sort; never shown until then. Left out of
                  areas with marked_only (a whole country would be too many to sort).
A place already listed in an earlier area is left out of later ones, so the
metro areas come first and the country-wide file only adds what they lack.

OpenStreetMap boxes ("osm") -> osm-<scope>.json: places to eat tagged
diet:halal=yes/only, read from Overpass here because this job can wait and
retry, which the API server can't. If Overpass can't be reached, last month's
file is published again unchanged.

The API picks the files up from the places-data branch and imports them with
import_places() (match or create, then the sync-safety check). Nothing here
talks to our database.

Usage: python places_data.py AREAS_JSON OUT_DIR
Env: PREVIOUS_URL = where last month's files are served (for the OpenStreetMap fallback).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.services.places import osm_import_item  # noqa: E402  (pure functions, no I/O)

OVERTURE = "overturemaps-us-west-2"
OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

OFTEN_HALAL = re.compile(
    r"middle_eastern|pakistani|afghan|halal|turkish|persian|lebanese|arab|yemeni|egyptian|syrian|uyghur|"
    r"bangladeshi|somali|uzbek|iraqi|moroccan|palestinian|jordanian|kebab|shawarma|falafel|indonesian|malaysian"
)
EATERY = re.compile(r"restaurant|fast_food|food_truck|cafe|coffee|bakery|diner|kebab|shawarma|falafel|pizza|burger|sandwich|grill|bbq|deli\b|food_court|eat_and_drink|food_and_drink")
NOT_EATERY = re.compile(r"grocery|supermarket|butcher|meat_shop|market|wholesale|convenience|liquor|bar\b|pub\b|brewery|winery|night_club|caterer|food_bank|mosque|school|store")
GROCERY_NAME = re.compile(r"\b(market|supermarket|grocery|groceries|mart|butcher|meats?|bazaa?r|store|wholesale)\b", re.I)
EATERY_NAME = re.compile(r"\b(grill|grille|kitchen|restaurant|cafe|café|kabob|kebab|kabab|pizza|burger|shawarma|biryani|bistro|chicken|bbq|gyro|falafel|tandoori|cuisine|eatery|express)\b", re.I)


def kind_of(primary: str) -> str:
    if "fast_food" in primary or "burger" in primary or "sandwich" in primary:
        return "fast_food"
    if "cafe" in primary or "coffee" in primary:
        return "cafe"
    if "bakery" in primary:
        return "bakery"
    if "food_truck" in primary:
        return "food_truck"
    return "restaurant"


def area_items(con, path: str, cols: set[str], bbox: list[float], marked_only: bool = False) -> list[dict]:
    s, w, n, e = bbox
    cat_cols = [c for c in ["categories", "taxonomy", "basic_category"] if c in cols]
    cat_expr = " || ' ' || ".join(f"coalesce(to_json({c})::VARCHAR, '')" for c in cat_cols) or "''"
    primary = "categories.primary" if "categories" in cols else "NULL"
    pick = lambda col, expr: expr if col in cols else "NULL"  # noqa: E731
    # A country-sized box holds millions of places: let the database keep only the ones marked halal.
    only_marked = f"AND (lower(names.primary) LIKE '%halal%' OR lower({cat_expr}) LIKE '%halal%')" if marked_only else ""
    rows = con.execute(f"""
        SELECT id, names.primary, bbox.ymin, bbox.xmin, {cat_expr}, {primary},
               {pick('operating_status', 'operating_status')}, {pick('confidence', 'confidence')},
               {pick('addresses', 'addresses[1].freeform')}, {pick('addresses', 'addresses[1].locality')}, {pick('addresses', 'addresses[1].region')},
               {pick('phones', 'phones[1]')}, {pick('websites', 'websites[1]')}, {pick('addresses', 'addresses[1].country')}
        FROM read_parquet('{path}', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {w} AND {e} AND bbox.ymin BETWEEN {s} AND {n} {only_marked}""").fetchall()
    items = []
    for pid, name, lat, lng, cats, prim, status, confidence, street, city, region, phone, website, country in rows:
        name, cats, prim = (name or "").strip(), (cats or "").lower(), (prim or "").lower()
        if not prim:
            # Newer releases keep the category under taxonomy/basic_category instead of categories.
            found = re.search(r'"primary":"([^"]+)"', cats) or re.search(r'^\s*"?([a-z_]+)"?\s*$', cats)
            prim = found.group(1) if found else ""
        if not name or (status and status != "open"):
            continue
        if confidence is not None and confidence < 0.5:
            continue
        marked = "halal" in cats or "halal" in name.lower()
        often = bool(OFTEN_HALAL.search(cats + " " + name.lower()))
        if not (marked or (often and not marked_only)):
            continue
        eatery = bool(EATERY.search(prim or cats))
        if NOT_EATERY.search(prim) and not EATERY.search(prim):
            continue
        if not eatery:
            continue
        if GROCERY_NAME.search(name) and not EATERY_NAME.search(name):
            continue
        cuisine = re.sub(r"_?restaurant$", "", prim).replace("_", " ").strip()
        digits = re.sub(r"\D", "", phone or "")
        items.append({
            "external_id": pid,
            "name": name[:160],
            "lat": round(lat, 6),
            "lng": round(lng, 6),
            "address": street,
            "city": city,
            "region": region,
            "country": (country or "").upper() or None,
            "phone": digits[-10:] if len(digits) >= 10 else None,
            "website": website if website and re.match(r"^https?://", website) else None,
            "cuisine": [cuisine] if cuisine and cuisine not in ("halal", "") else [],
            "kind": kind_of(prim),
            "halal": marked,
            "data": {"categories": prim or None, "confidence": round(confidence, 2) if confidence is not None else None},
        })
    return items


def latest_overture_release() -> str:
    import xml.etree.ElementTree as ET

    url = f"https://{OVERTURE}.s3.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"
    root = ET.fromstring(urllib.request.urlopen(url, timeout=60).read())
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    prefixes = sorted(p.text for p in root.findall(".//s3:CommonPrefixes/s3:Prefix", ns))
    if not prefixes:
        raise RuntimeError("no Overture releases listed")
    return prefixes[-1].rstrip("/")


def keep_for_area(items: list[dict], area: dict, already: set[str]) -> list[dict]:
    """Apply an area's state/country limits and drop places an earlier area already lists."""

    regions = {r.upper() for r in area.get("regions") or []}
    countries = {c.upper() for c in area.get("countries") or []}
    kept = []
    for item in items:
        if regions and (item.get("region") or "").upper() not in regions:
            continue
        if countries and (item.get("country") or "").upper() not in countries:
            continue
        if item["external_id"] in already:
            continue
        kept.append({k: v for k, v in item.items() if k != "country"})
    return kept


def overpass_elements(bbox: list[float], servers=OVERPASS, rounds: int = 2, pause: float = 15.0, fetch=None) -> list[dict] | None:
    """Everything tagged diet:halal=yes/only in a box, or None when no server answered properly."""

    s, w, n, e = bbox
    query = (f'[out:json][timeout:180];(nwr["diet:halal"="yes"]({s},{w},{n},{e});'
             f'nwr["diet:halal"="only"]({s},{w},{n},{e}););out center tags;')

    def http(server: str) -> dict:
        request = urllib.request.Request(server, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers={"User-Agent": "tayyib-places-data/1.0 (github.com/Tayyib805/tayyib-backend)"})
        with urllib.request.urlopen(request, timeout=200) as response:
            return json.load(response)

    fetch = fetch or http
    for attempt in range(rounds):
        for server in servers:
            try:
                data = fetch(server)
                # Overpass answers 200 with a "remark" when it ran out of time or memory: that result is partial.
                if data.get("remark"):
                    raise RuntimeError(str(data["remark"])[:200])
                return data.get("elements") or []
            except Exception as exc:  # noqa: BLE001
                print(f"Overpass round {attempt + 1} {server}: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
                time.sleep(pause)
    return None


def osm_area_items(elements: list[dict]) -> list[dict]:
    """Overpass elements as import items, using the same rules as the API (places to eat only, no groceries)."""

    items, seen = [], set()
    for element in elements:
        item = osm_import_item(element)
        if item and item["external_id"] not in seen:
            seen.add(item["external_id"])
            items.append(item)
    return items


def previous_file(base_url: str, source: str, scope: str) -> tuple[dict, dict] | None:
    """(index entry, file contents) of what was published last time for this source and scope, if any."""

    def get(name: str) -> dict:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/{name}", timeout=60) as response:
            return json.load(response)

    try:
        index = get("index.json")
        for entry in index.get("areas") or []:
            if entry.get("scope") == scope and (entry.get("source") or index.get("source")) == source:
                return entry, get(entry["file"])
    except Exception as exc:  # noqa: BLE001
        print(f"No earlier {source} file for {scope}: {type(exc).__name__}", flush=True)
    return None


def main() -> None:
    import duckdb

    config = json.load(open(sys.argv[1]))
    out_dir = sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    release = latest_overture_release()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    path = f"s3://{OVERTURE}/{release}/theme=places/type=place/*"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}', hive_partitioning=1)").fetchall()}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    index = {"source": "overture", "release": release.split("/")[-1], "generated_at": now, "areas": [],
             "attribution": "Overture Maps Foundation, overturemaps.org; OpenStreetMap contributors, openstreetmap.org/copyright"}
    summary = [f"# Halal Maps open data ({index['release']})", "", "| Area | From | Marked halal | Candidates to sort |", "|---|---|---|---|"]

    def write(name: str, body: dict) -> None:
        json.dump(body, open(os.path.join(out_dir, name), "w"), separators=(",", ":"))

    already: set[str] = set()
    for area in config["areas"]:
        started = time.time()
        items = keep_for_area(area_items(con, path, cols, area["bbox"], bool(area.get("marked_only"))), area, already)
        already.update(i["external_id"] for i in items)
        marked = sum(i["halal"] for i in items)
        print(f"{area['scope']}: {len(items)} places in {time.time() - started:.0f}s", flush=True)
        if not items:
            raise SystemExit(f"{area['scope']}: no places found; refusing to publish an empty file")
        name = f"{area['scope']}.json"
        write(name, {"scope": area["scope"], "label": area.get("label"), "source": "overture", "release": index["release"],
                     "generated_at": now, "items": items})
        index["areas"].append({"scope": area["scope"], "label": area.get("label"), "source": "overture", "file": name,
                               "generated_at": now, "count": len(items), "marked_halal": marked, "bbox": area["bbox"]})
        summary.append(f"| {area.get('label') or area['scope']} | Overture Maps | {marked} | {len(items) - marked} |")

    for box in config.get("osm") or []:
        elements = overpass_elements(box["bbox"])
        items = osm_area_items(elements) if elements is not None else []
        name = f"osm-{box['scope']}.json"
        if items:
            write(name, {"scope": box["scope"], "label": box.get("label"), "source": "osm", "generated_at": now, "items": items})
            index["areas"].append({"scope": box["scope"], "label": box.get("label"), "source": "osm", "file": name,
                                   "generated_at": now, "count": len(items), "marked_halal": len(items), "bbox": box["bbox"]})
            summary.append(f"| {box.get('label') or box['scope']} | OpenStreetMap | {len(items)} | 0 |")
            continue
        # Overpass didn't answer: keep serving last month's file, unchanged, so nothing is re-imported or lost.
        earlier = previous_file(os.environ.get("PREVIOUS_URL", ""), "osm", box["scope"]) if os.environ.get("PREVIOUS_URL") else None
        if earlier:
            entry, body = earlier
            write(entry["file"], body)
            index["areas"].append(entry)
            summary.append(f"| {box.get('label') or box['scope']} | OpenStreetMap (last month's file; the map service didn't answer) | {entry.get('count')} | 0 |")
        else:
            summary.append(f"| {box.get('label') or box['scope']} | OpenStreetMap: not published (the map service didn't answer) | 0 | 0 |")

    json.dump(index, open(os.path.join(out_dir, "index.json"), "w"), indent=1)
    text = "\n".join(summary)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(text + "\n")


if __name__ == "__main__":
    main()
