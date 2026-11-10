"""Madhab rulings: exact matching, the general verdict kept, product-specific evidence wins."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("rulings", Path(__file__).resolve().parents[1] / "app/services/rulings.py")
ru = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ru)

INGREDIENTS = [
    {"id": "gel", "name": "Gelatin", "e_number": "E441", "common_aliases": ["Gelatine"]},
    {"id": "ren", "name": "Rennet", "e_number": None, "common_aliases": []},
    {"id": "shr", "name": "Shrimp", "e_number": None, "common_aliases": ["Prawn", "Prawns"]},
]


def ruling(ingredient, madhab, status, superseded=None):
    return {"ingredient_id": ingredient, "madhab": madhab, "status": status, "reasoning": f"{madhab} view on {ingredient}",
            "source": "Test scholar, Test work", "source_url": None, "superseded_at": superseded}


INDEX = ru.build_index(
    INGREDIENTS,
    [ruling("shr", "hanafi", "haram"), ruling("shr", "shafii", "halal"), ruling("shr", "maliki", "halal"),
     ruling("gel", "hanafi", "halal"), ruling("gel", "shafii", "haram"), ruling("gel", "hanbali", "uncertain", superseded="2026-01-01")],
)


def scan(flagged, overall):
    return {"id": "p", "overall_status": overall, "flagged_ingredients": flagged, "tier": 3, "verified_source": None}


def test_matching_is_exact_on_name_alias_or_e_number():
    assert ru.find_ingredient({"name": "Gelatin (E441)"}, INDEX) == "gel"
    assert ru.find_ingredient({"name": "Beef gelatine", "e_number": "E 441"}, INDEX) == "gel"
    assert ru.find_ingredient({"name": "gelatine"}, INDEX) == "gel"
    assert ru.find_ingredient({"name": "Prawns"}, INDEX) == "shr"
    assert ru.find_ingredient({"name": "Microbial rennet"}, INDEX) is None, "a different thing must not inherit the ruling"
    assert ru.find_ingredient({"name": "Shrimp flavour"}, INDEX) is None


def test_a_school_ruling_changes_the_item_and_the_overall_verdict():
    base = scan([{"name": "Shrimp", "status": "halal", "reason": "Seafood"}], "halal")
    out = ru.apply_madhab(base, "hanafi", INDEX)
    assert out["overall_status"] == "haram" and out["flagged_ingredients"][0]["status"] == "haram"
    assert out["flagged_ingredients"][0]["madhab_ruling"] == {"madhab": "hanafi", "source": "Test scholar, Test work", "source_url": None}
    assert out["flagged_ingredients"][0]["general_status"] == "halal"
    assert out["madhab_applied"] == {"madhab": "hanafi", "name": "Hanafi", "rulings": 1, "changed": 1, "general_status": "halal"}
    assert base["overall_status"] == "halal" and "madhab_ruling" not in base["flagged_ingredients"][0], "the general response is untouched"

    relaxed = ru.apply_madhab(scan([{"name": "Gelatin", "status": "uncertain"}], "uncertain"), "hanafi", INDEX)
    assert relaxed["overall_status"] == "halal"


def test_no_ruling_no_change():
    base = scan([{"name": "Shrimp", "status": "halal"}], "halal")
    assert ru.apply_madhab(base, "general", INDEX) is base
    assert ru.apply_madhab(base, None, INDEX) is base
    assert ru.apply_madhab(base, "hanbali", INDEX) is base, "no Hanbali ruling on file"
    assert ru.apply_madhab(scan([{"name": "Gelatin", "status": "uncertain"}], "uncertain"), "hanbali", INDEX)["overall_status"] == "uncertain", "a superseded ruling is ignored"


def test_strict_takes_the_most_cautious_school():
    out = ru.apply_madhab(scan([{"name": "Shrimp", "status": "halal"}, {"name": "Gelatin", "status": "uncertain"}], "uncertain"), "strict", INDEX)
    assert [i["status"] for i in out["flagged_ingredients"]] == ["haram", "haram"]
    assert out["madhab_applied"]["name"] == "strictest" and out["overall_status"] == "haram"


def test_overall_is_left_alone_when_something_else_decided_it():
    # Overall is uncertain, but the only flagged item is halal: the verdict came from elsewhere (e.g. an unreadable label).
    out = ru.apply_madhab(scan([{"name": "Shrimp", "status": "halal"}], "uncertain"), "shafii", INDEX)
    assert out["overall_status"] == "uncertain" and out["madhab_applied"]["changed"] == 0


def test_product_specific_verification_outranks_a_general_ruling():
    confirmed = {**scan([{"name": "Gelatin", "status": "halal", "reason": "Confirmed by the maker"}], "halal"), "tier": 1, "verified_source": "manufacturer"}
    assert ru.apply_madhab(confirmed, "shafii", INDEX) is confirmed
