-- 009: fix scan logging and add product categories for halal alternatives.
--
-- scan_logs: the API has been writing country_code (and the app sends a
-- barcode and scan method), but the table never had those columns, so every
-- scan log insert failed. Unknown products also need a row without a
-- product_id so they still appear in a user's history.
alter table scan_logs add column if not exists barcode text;
alter table scan_logs add column if not exists country_code varchar(2);
alter table scan_logs add column if not exists scan_method text;
alter table scan_logs alter column product_id drop not null;

create index if not exists scan_logs_user_scanned_idx
  on scan_logs (user_id, scanned_at desc);

-- products: Open Food Facts category tags (e.g. en:gummies), most specific
-- last, plus the single category used to compare like with like.
alter table products add column if not exists categories text[] not null default '{}';
alter table products add column if not exists primary_category text;

create index if not exists products_categories_gin
  on products using gin (categories);
create index if not exists products_status_tier_idx
  on products (overall_status, verification_tier);
