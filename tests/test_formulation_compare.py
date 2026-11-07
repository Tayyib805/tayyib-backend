"""Label comparison: tolerate reading noise, never miss a real recipe change."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "formulation_compare", Path(__file__).resolve().parents[1] / "app/services/formulation_compare.py"
)
fc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fc)

STORED = (
    "Sugar, enriched flour (wheat flour, niacin, reduced iron, thiamin mononitrate), "
    "palm oil, cocoa, contains 2% or less of: salt, soy lecithin, natural flavor."
)


def test_same_label_with_reading_noise_matches():
    label = (
        "INGREDIENTS: SUGAR, ENRICHED FLOUR (WHEAT FLOUR, NIACIN, REDUCED IRON, THIAMINE MONONITRATE), "
        "PALM OIL, COCOA (PROCESSED WITH ALKALI), CONTAINS 2% OR LESS OF SALT, SOY LECITHlN, NATURAL FLAVOR. "
        "MAY CONTAIN PEANUTS."
    )
    r = fc.compare(STORED, label)
    assert r["matched"], r


def test_new_animal_ingredient_is_caught():
    label = STORED.replace("palm oil", "palm oil, gelatin")
    r = fc.compare(STORED, label)
    assert not r["matched"] and r["added"] == ["gelatin"] and r["removed"] == []


def test_swapped_ingredient_is_caught():
    label = STORED.replace("soy lecithin", "mono and diglycerides")
    r = fc.compare(STORED, label)
    assert not r["matched"]
    assert "mono and diglycerides" in r["added"] and "soy lecithin" in r["removed"]


def test_removed_ingredient_is_caught():
    r = fc.compare(STORED, STORED.replace(", natural flavor", ""))
    assert not r["matched"] and r["removed"] == ["natural flavor"]


def test_percentages_and_order_dont_matter():
    a = "Water, sugar 12%, citric acid, natural flavors"
    b = "sugar, water, natural flavors, citric acid"
    assert fc.compare(a, b)["matched"]


def test_unreadable_label_is_flagged():
    assert fc.compare(STORED, "")["readable"] is False


def test_source_note_change_is_caught():
    a = "Sugar, gelatin (from beef), citric acid"
    b = "Sugar, gelatin (from pork), citric acid"
    r = fc.compare(a, b)
    assert not r["matched"] and r["added"] == ["from pork"]
