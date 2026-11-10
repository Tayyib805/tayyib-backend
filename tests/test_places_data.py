"""The monthly place-file builder's pure parts (scripts/places_data.py) and the coverage boxes the API reads."""

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    spec = importlib.util.spec_from_file_location("places_data", ROOT / "scripts" / "places_data.py")
    module = importlib.util.module_from_spec(spec)
    saved = sys.modules.get("app")
    try:
        # Other unit tests register a placeholder "app"; the script needs the real package.
        if saved is not None and not hasattr(saved, "__path__"):
            for key in [k for k in sys.modules if k == "app" or k.startswith("app.")]:
                del sys.modules[key]
        spec.loader.exec_module(module)
    finally:
        pass
    return module


def item(external_id, region="CA", country="US", halal=True):
    return {"external_id": external_id, "name": "X", "lat": 1.0, "lng": 1.0, "region": region, "country": country, "halal": halal}


def test_areas_keep_their_own_states_and_countries_and_never_repeat_a_place():
    pd = load()
    metro = {"scope": "metro", "regions": ["CA"]}
    country = {"scope": "country", "countries": ["US", "CA"], "marked_only": True}
    found = [item("a"), item("b", region="NV"), item("c", region="ca")]
    first = pd.keep_for_area(found, metro, set())
    assert [i["external_id"] for i in first] == ["a", "c"]
    assert all("country" not in i for i in first), "the country is only used for filtering"

    wide = [item("a"), item("b", region="NV"), item("d", region="ON", country="ca"), item("e", country="MX"), item("f", country=None)]
    second = pd.keep_for_area(wide, country, {i["external_id"] for i in first})
    assert [i["external_id"] for i in second] == ["b", "d"], "already listed, Mexico and unknown-country places are left out"


def test_overpass_retries_other_servers_and_refuses_partial_answers():
    pd = load()
    calls = []

    def fetch(server):
        calls.append(server)
        if server == "one":
            raise TimeoutError("slow")
        if server == "two":
            return {"elements": [{"id": 1}], "remark": "runtime error: Query timed out"}
        return {"elements": [{"id": 1}, {"id": 2}]}

    assert pd.overpass_elements([1, 2, 3, 4], servers=("one", "two", "three"), pause=0, fetch=fetch) == [{"id": 1}, {"id": 2}]
    assert calls == ["one", "two", "three"]

    calls.clear()
    assert pd.overpass_elements([1, 2, 3, 4], servers=("one", "two"), rounds=2, pause=0, fetch=fetch) is None
    assert calls == ["one", "two", "one", "two"], "gives up after every server failed in every round"


def test_openstreetmap_items_follow_the_api_rules():
    pd = load()

    def el(osm_id, name, halal="yes", amenity="restaurant", **tags):
        return {"type": "node", "id": osm_id, "lat": 40.0, "lon": -74.0, "tags": {"name": name, "amenity": amenity, "diet:halal": halal, **tags}}

    items = pd.osm_area_items([
        el(1, "Sultan Grill", "only"),
        el(1, "Sultan Grill", "only"),            # listed twice by the two-part query
        el(2, "Halal Meat Market"),               # a grocery
        el(3, "Corner Shop", amenity="fuel"),     # not a place to eat
        {"type": "way", "id": 9, "center": {"lat": 41.0, "lon": -75.0}, "tags": {"name": "Pita Palace", "amenity": "fast_food", "diet:halal": "yes"}},
    ])
    assert [(i["external_id"], i["name"], i.get("halal_menu")) for i in items] == [("node/1", "Sultan Grill", "full"), ("way/9", "Pita Palace", None)]
    assert all(i["halal"] is True for i in items)


def test_the_published_areas_file_is_what_the_api_calls_covered():
    config = json.loads((ROOT / "scripts" / "places_areas.json").read_text())
    scopes = [a["scope"] for a in config["areas"]]
    assert scopes[-1] == "north-america" and config["areas"][-1].get("marked_only"), "the country-wide area comes last and lists only marked places"
    assert all(not a.get("marked_only") for a in config["areas"][:-1])
    for box in [a["bbox"] for a in config["areas"]] + [a["bbox"] for a in config["osm"]]:
        s, w, n, e = box
        assert -90 <= s < n <= 90 and -180 <= w < e <= 180

    def inside(lat, lng):
        return any(a["bbox"][0] <= lat <= a["bbox"][2] and a["bbox"][1] <= lng <= a["bbox"][3] for a in config["areas"] + config["osm"])

    assert inside(33.8353, -117.9145) and inside(40.9168, -74.1718) and inside(41.88, -87.63)   # Anaheim, Paterson, Chicago
    assert inside(21.31, -157.86) and inside(61.22, -149.9) and inside(43.65, -79.38)            # Honolulu, Anchorage, Toronto
    assert not inside(51.5, -0.12) and not inside(6.5, 3.4)                                      # London, Lagos
