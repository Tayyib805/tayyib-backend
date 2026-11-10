"""Build the monthly open-data place files for Halal Maps (run by places-data.yml).

For each area in places_areas.json, reads the latest Overture Maps places
release and writes <scope>.json with two kinds of items:

- halal: true  -> Overture marks it halal (category or name). Goes on the map as "Listed".
- halal: false -> a place to eat in an often-halal cuisine. Becomes a candidate
                  for a person to sort; never shown until then.

The API picks the files up from the places-data branch and imports them with
import_places() (match or create, then the sync-safety check). Nothing here
talks to our database.

Usage: python places_data.py AREAS_JSON OUT_DIR
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

import duckdb

from coverage_check import OVERTURE, latest_overture_release

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


def area_items(con, path: str, cols: set[str], bbox: list[float]) -> list[dict]:
    s, w, n, e = bbox
    cat_cols = [c for c in ["categories", "taxonomy", "basic_category"] if c in cols]
    cat_expr = " || ' ' || ".join(f"coalesce(to_json({c})::VARCHAR, '')" for c in cat_cols) or "''"
    primary = "categories.primary" if "categories" in cols else "NULL"
    pick = lambda col, expr: expr if col in cols else "NULL"  # noqa: E731
    rows = con.execute(f"""
        SELECT id, names.primary, bbox.ymin, bbox.xmin, {cat_expr}, {primary},
               {pick('operating_status', 'operating_status')}, {pick('confidence', 'confidence')},
               {pick('addresses', 'addresses[1].freeform')}, {pick('addresses', 'addresses[1].locality')}, {pick('addresses', 'addresses[1].region')},
               {pick('phones', 'phones[1]')}, {pick('websites', 'websites[1]')}
        FROM read_parquet('{path}', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {w} AND {e} AND bbox.ymin BETWEEN {s} AND {n}""").fetchall()
    items = []
    for pid, name, lat, lng, cats, prim, status, confidence, street, city, region, phone, website in rows:
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
        if not (marked or often):
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
            "phone": digits[-10:] if len(digits) >= 10 else None,
            "website": website if website and re.match(r"^https?://", website) else None,
            "cuisine": [cuisine] if cuisine and cuisine not in ("halal", "") else [],
            "kind": kind_of(prim),
            "halal": marked,
            "data": {"categories": prim or None, "confidence": round(confidence, 2) if confidence is not None else None},
        })
    return items


def main() -> None:
    areas = json.load(open(sys.argv[1]))["areas"]
    out_dir = sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)
    release = latest_overture_release()
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    path = f"s3://{OVERTURE}/{release}/theme=places/type=place/*"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path}', hive_partitioning=1)").fetchall()}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    index = {"source": "overture", "release": release.split("/")[-1], "generated_at": now, "areas": [],
             "attribution": "Overture Maps Foundation, overturemaps.org"}
    summary = [f"# Halal Maps open data ({index['release']})", "", "| Area | Marked halal | Candidates to sort |", "|---|---|---|"]
    for area in areas:
        items = area_items(con, path, cols, area["bbox"])
        if area.get("regions"):
            wanted = {r.upper() for r in area["regions"]}
            items = [i for i in items if (i.get("region") or "").upper() in wanted]
        marked = sum(i["halal"] for i in items)
        if not items:
            raise SystemExit(f"{area['scope']}: no places found; refusing to publish an empty file")
        json.dump({"scope": area["scope"], "label": area.get("label"), "source": "overture", "release": index["release"],
                   "generated_at": now, "items": items}, open(os.path.join(out_dir, f"{area['scope']}.json"), "w"), separators=(",", ":"))
        index["areas"].append({"scope": area["scope"], "label": area.get("label"), "file": f"{area['scope']}.json",
                               "generated_at": now, "count": len(items), "marked_halal": marked})
        summary.append(f"| {area.get('label') or area['scope']} | {marked} | {len(items) - marked} |")
    json.dump(index, open(os.path.join(out_dir, "index.json"), "w"), indent=1)
    text = "\n".join(summary)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        open(os.environ["GITHUB_STEP_SUMMARY"], "a").write(text + "\n")


if __name__ == "__main__":
    main()
