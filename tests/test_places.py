"""Unit tests for map place filtering: only real halal places to eat."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "places", Path(__file__).resolve().parents[1] / "app/services/places.py"
)
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)

ORIGIN = (40.7357, -74.1724)  # Newark, NJ


def el(tags, lat=40.736, lon=-74.17, kind="node", id_=1, center=False):
    base = {"type": kind, "id": id_, "tags": tags}
    if center:
        base["center"] = {"lat": lat, "lon": lon}
    else:
        base.update(lat=lat, lon=lon)
    return base


def test_keeps_halal_restaurants_fast_food_and_cafes():
    for amenity in ("restaurant", "fast_food", "cafe", "food_court"):
        place = pl.classify_osm(el({"name": "Sahara Grill", "amenity": amenity, "diet:halal": "yes"}), ORIGIN)
        assert place and place["kind"] == amenity


def test_fully_halal_vs_serves_halal():
    assert pl.classify_osm(el({"name": "A", "amenity": "restaurant", "diet:halal": "only"}), ORIGIN)["halal_level"] == "fully_halal"
    assert pl.classify_osm(el({"name": "B", "amenity": "restaurant", "diet:halal": "yes"}), ORIGIN)["halal_level"] == "halal"


def test_excludes_limited_no_and_untagged():
    for value in ("limited", "no", ""):
        assert pl.classify_osm(el({"name": "C", "amenity": "restaurant", "diet:halal": value}), ORIGIN) is None


def test_excludes_shops_and_groceries_even_if_tagged_halal():
    assert pl.classify_osm(el({"name": "Al Noor", "shop": "butcher", "diet:halal": "only"}), ORIGIN) is None
    assert pl.classify_osm(el({"name": "Al Noor", "shop": "supermarket", "amenity": "restaurant", "diet:halal": "yes"}), ORIGIN) is None
    assert pl.classify_osm(el({"name": "Halal Meat Market", "amenity": "restaurant", "diet:halal": "only"}), ORIGIN) is None
    assert pl.classify_osm(el({"name": "Madina Grocery", "amenity": "fast_food", "diet:halal": "yes"}), ORIGIN) is None


def test_market_named_eateries_stay():
    assert pl.classify_osm(el({"name": "Nazar Market Grill", "amenity": "restaurant", "diet:halal": "yes"}), ORIGIN)


def test_excludes_non_food_amenities_disused_private_unnamed():
    assert pl.classify_osm(el({"name": "Masjid", "amenity": "place_of_worship", "diet:halal": "yes"}), ORIGIN) is None
    assert pl.classify_osm(el({"name": "Old Spot", "amenity": "restaurant", "diet:halal": "yes", "disused": "yes"}), ORIGIN) is None
    assert pl.classify_osm(el({"name": "Staff Canteen", "amenity": "restaurant", "diet:halal": "yes", "access": "private"}), ORIGIN) is None
    assert pl.classify_osm(el({"amenity": "restaurant", "diet:halal": "yes"}), ORIGIN) is None
    assert pl.classify_osm(el({"name": "Shut", "amenity": "restaurant", "diet:halal": "yes", "opening_hours": "closed"}), ORIGIN) is None


def test_ways_use_center_and_details_are_cleaned():
    place = pl.classify_osm(
        el(
            {
                "name": "Kabab House",
                "amenity": "restaurant",
                "diet:halal": "only",
                "cuisine": "afghan;kebab;halal",
                "addr:housenumber": "12",
                "addr:street": "Main St",
                "addr:city": "Newark",
                "website": "kababhouse.example",
                "diet:halal:certification": "HFSAA",
            },
            kind="way",
            id_=99,
            center=True,
        ),
        ORIGIN,
    )
    assert place["id"] == "osm:way/99"
    assert place["cuisine"] == "Afghan, Kebab"
    assert place["address"] == "12 Main St, Newark"
    assert place["website"] == "https://kababhouse.example"
    assert place["certified"] and place["certifier"] == "HFSAA"


def test_unverified_listings_never_show():
    row = {"id": "x", "name": "New Spot", "lat": 40.736, "lng": -74.17, "halal_status": "unverified"}
    assert pl.from_listing(row, ORIGIN) is None
    row["halal_status"] = "fully_halal"
    assert pl.from_listing(row, ORIGIN)["source"] == "tayyib"


def test_merge_prefers_listing_hides_reported_and_sorts_by_distance():
    listing = pl.from_listing({"id": "x", "name": "Sahara Grill", "lat": 40.7361, "lng": -74.1701, "halal_status": "fully_halal"}, ORIGIN)
    dup = pl.classify_osm(el({"name": "Sahara Grill", "amenity": "restaurant", "diet:halal": "yes"}, lat=40.7362, lon=-74.1702, id_=2), ORIGIN)
    far = pl.classify_osm(el({"name": "Far Cafe", "amenity": "cafe", "diet:halal": "yes"}, lat=40.80, lon=-74.17, id_=3), ORIGIN)
    near = pl.classify_osm(el({"name": "Near Cafe", "amenity": "cafe", "diet:halal": "yes"}, lat=40.7358, lon=-74.1725, id_=4), ORIGIN)
    hidden = pl.classify_osm(el({"name": "Reported", "amenity": "cafe", "diet:halal": "yes"}, id_=5), ORIGIN)
    merged = pl.merge_places([listing], [dup, far, near, hidden], hidden_ids={"osm:node/5"}, radius_km=5)
    assert [p["name"] for p in merged] == ["Near Cafe", "Sahara Grill"]
    assert merged[1]["source"] == "tayyib"
