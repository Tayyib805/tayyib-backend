"""Halal Maps endpoints against the real schema."""

import pytest

from tests.integration.conftest import run

OC = (33.8353, -117.9145)  # Anaheim


def element(osm_id, name, lat, lng, halal="yes", amenity="restaurant", **tags):
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lng, "tags": {"name": name, "amenity": amenity, "diet:halal": halal, **tags}}


@pytest.fixture()
def api(pg, monkeypatch):
    from fastapi import HTTPException  # noqa: F401  (stub or real)

    from app.routers import restaurants
    from app.services import places_store

    places_store._running.clear()
    places_store._last_attempt.clear()
    places_store._imported.clear()
    places_store._imported_checked = None
    places_store._osm_failures = 0
    places_store._osm_last_failure = None
    # Tests choose their own covered boxes; by default nothing is covered and squares use the live service.
    monkeypatch.setattr(places_store, "configured_boxes", lambda: [])
    state = {"elements": [], "calls": 0}

    async def fake_fetch(south, west, north, east):
        state["calls"] += 1
        return state["elements"]

    monkeypatch.setattr(places_store, "fetch_halal_food_bbox", fake_fetch)
    monkeypatch.setattr(places_store, "open_data_due", lambda: False)
    # Points and notifications are covered elsewhere; keep these tests on places.
    return restaurants, state


def names(result):
    return sorted(p["name"] for p in result["items"])


def test_first_view_reads_openstreetmap_once_then_answers_from_the_database(pg, api):
    r, state = api
    state["elements"] = [
        element(1, "Al Amir", 33.8353, -117.9145, "only", cuisine="lebanese", phone="+1 714 555 0100"),
        element(2, "Kabob Express", 33.8400, -117.9200),
        element(3, "Halal Meat Market", 33.8410, -117.9210),  # a grocery: never listed
        element(4, "Somewhere Else Grill", 33.99, -117.60),   # outside 8 km
    ]
    first = run(r.nearby_places(lat=OC[0], lng=OC[1], radius_km=8, supabase=pg))
    assert first["osm"] == "ok" and names(first) == ["Al Amir", "Kabob Express"]
    amir = next(p for p in first["items"] if p["name"] == "Al Amir")
    assert amir["halal_level"] == "fully_halal" and amir["tier"] == "listed" and amir["labels"]["halal_menu"] == "full"
    assert amir["distance_km"] == 0 and amir["cuisine"] == "Lebanese"
    kabob = next(p for p in first["items"] if p["name"] == "Kabob Express")
    assert kabob["halal_level"] == "halal" and kabob["labels"]["alcohol"] == "unknown"

    calls = state["calls"]
    again = run(r.nearby_places(lat=OC[0], lng=OC[1], radius_km=8, supabase=pg))
    assert names(again) == names(first) and state["calls"] == calls, "a synced square is not re-read"


def test_openstreetmap_down_in_a_new_area_says_so(pg, api):
    r, state = api
    state["elements"] = None
    out = run(r.nearby_places(lat=10.0, lng=10.0, radius_km=8, supabase=pg))
    assert out["items"] == [] and out["osm"] == "unavailable"


def place_id(pg, name):
    return pg.run(f"select to_json(id) from public.places where name = '{name}'")


def tier_status(pg, name):
    row = pg.run(f"select row_to_json(t) from (select tier, status, halal_menu, alcohol, confidence from public.places where name = '{name}') t")
    return row


def test_shoppers_confirm_then_close_a_place(pg, api, people):
    r, _ = api
    pid = place_id(pg, "Kabob Express")
    a, b, c, d = people(), people(), people(), people()

    out = run(r.answer_about_place(pid, r.PlaceAnswers(halal="yes", alcohol="no"), user=a, supabase=pg))
    assert out["saved"] and out["points_awarded"] == 5 and out["place"]["labels"]["alcohol"] == "no"
    assert tier_status(pg, "Kabob Express")["tier"] == "listed"
    again = run(r.answer_about_place(pid, r.PlaceAnswers(halal="yes"), user=a, supabase=pg))
    assert again["points_awarded"] == 0, "points once per place"
    run(r.answer_about_place(pid, r.PlaceAnswers(halal="yes"), user=b, supabase=pg))
    assert tier_status(pg, "Kabob Express")["tier"] == "confirmed"

    # Closure needs several people; reports made after the last "it's open" count.
    for person, expected in ((b, "active"), (c, "possibly_closed"), (d, "closed")):
        run(r.report_place(r.PlaceReport(place_id=pid, reason="closed"), user=person, supabase=pg))
        assert tier_status(pg, "Kabob Express")["status"] == expected
    assert "Kabob Express" not in names(run(r.nearby_places(lat=OC[0], lng=OC[1], radius_km=8, supabase=pg)))
    with pytest.raises(Exception) as err:
        run(r.get_place(pid, supabase=pg))
    assert getattr(err.value, "status_code", None) == 404


def test_adding_a_place_established_account_goes_live_and_duplicates_merge_in(pg, api, people):
    r, _ = api
    veteran, other = people(days_old=40, scans=6), people(days_old=40, scans=6)
    body = r.PlaceCreate(name="Sahara Grill", lat=33.8300, lng=-117.9000, cuisine_type="Syrian, Grill", halal_menu="full", alcohol="no")
    out = run(r.add_place(body, user=veteran, supabase=pg))
    assert out == {"matched": False, "live": True, "id": out["id"], "name": "Sahara Grill"}
    shown = next(p for p in run(r.nearby_places(lat=OC[0], lng=OC[1], radius_km=8, supabase=pg))["items"] if p["name"] == "Sahara Grill")
    assert shown["tier"] == "listed" and shown["halal_level"] == "fully_halal" and shown["labels"]["alcohol"] == "no"

    same = r.PlaceCreate(name="Sahara Grill & Restaurant", lat=33.8301, lng=-117.9001)
    second = run(r.add_place(same, user=other, supabase=pg))
    assert second["matched"] and second["id"] == out["id"]
    assert tier_status(pg, "Sahara Grill")["tier"] == "confirmed", "two shoppers now vouch for it"

    with pytest.raises(Exception) as err:
        run(r.add_place(r.PlaceCreate(name="No Pin Cafe"), user=veteran, supabase=pg))
    assert getattr(err.value, "status_code", None) == 400
    with pytest.raises(Exception) as err:
        run(r.add_place(r.PlaceCreate(name="Halal Meat Market", lat=33.83, lng=-117.9), user=veteran, supabase=pg))
    assert getattr(err.value, "status_code", None) == 400


def test_new_account_place_waits_for_review_then_admin_sorts_it(pg, api, people):
    r, _ = api
    newbie, admin, shopper = people(days_old=1, scans=0), people(admin=True), people()
    out = run(r.add_place(r.PlaceCreate(name="Fresh Falafel House", lat=33.8200, lng=-117.9300, halal_menu="partial"), user=newbie, supabase=pg))
    assert out["live"] is False
    assert "Fresh Falafel House" not in names(run(r.nearby_places(lat=OC[0], lng=OC[1], radius_km=8, supabase=pg)))

    with pytest.raises(Exception) as err:
        run(r.admin_review(lat=None, lng=None, user=shopper, supabase=pg))
    assert getattr(err.value, "status_code", None) == 403
    queue = run(r.admin_review(lat=OC[0], lng=OC[1], user=admin, supabase=pg))
    assert any(c["name"] == "Fresh Falafel House" for c in queue["candidates"])
    assert any(f["places"]["name"] == "Fresh Falafel House" for f in queue["flags"])

    decided = run(r.decide_place(out["id"], r.PlaceDecision(decision="halal"), user=admin, supabase=pg))
    assert decided["status"] == "active" and decided["tier"] == "confirmed"
    shown = next(p for p in run(r.nearby_places(lat=OC[0], lng=OC[1], radius_km=8, supabase=pg))["items"] if p["name"] == "Fresh Falafel House")
    assert shown["halal_level"] == "halal_options", "the adder's menu answer was kept"
    queue = run(r.admin_review(lat=OC[0], lng=OC[1], user=admin, supabase=pg))
    assert not any(f["places"]["name"] == "Fresh Falafel House" for f in queue["flags"])


def test_admin_sorts_imported_candidates_and_handles_flags_and_duplicates(pg, api, people):
    r, _ = api
    admin, shopper = people(admin=True), people()
    result = pg.rpc(
        "import_places",
        {
            "items": [
                {"external_id": "ov-a", "name": "Zaytoon Kitchen", "lat": 33.8100, "lng": -117.9100, "halal": False, "data": {"categories": "middle_eastern_restaurant"}},
                {"external_id": "ov-b", "name": "Brew Pub Kebab", "lat": 33.8110, "lng": -117.9110, "halal": False},
                {"external_id": "ov-c", "name": "Olive Tree", "lat": 33.8500, "lng": -117.9400, "halal": True},
                {"external_id": "ov-d", "name": "Olive Tree Mediterranean Cafe", "lat": 33.8501, "lng": -117.9401, "halal": True},
            ],
            "p_source": "overture",
            "p_scope": "test",
        },
    ).execute().data
    assert result["added"] >= 3
    queue = run(r.admin_review(lat=OC[0], lng=OC[1], user=admin, supabase=pg))
    zaytoon = next(c for c in queue["candidates"] if c["name"] == "Zaytoon Kitchen")
    assert zaytoon["sources"] == ["Overture Maps (middle eastern restaurant)"]
    assert queue["counts"]["candidates"] >= 2

    run(r.decide_place(zaytoon["id"], r.PlaceDecision(decision="halal_full", alcohol="no"), user=admin, supabase=pg))
    assert tier_status(pg, "Zaytoon Kitchen") | {"confidence": 0} == {"tier": "confirmed", "status": "active", "halal_menu": "full", "alcohol": "no", "confidence": 0}
    pub = place_id(pg, "Brew Pub Kebab")
    run(r.decide_place(pub, r.PlaceDecision(decision="not_halal", note="Bar; pork on the menu"), user=admin, supabase=pg))
    assert tier_status(pg, "Brew Pub Kebab")["status"] == "hidden"
    shown = names(run(r.nearby_places(lat=OC[0], lng=OC[1], radius_km=8, supabase=pg)))
    assert "Zaytoon Kitchen" in shown and "Brew Pub Kebab" not in shown

    # A flag that isn't about halal or closure goes to review and can be dismissed.
    olive = place_id(pg, "Olive Tree")
    run(r.report_place(r.PlaceReport(place_id=olive, reason="wrong_location", note="Pin is across the street"), user=shopper, supabase=pg))
    run(r.report_place(r.PlaceReport(place_id=olive, reason="wrong_location"), user=shopper, supabase=pg))
    queue = run(r.admin_review(lat=OC[0], lng=OC[1], user=admin, supabase=pg))
    flags = [f for f in queue["flags"] if f["place_id"] == olive]
    assert len(flags) == 1 and flags[0]["note"] == "Pin is across the street"
    run(r.dismiss_flag(flags[0]["id"], user=admin, supabase=pg))

    # Possible duplicates: merge keeps one listing with both sources.
    pairs = [d for d in queue["duplicates"] if {d["a"]["name"], d["b"]["name"]} == {"Olive Tree", "Olive Tree Mediterranean Cafe"}]
    if pairs:  # names this close may also have matched outright at import
        keep, other = pairs[0]["a"]["id"], pairs[0]["b"]["id"]
        assert run(r.decide_duplicate(r.DuplicateDecision(keep=keep, other=other, decision="merge"), user=admin, supabase=pg)) == {"merged": True}
    assert sum(n.startswith("Olive Tree") for n in names(run(r.nearby_places(lat=OC[0], lng=OC[1], radius_km=8, supabase=pg)))) == 1


def test_ratings(pg, api, people):
    r, _ = api
    from app.models.restaurant import RestaurantReviewCreate

    amir = place_id(pg, "Al Amir")
    a, b = people(), people()
    run(r.add_place_review(amir, RestaurantReviewCreate(rating=5, halal_confidence=5, body="Great"), user=a, supabase=pg))
    run(r.add_place_review(amir, RestaurantReviewCreate(rating=3, halal_confidence=4), user=b, supabase=pg))
    run(r.add_place_review(amir, RestaurantReviewCreate(rating=4, halal_confidence=4), user=b, supabase=pg))
    place = run(r.get_place(amir, supabase=pg))
    assert place["review_count"] == 2 and place["avg_rating"] == 4.5


def test_monthly_open_data_imports_once_and_refuses_a_gutted_file(pg, api, monkeypatch):
    from app.services import places_store

    def area_file(n, stamp):
        items = [{"external_id": f"od-{i}", "name": f"Open Data Grill {i}", "lat": 41.0 + i * 0.01, "lng": -74.0, "halal": i % 2 == 0} for i in range(n)]
        return {"index.json": {"source": "overture", "areas": [{"scope": "test-area", "file": "test-area.json", "generated_at": stamp}]},
                "test-area.json": {"items": items}}

    files = area_file(30, "2026-11-05T09:00:00+00:00")

    async def fake_get(url):
        return files[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(places_store, "_get_json", fake_get)
    first = run(places_store.import_open_data(pg, "https://example.test/data"))
    assert first == [{"source": "overture", "scope": "test-area", "items": 30, "added": 30, "matched": 0, "seen": 0, "outcome": "ok"}]
    counts = pg.run("select json_object_agg(status, n) from (select status, count(*) n from public.places where name like 'Open Data Grill %' group by 1) t")
    assert counts == {"active": 15, "candidate": 15}

    assert run(places_store.import_open_data(pg, "https://example.test/data")) == [], "same file is not imported twice"

    # Next month's file lost most of its places: imported, but nothing is marked missing.
    files = area_file(5, "2099-01-05T09:00:00+00:00")
    again = run(places_store.import_open_data(pg, "https://example.test/data"))
    assert again[0]["outcome"] == "suspect" and again[0]["seen"] == 5
    missing = pg.run("select to_json(count(*)) from public.place_sources where scope = 'test-area' and missing_runs > 0")
    assert missing == 0


def test_inside_an_imported_box_the_map_never_waits_on_the_live_service(pg, api, monkeypatch):
    from app.services import places_store

    r, state = api
    state["elements"] = None  # the live service is down
    monkeypatch.setattr(places_store, "configured_boxes", lambda: [("overture", "box-a", (50.0, 50.0, 52.0, 52.0))])

    # Configured but not imported yet: the square still falls back to the live service.
    before = run(r.nearby_places(lat=51.0, lng=51.0, radius_km=8, supabase=pg))
    assert before["osm"] == "unavailable" and state["calls"] == 1

    pg.run("with x as (insert into public.sync_runs (source, scope, status, finished_at) values ('overture', 'box-a', 'ok', now()) returning 1) select to_json(count(*)) from x")
    places_store._imported_checked = None
    out = run(r.nearby_places(lat=51.4, lng=51.4, radius_km=8, supabase=pg))
    assert out["items"] == [] and out["osm"] == "ok", "an empty covered square is simply empty"
    assert state["calls"] == 1, "nothing live is read inside a covered box"
    outside = run(r.nearby_places(lat=53.0, lng=51.0, radius_km=8, supabase=pg))
    assert outside["osm"] == "unavailable" and state["calls"] == 2, "just outside the box the live service is still used"


def test_after_repeated_failures_new_areas_answer_at_once(pg, api):
    from datetime import timedelta

    from app.services import places_store

    r, state = api
    state["elements"] = None
    for i in range(3):
        out = run(r.nearby_places(lat=-20.0 - i, lng=20.0, radius_km=8, supabase=pg))
        assert out["osm"] == "unavailable"
    assert state["calls"] == 3 and places_store.osm_struggling()

    out = run(r.nearby_places(lat=-30.0, lng=20.0, radius_km=8, supabase=pg))
    assert out["osm"] == "unavailable" and state["calls"] == 3, "no new read, and no waiting, while the service is struggling"

    # Half an hour without a failure: try again, and a working service clears the streak.
    places_store._osm_last_failure -= timedelta(minutes=31)
    state["elements"] = [element(900, "Cape Kabob", -31.0, 20.0)]
    out = run(r.nearby_places(lat=-31.0, lng=20.0, radius_km=8, supabase=pg))
    assert names(out) == ["Cape Kabob"] and out["osm"] == "ok" and not places_store.osm_struggling()
    assert places_store._osm_failures == 0


def test_monthly_files_from_both_sources_land_on_one_place(pg, api, monkeypatch):
    from app.services import places_store

    stamp = "2026-11-05T09:00:00+00:00"
    files = {
        "index.json": {"source": "overture", "areas": [
            {"scope": "box-b", "source": "overture", "file": "box-b.json", "generated_at": stamp},
            {"scope": "box-b", "source": "osm", "file": "osm-box-b.json", "generated_at": stamp},
            {"scope": "box-b", "source": "somewhere-else", "file": "x.json", "generated_at": stamp},
        ]},
        "box-b.json": {"items": [{"external_id": "ovt-1", "name": "Twin Source Grill", "lat": 45.0, "lng": -93.0, "halal": True}]},
        "osm-box-b.json": {"items": [
            {"external_id": "node/77", "name": "Twin Source Grill", "lat": 45.0001, "lng": -93.0001, "halal": True, "halal_menu": "full"},
            {"external_id": "node/78", "name": "Only On OpenStreetMap Cafe", "lat": 45.01, "lng": -93.0, "halal": True},
        ]},
    }

    async def fake_get(url):
        return files[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(places_store, "_get_json", fake_get)
    out = run(places_store.import_open_data(pg, "https://example.test/data"))
    assert [(o["source"], o["outcome"], o["added"], o["matched"]) for o in out] == [("overture", "ok", 1, 0), ("osm", "ok", 1, 1)]
    row = pg.run("select row_to_json(t) from (select p.halal_menu, p.tier, (select json_agg(s.source order by s.source) from public.place_sources s where s.place_id = p.id) sources "
                 "from public.places p where p.name = 'Twin Source Grill') t")
    assert row == {"halal_menu": "full", "tier": "listed", "sources": ["osm", "overture"]}
    assert ("osm", "box-b") in places_store._imported and ("overture", "box-b") in places_store._imported
