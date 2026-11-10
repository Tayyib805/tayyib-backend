"""Admin maintenance jobs, started from the Review screen.

- categories: fill Open Food Facts categories for products saved before
  categories existed (they can't appear as halal alternatives until then).
- recheck: run the ingredient check for products that have an ingredient
  list but no verdict yet (their analysis failed, e.g. while the API key was
  invalid). Only automatic (tier 3) verdicts are touched.
- device: products whose Open Food Facts record was fetched by a shopper's
  device (when our own lookups were busy) are looked up again by the server
  and checked from Open Food Facts' own record.

Jobs run in the background on this server, one at a time per kind. Progress
lives in memory: it resets if the server restarts, and the job can simply be
started again (it only picks up products that still need it). The Review
screen polls progress, which also keeps a sleeping free instance awake.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.database import get_current_user, get_supabase, is_admin
from app.services.openfoodfacts import lookup_barcode

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/maintenance", tags=["admin"])

Kind = Literal["categories", "recheck", "device"]
KINDS = ("categories", "recheck", "device")
# Open Food Facts allows 15 product reads a minute per IP; scans share that budget.
OFF_DELAY_SECONDS = 6.0
RECHECK_DELAY_SECONDS = 1.0
BUSY_WAIT_SECONDS = 60.0

_jobs: dict[str, dict] = {}
_tasks: dict[str, asyncio.Task] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_admin(user: dict, supabase: Client) -> None:
    if not is_admin(user["id"], supabase):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only.")


def _pending(supabase: Client, kind: str) -> list[dict]:
    """Products this job still needs to visit."""

    if kind == "categories":
        rows = (
            supabase.table("products").select("id,name,barcode,categories").not_.is_("barcode", "null").limit(2000).execute()
        ).data or []
        return [r for r in rows if not r.get("categories")]
    if kind == "device":
        from app.services.label_qc import formulations

        rows = (
            supabase.table("product_formulations").select("product_id,country_code")
            .eq("data_source", "user_submission").limit(2000).execute()
        ).data or []
        pairs = sorted({(r["product_id"], r.get("country_code") or "US") for r in rows})
        if not pairs:
            return []
        products = (
            supabase.table("products").select("id,name,barcode").in_("id", sorted({p for p, _ in pairs})).execute()
        ).data or []
        by_id = {p["id"]: p for p in products if p.get("barcode")}
        todo = []
        for product_id, country in pairs:
            # Only while the device's record is still the one the verdict rests on.
            current, _ = formulations(supabase, product_id, country)
            if product_id in by_id and current and current.get("data_source") == "user_submission":
                todo.append({**by_id[product_id], "country_code": country})
        return todo
    rows = (
        supabase.table("products")
        .select("id,name,verification_tier")
        .is_("analyzed_at", "null")
        .limit(2000)
        .execute()
    ).data or []
    rows = [r for r in rows if int(r.get("verification_tier") or 3) >= 3]
    if not rows:
        return []
    with_list = (
        supabase.table("product_formulations")
        .select("product_id,ingredients_text")
        .in_("product_id", [r["id"] for r in rows])
        .execute()
    ).data or []
    has = {f["product_id"] for f in with_list if (f.get("ingredients_text") or "").strip()}
    return [r for r in rows if r["id"] in has]


def counts(supabase: Client) -> dict:
    return {kind: len(_pending(supabase, kind)) for kind in KINDS}


def _public(kind: str) -> dict | None:
    job = _jobs.get(kind)
    if not job:
        return None
    return {k: v for k, v in job.items() if k != "log"} | {"recent": job["log"][-5:]}


async def _run_categories(supabase: Client, job: dict, todo: list[dict]) -> None:
    for row in todo:
        if job.get("stop"):
            job["status"] = "stopped"
            return
        try:
            off = await lookup_barcode(row["barcode"], "US")
            if off.get("busy"):
                await asyncio.sleep(BUSY_WAIT_SECONDS)
                off = await lookup_barcode(row["barcode"], "US")
            if not off.get("found"):
                off = await lookup_barcode(row["barcode"], "WORLD")
            categories = off.get("categories") or []
            if categories:
                supabase.table("products").update(
                    {"categories": categories, "primary_category": off.get("primary_category")}
                ).eq("id", row["id"]).execute()
                job["changed"] += 1
                job["log"].append(f"{row.get('name') or row['barcode']}: {off.get('primary_category') or 'filled'}")
            else:
                job["skipped"] += 1
                job["log"].append(f"{row.get('name') or row['barcode']}: none listed on Open Food Facts")
        except Exception as exc:  # keep going; one product shouldn't stop the job
            job["failed"] += 1
            log.warning("category backfill failed for %s: %s", row.get("id"), exc)
        job["done"] += 1
        await asyncio.sleep(OFF_DELAY_SECONDS)


async def _run_recheck(supabase: Client, job: dict, todo: list[dict]) -> None:
    from app.routers.products import _record_analysis, _save_flagged_ingredients, _normalize_confidence
    from app.services.claude_analysis import analyze_ingredients, describe_error, self_check
    from app.services.label_qc import formulations

    if not await self_check():
        job["status"] = "failed"
        job["message"] = "Ingredient checking isn't working right now (check the Anthropic API key on Render). Nothing was changed."
        return
    known = supabase.table("ingredients").select("name,e_number,default_status").execute().data or []
    for row in todo:
        if job.get("stop"):
            job["status"] = "stopped"
            return
        try:
            countries = (
                supabase.table("product_formulations").select("country_code").eq("product_id", row["id"]).execute()
            ).data or []
            formulation = None
            for country in sorted({c.get("country_code") or "US" for c in countries}, key=lambda c: c != "US"):
                current, _ = formulations(supabase, row["id"], country)
                if current and (current.get("ingredients_text") or "").strip():
                    formulation = current
                    break
            if not formulation:
                job["skipped"] += 1
                job["done"] += 1
                continue
            text = formulation["ingredients_text"]
            analysis = await analyze_ingredients(text, known)
            if analysis.get("failed"):
                job["failed"] += 1
                job["log"].append(f"{row.get('name')}: check failed ({analysis.get('error') or 'unavailable'})")
                if job["failed"] >= 3 and job["changed"] == 0:
                    job["status"] = "failed"
                    job["message"] = "Checks keep failing, so the job stopped. Nothing else was changed."
                    return
            else:
                supabase.table("products").update(
                    {
                        "overall_status": analysis.get("overall_status", "uncertain"),
                        "confidence_level": _normalize_confidence(analysis.get("confidence")),
                        "analyzed_at": _now(),
                        "updated_at": _now(),
                    }
                ).eq("id", row["id"]).eq("verification_tier", 3).execute()
                _save_flagged_ingredients(supabase, row["id"], analysis.get("flagged_ingredients") or [], analysis.get("confidence"))
                _record_analysis(
                    supabase, row["id"], formulation, text, analysis, formulation.get("data_source") or "label_scan"
                )
                job["changed"] += 1
                job["log"].append(f"{row.get('name')}: {analysis.get('overall_status')}")
        except Exception as exc:
            job["failed"] += 1
            log.warning("recheck failed for %s: %s", row.get("id"), describe_error(exc))
        job["done"] += 1
        await asyncio.sleep(RECHECK_DELAY_SECONDS)


async def _run_device(supabase: Client, job: dict, todo: list[dict]) -> None:
    from app.routers.products import _analyze_and_enrich_product, _fetch_product_by_barcode_country
    from app.services.claude_analysis import self_check

    if not await self_check():
        job["status"] = "failed"
        job["message"] = "Ingredient checking isn't working right now (check the Anthropic API key on Render). Nothing was changed."
        return
    for row in todo:
        if job.get("stop"):
            job["status"] = "stopped"
            return
        name = row.get("name") or row["barcode"]
        try:
            off = await lookup_barcode(row["barcode"], row["country_code"])
            if off.get("busy"):
                await asyncio.sleep(BUSY_WAIT_SECONDS)
                off = await lookup_barcode(row["barcode"], row["country_code"])
            if off.get("busy") or off.get("error"):
                job["status"] = "stopped"
                job["message"] = "Open Food Facts is busy right now. Start this again later; it continues with what's left."
                return
            if not off.get("found"):
                job["skipped"] += 1
                job["log"].append(f"{name}: not on Open Food Facts any more, left as it is")
            else:
                product = _fetch_product_by_barcode_country(supabase, row["barcode"], row["country_code"])
                before = (product or {}).get("product_formulation") or {}
                scans = (product or {}).get("scan_count")
                await _analyze_and_enrich_product(supabase, product, off, row["barcode"], row["country_code"])
                if product and scans is not None:
                    # A re-check isn't a scan.
                    supabase.table("products").update({"scan_count": scans}).eq("id", product["id"]).execute()
                after = _fetch_product_by_barcode_country(supabase, row["barcode"], row["country_code"]) or {}
                now = after.get("product_formulation") or {}
                if now.get("data_source") == "user_submission":
                    job["skipped"] += 1
                    job["log"].append(f"{name}: couldn't be checked this time")
                else:
                    job["changed"] += 1
                    same = " ".join((before.get("ingredients_text") or "").split()).lower() == " ".join((now.get("ingredients_text") or "").split()).lower()
                    job["log"].append(f"{name}: " + ("matches Open Food Facts" if same else "replaced with Open Food Facts' own record"))
        except Exception as exc:
            job["failed"] += 1
            log.warning("device re-check failed for %s: %s", row.get("id"), type(exc).__name__)
        job["done"] += 1
        await asyncio.sleep(OFF_DELAY_SECONDS)


async def _run(kind: str, supabase: Client, todo: list[dict]) -> None:
    job = _jobs[kind]
    try:
        if kind == "categories":
            await _run_categories(supabase, job, todo)
        elif kind == "device":
            await _run_device(supabase, job, todo)
        else:
            await _run_recheck(supabase, job, todo)
        if job["status"] == "running":
            job["status"] = "done"
    except Exception as exc:
        job["status"] = "failed"
        job["message"] = "The job stopped unexpectedly. Start it again to pick up where it left off."
        log.exception("maintenance job %s crashed: %s", kind, exc)
    finally:
        job["finished_at"] = _now()


@router.get("")
async def maintenance_status(user: dict = Depends(get_current_user), supabase: Client = Depends(get_supabase)) -> dict:
    _require_admin(user, supabase)
    return {"pending": counts(supabase), "jobs": {k: _public(k) for k in KINDS}}


@router.post("/{kind}")
async def start_job(kind: Kind, user: dict = Depends(get_current_user), supabase: Client = Depends(get_supabase)) -> dict:
    _require_admin(user, supabase)
    if _jobs.get(kind, {}).get("status") == "running":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That job is already running.")
    todo = _pending(supabase, kind)
    _jobs[kind] = {
        "kind": kind,
        "status": "running" if todo else "done",
        "total": len(todo),
        "done": 0,
        "changed": 0,
        "skipped": 0,
        "failed": 0,
        "started_at": _now(),
        "finished_at": None if todo else _now(),
        "message": None if todo else "Nothing needs this right now.",
        "log": [],
    }
    if todo:
        _tasks[kind] = asyncio.create_task(_run(kind, supabase, todo))
    return {"job": _public(kind)}


@router.post("/{kind}/stop")
async def stop_job(kind: Kind, user: dict = Depends(get_current_user), supabase: Client = Depends(get_supabase)) -> dict:
    _require_admin(user, supabase)
    job = _jobs.get(kind)
    if job and job.get("status") == "running":
        job["stop"] = True
    return {"job": _public(kind)}
