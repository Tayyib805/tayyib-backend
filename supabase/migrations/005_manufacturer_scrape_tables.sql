alter table products
add column if not exists data_source text default 'open_food_facts';

alter table products
drop constraint if exists products_data_source_check;

alter table products
add constraint products_data_source_check
  check (
    data_source in (
      'open_food_facts',
      'label_scan',
      'admin_entry',
      'user_submission',
      'manufacturer_scrape'
    )
  );

create table if not exists product_formulations (
  id uuid primary key default gen_random_uuid(),
  product_id uuid references products(id) on delete cascade,
  ingredients_text text not null,
  allergens text,
  certifications text[] default '{}',
  source_url text,
  effective_from timestamptz default now(),
  effective_to timestamptz,
  data_source text default 'manufacturer_scrape'
    check (
      data_source in (
        'open_food_facts',
        'label_scan',
        'admin_entry',
        'user_submission',
        'manufacturer_scrape'
      )
    ),
  created_at timestamptz default now()
);

create table if not exists analysis_runs (
  id uuid primary key default gen_random_uuid(),
  product_id uuid references products(id) on delete cascade,
  product_formulation_id uuid references product_formulations(id) on delete set null,
  analysis_type text default 'ingredients_text',
  input_text text,
  result jsonb not null default '{}',
  overall_status text default 'uncertain'
    check (overall_status in ('halal', 'haram', 'uncertain')),
  confidence_level text default 'low'
    check (confidence_level in ('high', 'medium', 'low')),
  data_source text default 'manufacturer_scrape'
    check (
      data_source in (
        'open_food_facts',
        'label_scan',
        'admin_entry',
        'user_submission',
        'manufacturer_scrape'
      )
    ),
  created_at timestamptz default now()
);

create index if not exists idx_product_formulations_product_id
  on product_formulations(product_id);

create index if not exists idx_product_formulations_effective_from
  on product_formulations(effective_from);

create index if not exists idx_analysis_runs_product_id
  on analysis_runs(product_id);

create index if not exists idx_analysis_runs_product_formulation_id
  on analysis_runs(product_formulation_id);

alter table product_formulations enable row level security;
alter table analysis_runs enable row level security;

create policy "Product formulations are publicly readable"
  on product_formulations for select
  using (true);

create policy "Analysis runs are publicly readable"
  on analysis_runs for select
  using (true);

create policy "Service role can insert product formulations"
  on product_formulations for insert
  to service_role
  with check (true);

create policy "Service role can insert analysis runs"
  on analysis_runs for insert
  to service_role
  with check (true);

create policy "Service role can update analysis runs"
  on analysis_runs for update
  to service_role
  using (true)
  with check (true);
