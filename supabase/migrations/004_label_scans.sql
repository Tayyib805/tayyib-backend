alter table products
add column if not exists data_source text default 'open_food_facts'
  check (data_source in ('open_food_facts', 'label_scan', 'admin_entry', 'user_submission'));

alter table products
add column if not exists ingredients_text text;

alter table products
add column if not exists raw_ingredients_image_url text;

create table if not exists label_scans (
  id uuid primary key default gen_random_uuid(),
  product_id uuid references products(id) on delete cascade,
  user_id uuid references auth.users(id),
  image_url text,
  extracted_ingredients text not null,
  extraction_confidence text check (extraction_confidence in ('high', 'medium', 'low')),
  verified boolean default false,
  created_at timestamptz default now()
);

create index if not exists idx_label_scans_product_id on label_scans(product_id);
create index if not exists idx_label_scans_verified on label_scans(verified);

alter table label_scans enable row level security;

create policy "Public read label_scans"
  on label_scans for select
  using (true);

create policy "Auth insert label_scans"
  on label_scans for insert
  to authenticated
  with check (auth.uid() is not null);
