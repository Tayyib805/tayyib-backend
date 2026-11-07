"""Unit tests for halal alternative selection (run: python -m pytest tests)."""

import importlib.util
from pathlib import Path

# Load the module directly so these tests run without the app's dependencies.
_spec = importlib.util.spec_from_file_location(
    "alternatives", Path(__file__).resolve().parents[1] / "app/services/alternatives.py"
)
alt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(alt)

GUMMY_TAGS = ["en:snacks", "en:sweet-snacks", "en:confectioneries", "en:candies", "en:gummies"]


def test_chain_is_specific_first_and_skips_umbrellas():
    assert alt.category_chain(GUMMY_TAGS, "en:gummies") == ["en:gummies", "en:candies", "en:confectioneries"]


def test_chain_cuts_at_primary_category():
    tags = [*GUMMY_TAGS, "en:vegan-gummies"]
    assert alt.category_chain(tags, "en:gummies")[0] == "en:gummies"


def test_chain_appends_unknown_primary_and_handles_empty():
    assert alt.category_chain([], "en:cookies") == ["en:cookies"]
    assert alt.category_chain(None, None) == []


def test_category_label():
    assert alt.category_label("en:breakfast-cereals") == "Breakfast cereals"
    assert alt.category_label(None) is None


def test_tayyib_rows_rank_by_category_then_tier_then_scans():
    chain = ["en:gummies", "en:candies"]
    rows = [
        {"id": "a", "barcode": "1", "categories": ["en:candies"], "verification_tier": 1, "scan_count": 99},
        {"id": "b", "barcode": "2", "categories": ["en:gummies"], "verification_tier": 3, "scan_count": 5},
        {"id": "c", "barcode": "3", "categories": ["en:gummies"], "verification_tier": 2, "scan_count": 1},
    ]
    assert [r["id"] for r in alt.rank_tayyib_candidates(rows, chain)] == ["c", "b", "a"]


def test_build_merges_sources_and_never_repeats_or_suggests_the_scanned_product():
    items = alt.build_alternatives(
        scanned_barcode="999",
        chain=["en:gummies"],
        tayyib_rows=[{"id": "a", "barcode": "1", "name": "Ours", "categories": ["en:gummies"], "verification_tier": 2}],
        label_rows=[
            {"barcode": "1", "name": "Dup of ours"},
            {"barcode": "999", "name": "The scanned one"},
            {"barcode": "2", "name": "Labelled"},
        ],
        known_status_by_barcode={},
    )
    assert [(i["source"], i["barcode"]) for i in items] == [("tayyib", "1"), ("label", "2")]
    assert items[0]["note"] == "Community reviewed"


def test_build_drops_labelled_products_our_analysis_disagrees_with():
    items = alt.build_alternatives(
        scanned_barcode=None,
        chain=["en:gummies"],
        tayyib_rows=[],
        label_rows=[{"barcode": "5", "name": "Claims halal"}, {"barcode": "6", "name": "Fine"}],
        known_status_by_barcode={"5": "haram", "6": "halal"},
    )
    assert [i["barcode"] for i in items] == ["6"]


def test_build_respects_limit():
    rows = [{"barcode": str(n), "name": f"P{n}"} for n in range(20)]
    assert len(alt.build_alternatives(scanned_barcode=None, chain=[], tayyib_rows=[], label_rows=rows, known_status_by_barcode={}, limit=4)) == 4


def test_label_note_reflects_our_own_verdict():
    items = alt.build_alternatives(
        scanned_barcode=None,
        chain=[],
        tayyib_rows=[],
        label_rows=[{"barcode": "7", "name": "Known"}, {"barcode": "8", "name": "New"}],
        known_status_by_barcode={"7": "halal"},
    )
    assert items[0]["note"].endswith("our screening agrees")
    assert items[1]["note"].endswith("not yet checked by tayyib")
