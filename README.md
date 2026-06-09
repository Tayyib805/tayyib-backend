# Tayyib Backend

FastAPI backend for Tayyib, a halal food intelligence platform.

## Setup

1. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in your Supabase and Anthropic values.

3. Run `supabase/migrations/001_initial_schema.sql` in the Supabase SQL editor.

4. Run `supabase/seed.sql` in the Supabase SQL editor.

5. Start the API:

   ```bash
   uvicorn app.main:app --reload
   ```

## Endpoints

- `GET /health` checks service health.
- `GET /` returns API info.
- `GET /products/scan/{barcode}` scans or creates a product from Open Food Facts and Claude analysis.
- `GET /products/{id}` returns a product with linked ingredients.
- `POST /products/{id}/vote` adds an authenticated tier-elevation vote.
- `GET /ingredients` lists known ingredients.
- `GET /restaurants/nearby` returns nearby restaurants using a bounding-box query and calculated distance.
- `GET /users/me` returns the authenticated profile.
- `GET /users/me/scans` returns scan history.
- `POST /scans` records a scan log.

Authenticated endpoints expect:

```http
Authorization: Bearer <supabase-user-jwt>
```

## Scrapers

Install the scraper browser dependency after installing Python packages:

```bash
python -m playwright install chromium
```

Apply `supabase/migrations/005_manufacturer_scrape_tables.sql` before saving scraped data.

Usage:

```bash
cd scrapers
python manufacturer_scraper.py --brand fritolay --dry-run
python seed_from_scrape.py --brand fritolay --limit 50
```

`--dry-run` prints results without saving to Supabase. `--limit N` stops after N products for testing.
