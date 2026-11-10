"""Product records fetched by a shopper's device: used only when we can't ask Open Food Facts ourselves, and re-checked later."""

import pytest

from tests.integration.conftest import run

GENUINE = "water, sugar, pork gelatin"
ALTERED = "water, sugar, pectin"


def off_record(barcode, text, name="Test Gummies"):
    return {"found": True, "barcode": barcode, "country_code": "US", "name": name, "brand": "Acme", "image_url": None,
            "ingredients_text": text, "categories": [], "primary_category": None, "website": None}


@pytest.fixture()
def api(pg, monkeypatch):
    from app.routers import maintenance, products
    from app.services import claude_analysis

    state = {"lookup": {"found": False, "busy": True}, "lookups": 0}

    async def fake_lookup(barcode, country_code="US"):
        state["lookups"] += 1
        answer = state["lookup"]
        return {**answer, "barcode": barcode} if answer.get("found") else {"found": False, "barcode": barcode, "country_code": country_code, **answer}

    async def fake_analysis(text, known):
        haram = "pork" in text.lower()
        return {"overall_status": "haram" if haram else "halal", "confidence": "high",
                "flagged_ingredients": [{"name": "pork gelatin", "status": "haram", "reason": "test"}] if haram else []}

    async def ready():
        return True

    monkeypatch.setattr(products, "lookup_barcode", fake_lookup)
    monkeypatch.setattr(maintenance, "lookup_barcode", fake_lookup)
    monkeypatch.setattr(products, "analyze_ingredients", fake_analysis)
    monkeypatch.setattr(claude_analysis, "self_check", ready)
    monkeypatch.setattr(maintenance, "OFF_DELAY_SECONDS", 0)
    monkeypatch.setattr(maintenance, "BUSY_WAIT_SECONDS", 0)
    maintenance._jobs.clear()
    return products, maintenance, state


def device(products, barcode, text):
    return products.DeviceLookup(country_code="US", product={"code": barcode, "product_name": "Test Gummies", "brands": "Acme", "ingredients_text": text})


def stored(pg, barcode):
    return pg.run(
        "select row_to_json(t) from (select p.overall_status, p.scan_count, "
        "(select json_agg(json_build_object('source', f.data_source, 'text', f.ingredients_text) order by f.effective_from) "
        " from public.product_formulations f where f.product_id = p.id) recipes "
        f"from public.products p where p.barcode = '{barcode}') t"
    )


def test_our_own_lookup_decides_when_we_can_ask(pg, api):
    products, _, state = api
    state["lookup"] = off_record("0000000000017", GENUINE)
    out = run(products.scan_from_device("0000000000017", device(products, "0000000000017", ALTERED), supabase=pg))
    assert out["overall_status"] == "haram", "the altered record from the device is ignored"
    assert stored(pg, "0000000000017")["recipes"] == [{"source": "open_food_facts", "text": GENUINE}]


def test_a_product_open_food_facts_does_not_have_is_not_taken_from_a_device(pg, api):
    products, _, state = api
    state["lookup"] = {"found": False}
    out = run(products.scan_from_device("0000000000024", device(products, "0000000000024", ALTERED), supabase=pg))
    assert out.status_code == 404 and out.content["requires_label_scan"] is True
    assert stored(pg, "0000000000024") is None


def test_device_record_is_used_while_we_are_busy_then_rechecked(pg, api, people):
    products, maintenance, state = api
    admin = people(admin=True)
    barcode = "0000000000031"
    state["lookup"] = {"found": False, "busy": True}
    out = run(products.scan_from_device(barcode, device(products, barcode, ALTERED), supabase=pg))
    assert out["overall_status"] == "halal"
    assert stored(pg, barcode)["recipes"] == [{"source": "user_submission", "text": ALTERED}]

    status = run(maintenance.maintenance_status(user=admin, supabase=pg))
    assert status["pending"]["device"] == 1 and set(status["jobs"]) == {"categories", "recheck", "device"}

    async def run_job():
        await maintenance.start_job("device", user=admin, supabase=pg)
        await maintenance._tasks["device"]
        return maintenance._jobs["device"]

    # Still busy: the job stops and changes nothing.
    job = run(run_job())
    assert job["status"] == "stopped" and "busy" in job["message"].lower()
    assert stored(pg, barcode)["recipes"][-1]["source"] == "user_submission"

    # Open Food Facts answers with its own record: the verdict now rests on that.
    scans = stored(pg, barcode)["scan_count"]
    state["lookup"] = off_record(barcode, GENUINE)
    job = run(run_job())
    after = stored(pg, barcode)
    assert job["status"] == "done" and job["changed"] == 1 and "replaced" in job["log"][-1]
    assert after["overall_status"] == "haram" and after["scan_count"] == scans, "a re-check isn't counted as a scan"
    assert after["recipes"][-1] == {"source": "open_food_facts", "text": GENUINE}
    assert maintenance._pending(pg, "device") == []
