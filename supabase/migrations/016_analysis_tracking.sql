-- 016: track which products hold a real analysis.
--
-- products.analyzed_at: when the current verdict came from a successful
-- analysis. A failed analysis used to be saved as "uncertain", and products
-- with no flagged ingredients were never reused from the cache. Scans now
-- reuse a product only when analyzed_at is set; products without it are
-- analysed again on their next scan.
--
-- product_formulations.last_verified_at: when someone last confirmed, from a
-- photo of the label, that the stored ingredients still match.

alter table public.products add column if not exists analyzed_at timestamptz;
update public.products p
   set analyzed_at = r.last_run
  from (select product_id, max(created_at) as last_run from public.analysis_runs group by product_id) r
 where r.product_id = p.id and p.analyzed_at is null;

alter table public.product_formulations add column if not exists last_verified_at timestamptz;
