"""Unit tests for the Open Food Facts read budget and record normalisation.

httpx and settings aren't needed for these paths, so light stand-ins are
registered before the module loads.
"""

import importlib.util
import sys
import types
from pathlib import Path

sys.modules.setdefault("httpx", types.SimpleNamespace(TimeoutException=TimeoutError, AsyncClient=None))
_config = types.ModuleType("app.config")
_config.get_settings = lambda: types.SimpleNamespace(OPEN_FOOD_FACTS_USER_AGENT="tayyib-tests/1.0")
sys.modules.setdefault("app", types.ModuleType("app"))
sys.modules["app.config"] = _config

_spec = importlib.util.spec_from_file_location(
    "off", Path(__file__).resolve().parents[1] / "app/services/openfoodfacts.py"
)
off = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(off)


def _reset():
    off._product_times.clear()
    off._backoff_until = 0.0
    off._misses.clear()


def test_budget_stops_before_the_published_limit():
    _reset()
    granted = sum(off._take_product_slot() for _ in range(20))
    assert granted == off._PRODUCT_LIMIT_PER_MINUTE < 15


def test_back_off_blocks_reads():
    _reset()
    off._back_off(60)
    assert not off._take_product_slot()


def test_busy_when_budget_spent():
    import asyncio

    _reset()
    for _ in range(off._PRODUCT_LIMIT_PER_MINUTE):
        off._take_product_slot()
    result = asyncio.run(off.lookup_barcode("012345678905"))
    assert result["busy"] is True and result["found"] is False


def test_cached_miss_spends_no_read():
    import asyncio

    _reset()
    off._misses["012345678905"] = off.time.monotonic()
    result = asyncio.run(off.lookup_barcode("012345678905"))
    assert result == {"found": False, "barcode": "012345678905", "country_code": "US"}
    assert not off._product_times


def test_normalize_trims_and_ignores_bad_types():
    record = {
        "product_name": "  Sample Biscuits  ",
        "brands": 42,
        "ingredients_text": "flour, sugar" + "x" * 6000,
        "categories_tags": ["en:biscuits", 7],
        "labels_tags": ["en:halal"],
    }
    p = off.normalize_product(record, "012345678905", "US")
    assert p["name"] == "Sample Biscuits"
    assert p["brand"] is None
    assert len(p["ingredients_text"]) == 5000
    assert p["categories"] == ["en:biscuits"]
    assert p["labels"] == ["en:halal"]
