"""Keep stored places in step with OpenStreetMap, one map square at a time.

The map always answers from the database. Opening the map in a square whose
OpenStreetMap copy is missing or over two weeks old re-reads that square in
the background (inline the very first time, so a new area isn't empty).

Squares are 0.2° (about 22 km). A read covers the square plus a margin, so
people near an edge still see their surroundings. Each place belongs to the
square it sits in; only the square that was fully read can mark a place as
missing, and it takes two healthy reads in a row (see finish_sync_run).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from math import floor

from supabase import Client

from app.services.overpass import fetch_halal_food_bbox
from app.services.places import osm_import_item

log = logging.getLogger("tayyib.places")

CELL_DEG = 0.2
MARGIN_DEG = 0.25
STALE_AFTER = timedelta(days=14)
RETRY_AFTER = timedelta(minutes=10)

_running: set[str] = set()
_background: set[asyncio.Task] = set()
_last_attempt: dict[str, datetime] = {}


def cell_of(lat: float, lng: float) -> str:
    return f"{floor(lat / CELL_DEG)}:{floor(lng / CELL_DEG)}"


def cell_bounds(cell: str) -> tuple[float, float, float, float]:
    """(south, west, north, east) of a square."""

    a, b = (int(x) for x in cell.split(":"))
    return (a * CELL_DEG, b * CELL_DEG, (a + 1) * CELL_DEG, (b + 1) * CELL_DEG)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def needs_sync(row: dict | None) -> bool:
    if not row or not row.get("last_synced_at"):
        return True
    try:
        synced = datetime.fromisoformat(str(row["last_synced_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    return _now() - synced > STALE_AFTER


def note_view(supabase: Client, cell: str) -> dict | None:
    """Record that someone looked at this square; returns its row."""

    try:
        rows = supabase.table("map_cells").select("*").eq("cell", cell).limit(1).execute().data
        if rows:
            supabase.table("map_cells").update({"last_viewed_at": _now().isoformat()}).eq("cell", cell).execute()
            return rows[0]
        supabase.table("map_cells").upsert({"cell": cell}, on_conflict="cell").execute()
    except Exception as exc:  # the map still works without the bookkeeping
        log.warning("map_cells bookkeeping failed: %s", type(exc).__name__)
    return None


def can_attempt(cell: str) -> bool:
    last = _last_attempt.get(cell)
    return cell not in _running and (last is None or _now() - last > RETRY_AFTER)


async def sync_cell(supabase: Client, cell: str) -> bool:
    """Re-read one square from OpenStreetMap. True when it finished cleanly."""

    if cell in _running:
        return False
    _running.add(cell)
    _last_attempt[cell] = _now()
    run_id = None
    try:
        south, west, north, east = cell_bounds(cell)
        run = supabase.table("sync_runs").insert({"source": "osm", "scope": cell}).execute().data
        run_id = run[0]["id"] if run else None
        elements = await fetch_halal_food_bbox(south - MARGIN_DEG, west - MARGIN_DEG, north + MARGIN_DEG, east + MARGIN_DEG)
        if elements is None:
            if run_id:
                supabase.table("sync_runs").update(
                    {"status": "failed", "finished_at": _now().isoformat(), "error": "OpenStreetMap unavailable"}
                ).eq("id", run_id).execute()
            return False

        by_cell: dict[str, list[dict]] = {}
        for element in elements:
            item = osm_import_item(element)
            if item:
                by_cell.setdefault(cell_of(item["lat"], item["lng"]), []).append(item)
        totals = {"added": 0, "matched": 0, "seen": 0}
        for scope, items in by_cell.items():
            for start in range(0, len(items), 200):
                result = supabase.rpc(
                    "import_places", {"items": items[start : start + 200], "p_source": "osm", "p_scope": scope}
                ).execute().data or {}
                for key in totals:
                    totals[key] += int(result.get(key) or 0)

        outcome = "ok"
        if run_id:
            supabase.table("sync_runs").update(
                {"seen": sum(len(v) for v in by_cell.values()), "added": totals["added"], "updated": totals["seen"] + totals["matched"]}
            ).eq("id", run_id).execute()
            outcome = supabase.rpc("finish_sync_run", {"run": run_id}).execute().data or "ok"
        supabase.table("map_cells").upsert(
            {"cell": cell, "last_synced_at": _now().isoformat(), "place_count": len(by_cell.get(cell, []))},
            on_conflict="cell",
        ).execute()
        log.info("Places sync %s: %s elements, %s (%s)", cell, len(elements), totals, outcome)
        return True
    except Exception as exc:
        log.exception("Places sync %s failed: %s", cell, exc)
        if run_id:
            try:
                supabase.table("sync_runs").update(
                    {"status": "failed", "finished_at": _now().isoformat(), "error": type(exc).__name__}
                ).eq("id", run_id).execute()
            except Exception:
                pass
        return False
    finally:
        _running.discard(cell)


def is_running(cell: str) -> bool:
    return cell in _running


def sync_in_background(supabase: Client, cell: str) -> asyncio.Task:
    _tasks_keepalive = _background
    task = asyncio.create_task(sync_cell(supabase, cell))
    _tasks_keepalive.add(task)
    task.add_done_callback(_tasks_keepalive.discard)
    return task


# ── Monthly open data (Overture Maps) ───────────────────────────────────────
# A GitHub Actions job rebuilds one file per area each month and publishes
# them on the places-data branch. We look for newer files a few times a day
# and import them; finish_sync_run() refuses a file that suddenly lost a lot.

OPEN_DATA_CHECK_EVERY = timedelta(hours=6)
_open_data_checked: datetime | None = None
_open_data_running = False


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else None
    except ValueError:
        return None


async def _get_json(url: str) -> dict:
    import httpx

    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "tayyib/1.0"})
        response.raise_for_status()
        return response.json()


def open_data_due() -> bool:
    return not _open_data_running and (_open_data_checked is None or _now() - _open_data_checked > OPEN_DATA_CHECK_EVERY)


async def import_open_data(supabase: Client, base_url: str) -> list[dict]:
    """Import every area file that is newer than our last import of it. Returns what happened per area."""

    global _open_data_checked, _open_data_running
    if _open_data_running:
        return []
    _open_data_running = True
    _open_data_checked = _now()
    results: list[dict] = []
    try:
        index = await _get_json(f"{base_url.rstrip('/')}/index.json")
        source = index.get("source") or "overture"
        for area in index.get("areas") or []:
            scope, generated = area.get("scope"), _parse(area.get("generated_at"))
            if not scope or not area.get("file") or generated is None:
                continue
            last = (
                supabase.table("sync_runs").select("started_at").eq("source", source).eq("scope", scope)
                .in_("status", ["ok", "suspect"]).order("started_at", desc=True).limit(1).execute().data
            )
            if last and (_parse(last[0]["started_at"]) or generated) >= generated:
                continue
            run = supabase.table("sync_runs").insert({"source": source, "scope": scope}).execute().data
            run_id = run[0]["id"]
            try:
                items = (await _get_json(f"{base_url.rstrip('/')}/{area['file']}")).get("items") or []
                totals = {"added": 0, "matched": 0, "seen": 0}
                for start in range(0, len(items), 200):
                    out = supabase.rpc("import_places", {"items": items[start : start + 200], "p_source": source, "p_scope": scope}).execute().data or {}
                    for key in totals:
                        totals[key] += int(out.get(key) or 0)
                supabase.table("sync_runs").update(
                    {"seen": len(items), "added": totals["added"], "updated": totals["seen"] + totals["matched"]}
                ).eq("id", run_id).execute()
                outcome = supabase.rpc("finish_sync_run", {"run": run_id}).execute().data or "ok"
                results.append({"scope": scope, "items": len(items), **totals, "outcome": outcome})
                log.info("Open data %s: %s items, %s (%s)", scope, len(items), totals, outcome)
            except Exception as exc:
                supabase.table("sync_runs").update(
                    {"status": "failed", "finished_at": _now().isoformat(), "error": type(exc).__name__}
                ).eq("id", run_id).execute()
                results.append({"scope": scope, "outcome": "failed", "error": type(exc).__name__})
                log.warning("Open data %s failed: %s", scope, exc)
    except Exception as exc:
        log.warning("Open data check failed: %s", exc)
    finally:
        _open_data_running = False
    return results


def open_data_in_background(supabase: Client, base_url: str) -> asyncio.Task:
    task = asyncio.create_task(import_open_data(supabase, base_url))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task
