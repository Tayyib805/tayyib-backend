"""Seed Supabase products from manufacturer scrape results."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv(ROOT_DIR / ".env")

from app.database import supabase  # noqa: E402
from app.services.claude_analysis import analyze_ingredients  # noqa: E402

try:
    from .manufacturer_scraper import run_brand
except ImportError:  # Allows `cd scrapers && python seed_from_scrape.py`.
    from manufacturer_scraper import run_brand


DATA_SOURCE = "manufacturer_scrape"
VALID_STATUSES = {"halal", "haram", "uncertain"}
VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class SeedSummary:
    """Counters printed after a seed run."""

    total_scraped: int = 0
    successfully_seeded: int = 0
    already_existed: int = 0
    failed: int = 0
    statuses: Counter[str] = field(default_factory=Counter)


def now_iso() -> str:
    """Return the current UTC timestamp as an ISO string."""

    return datetime.now(UTC).isoformat()


def normalized_status(value: Any) -> str:
    """Return a valid product status."""

    status = str(value or "uncertain").lower()
    return status if status in VALID_STATUSES else "uncertain"


def normalized_confidence(value: Any) -> str:
    """Return a valid confidence value."""

    confidence = str(value or "low").lower()
    return confidence if confidence in VALID_CONFIDENCE else "low"


def fetch_existing_product(product: dict[str, Any]) -> dict[str, Any] | None:
    """Find an existing product by barcode, or by name/brand when barcode is absent."""

    barcode = product.get("barcode")
    if barcode:
        response = (
            supabase.table("products")
            .select("id,barcode,name,brand")
            .eq("barcode", barcode)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None

    query = supabase.table("products").select("id,barcode,name,brand").eq("name", product["name"])
    if product.get("brand"):
        query = query.eq("brand", product["brand"])
    response = query.limit(1).execute()
    return response.data[0] if response.data else None


def insert_product(product: dict[str, Any]) -> dict[str, Any]:
    """Insert a scraped product and return the saved row."""

    payload = {
        "barcode": product.get("barcode"),
        "name": product["name"],
        "brand": product.get("brand"),
        "image_url": product.get("image_url"),
        "verification_tier": 3,
        "scan_count": 0,
        "overall_status": "uncertain",
        "confidence_level": "low",
        "ingredients_text": product["ingredients_text"],
        "data_source": DATA_SOURCE,
    }
    created = (
        supabase.table("products")
        .insert({key: value for key, value in payload.items() if value is not None})
        .execute()
    )
    if not created.data:
        raise RuntimeError(f"Failed to insert product: {product['name']}")
    return created.data[0]


def insert_formulation(product_id: str, product: dict[str, Any]) -> dict[str, Any]:
    """Insert a product formulation row for the scraped ingredient statement."""

    payload = {
        "product_id": product_id,
        "ingredients_text": product["ingredients_text"],
        "allergens": product.get("allergens"),
        "certifications": product.get("certifications") or [],
        "source_url": product["product_url"],
        "effective_from": now_iso(),
        "data_source": DATA_SOURCE,
    }
    created = supabase.table("product_formulations").insert(payload).execute()
    if not created.data:
        raise RuntimeError(f"Failed to insert formulation for product {product_id}")
    return created.data[0]


def insert_analysis_run(
    product_id: str,
    formulation_id: str,
    ingredients_text: str,
    analysis: dict[str, Any],
) -> None:
    """Store the Claude analysis result."""

    status = normalized_status(analysis.get("overall_status"))
    confidence = normalized_confidence(analysis.get("confidence"))
    supabase.table("analysis_runs").insert(
        {
            "product_id": product_id,
            "product_formulation_id": formulation_id,
            "analysis_type": "ingredients_text",
            "input_text": ingredients_text,
            "result": analysis,
            "overall_status": status,
            "confidence_level": confidence,
            "data_source": DATA_SOURCE,
        }
    ).execute()


def update_product_analysis(product_id: str, analysis: dict[str, Any]) -> tuple[str, str]:
    """Update product status/confidence from analysis and return both values."""

    status = normalized_status(analysis.get("overall_status"))
    confidence = normalized_confidence(analysis.get("confidence"))
    supabase.table("products").update(
        {
            "overall_status": status,
            "confidence_level": confidence,
            "updated_at": now_iso(),
        }
    ).eq("id", product_id).execute()
    return status, confidence


def fetch_known_ingredients() -> list[dict[str, Any]]:
    """Fetch known ingredient rulings for Claude context."""

    response = supabase.table("ingredients").select("name,e_number,default_status").execute()
    return response.data or []


async def seed_product(
    product: dict[str, Any],
    known_ingredients: list[dict[str, Any]],
) -> str | None:
    """Seed one scraped product.

    Returns the analyzed status when a new product is seeded, or None if it
    already existed.
    """

    existing = fetch_existing_product(product)
    if existing:
        return None

    saved_product = insert_product(product)
    formulation = insert_formulation(saved_product["id"], product)
    analysis = await analyze_ingredients(product["ingredients_text"], known_ingredients)
    insert_analysis_run(
        saved_product["id"],
        formulation["id"],
        product["ingredients_text"],
        analysis,
    )
    status, _confidence = update_product_analysis(saved_product["id"], analysis)
    logging.info("Seeded: %s - %s", product["name"], status)
    print(f"Seeded: {product['name']} - {status}")
    return status


async def seed_from_scrape(
    brand: str,
    limit: int | None,
    dry_run: bool,
    delay: float,
) -> SeedSummary:
    """Scrape a brand and seed new products into Supabase."""

    products = await run_brand(brand, limit=limit, delay=delay)
    summary = SeedSummary(total_scraped=len(products))

    if dry_run:
        for product in products:
            print(f"Dry run: {product['name']} ({product['brand']})")
        return summary

    known_ingredients = fetch_known_ingredients()
    for product in products:
        try:
            status = await seed_product(product, known_ingredients)
            if status is None:
                summary.already_existed += 1
                continue
            summary.successfully_seeded += 1
            summary.statuses[status] += 1
        except Exception as exc:
            summary.failed += 1
            logging.error("Failed to seed %s: %s", product.get("name", "unknown product"), exc)

    return summary


def print_summary(summary: SeedSummary) -> None:
    """Print the requested seed summary."""

    print()
    print(f"Total scraped: {summary.total_scraped}")
    print(f"Successfully seeded: {summary.successfully_seeded}")
    print(f"Already existed: {summary.already_existed}")
    print(f"Failed: {summary.failed}")
    print(
        "Halal: "
        f"{summary.statuses['halal']}  "
        "Uncertain: "
        f"{summary.statuses['uncertain']}  "
        "Haram: "
        f"{summary.statuses['haram']}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed Tayyib products from manufacturer scrapes.")
    parser.add_argument("--brand", required=True, help="Brand scraper to run.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N products for testing.")
    parser.add_argument("--delay", type=float, default=2.5, help="Seconds between product requests.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results without saving to Supabase or running Claude analysis.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")
    summary = await seed_from_scrape(args.brand, args.limit, args.dry_run, args.delay)
    print_summary(summary)


if __name__ == "__main__":
    asyncio.run(main())

