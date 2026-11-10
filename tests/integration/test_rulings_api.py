"""Madhab rulings end to end: admin entry, history, and a verdict adjusted per school."""

import pytest

from tests.integration.conftest import run


@pytest.fixture()
def api(pg):
    from app.routers import ingredients, products

    products._rulings_cache.update({"at": 0.0, "index": None})
    return ingredients, products


def status_of(call):
    with pytest.raises(Exception) as err:
        run(call)
    return getattr(err.value, "status_code", None)


def test_rulings_need_an_admin_and_a_source(pg, api, people):
    ing, _ = api
    shopper = people()
    assert status_of(ing.admin_rulings(user=shopper, supabase=pg)) == 403
    with pytest.raises(Exception):
        ing.RulingCreate(madhab="hanafi", status="haram", reasoning="Too short", source="")
    with pytest.raises(Exception):
        ing.RulingCreate(madhab="general", status="haram", reasoning="Long enough reasoning text.", source="A scholar")


def test_admin_enters_rulings_and_a_scan_follows_the_school(pg, api, people):
    ing, products = api
    admin = people(admin=True)
    added = run(ing.add_ingredient(
        ing.IngredientCreate(name="Test Shellfish", common_aliases=["Test Prawn"], default_status="halal", ruling_source="Sample source for tests"),
        user=admin, supabase=pg))
    assert status_of(ing.add_ingredient(ing.IngredientCreate(name="test shellfish", default_status="halal", ruling_source="Sample source"), user=admin, supabase=pg)) == 409

    first = run(ing.set_ruling(added["id"], ing.RulingCreate(madhab="hanafi", status="uncertain", reasoning="Placeholder reasoning for a test.", source="Test scholar"), user=admin, supabase=pg))
    second = run(ing.set_ruling(added["id"], ing.RulingCreate(madhab="hanafi", status="haram", reasoning="Updated reasoning for a test ruling.", source="Test scholar, Test work", source_url="https://example.org/ruling"), user=admin, supabase=pg))
    history = pg.run(f"select json_agg(json_build_object('status', status, 'current', superseded_at is null, 'next', superseded_by) order by created_at) from public.ingredient_rulings where ingredient_id = '{added['id']}'")
    assert [h["status"] for h in history] == ["uncertain", "haram"] and [h["current"] for h in history] == [False, True]
    assert history[0]["next"] == second["id"], "the old ruling points at the one that replaced it"

    listing = run(ing.admin_rulings(user=admin, supabase=pg))
    entry = next(i for i in listing["ingredients"] if i["id"] == added["id"])
    assert entry["rulings"]["hanafi"]["status"] == "haram" and "shafii" not in entry["rulings"]

    scan = {"id": "p", "overall_status": "halal", "tier": 3, "verified_source": None,
            "flagged_ingredients": [{"name": "Test prawn", "status": "halal", "reason": "Seafood", "e_number": None}]}
    hanafi = products.for_madhab(pg, scan, "hanafi")
    assert hanafi["overall_status"] == "haram" and hanafi["madhab_applied"]["general_status"] == "halal"
    assert hanafi["flagged_ingredients"][0]["madhab_ruling"]["source"] == "Test scholar, Test work"
    assert products.for_madhab(pg, scan, "shafii") is scan and products.for_madhab(pg, scan, None) is scan

    run(ing.withdraw_ruling(second["id"], user=admin, supabase=pg))
    assert products.for_madhab(pg, scan, "hanafi") is scan, "a withdrawn ruling stops applying straight away"
    assert first["id"] != second["id"]
