"""Fill in Open Food Facts categories for products saved before migration 009.

Halal alternatives match products by category, so older products without
categories never show up as alternatives until this runs. New scans store
categories automatically, and the alternatives endpoint fills them in on
demand, so this is a one-time catch-up.

Run from the repo root with your .env in place:

    py -3.12 -m scripts.backfill_categories            # all products missing categories
    py -3.12 -m scripts.backfill_categories --dry-run  # show what would change

Open Food Facts allows 15 product reads per minute per IP, so this waits
4.5 seconds between products.
"""

import argparse
import asyncio

from app.database import supabase
from app.services.openfoodfacts import lookup_barcode

DELAY_SECONDS = 4.5


async def main(dry_run: bool) -> None:
    rows = (
        supabase.table("products")
        .select("id,name,barcode,categories")
        .not_.is_("barcode", "null")
        .execute()
    ).data or []
    todo = [row for row in rows if not row.get("categories")]
    print(f"{len(todo)} of {len(rows)} products need categories.")

    filled = 0
    for index, row in enumerate(todo, start=1):
        off = await lookup_barcode(row["barcode"], "US")
        if not off.get("found"):
            off = await lookup_barcode(row["barcode"], "WORLD")
        categories = off.get("categories") or []
        primary = off.get("primary_category")
        label = primary or "no categories on Open Food Facts"
        print(f"[{index}/{len(todo)}] {row['name']} ({row['barcode']}): {label}")
        if categories and not dry_run:
            supabase.table("products").update(
                {"categories": categories, "primary_category": primary}
            ).eq("id", row["id"]).execute()
            filled += 1
        await asyncio.sleep(DELAY_SECONDS)

    print(f"Done. Filled {filled} product(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    asyncio.run(main(parser.parse_args().dry_run))
