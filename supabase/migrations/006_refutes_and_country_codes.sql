alter table products
add column if not exists has_open_challenges boolean default false;

alter table products
add column if not exists challenge_count integer default 0;

alter table product_formulations
add column if not exists country_code text default 'US';

alter table label_scans
add column if not exists country_code text default 'US';

alter table scan_logs
add column if not exists country_code text default 'US';

create table if not exists product_refutes (
  id uuid primary key default gen_random_uuid(),
  product_id uuid references products(id) on delete cascade,
  user_id uuid references auth.users(id) on delete cascade,
  ingredient_name text,
  challenged_status text not null check (challenged_status in ('halal', 'haram', 'uncertain')),
  proposed_status text not null check (proposed_status in ('halal', 'haram', 'uncertain')),
  reasoning text not null check (char_length(reasoning) >= 20),
  evidence_url text,
  refute_type text not null default 'ingredient'
    check (refute_type in ('ingredient', 'ruling', 'manufacturer_confirmed')),
  status text not null default 'open'
    check (status in ('open', 'under_review', 'accepted', 'rejected', 'escalated_to_scholar')),
  support_votes integer not null default 0,
  oppose_votes integer not null default 0,
  review_notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique(product_id, user_id)
);

create table if not exists refute_votes (
  id uuid primary key default gen_random_uuid(),
  refute_id uuid references product_refutes(id) on delete cascade,
  user_id uuid references auth.users(id) on delete cascade,
  vote_type text not null check (vote_type in ('support', 'oppose')),
  created_at timestamptz default now(),
  unique(refute_id, user_id)
);

create index if not exists idx_product_formulations_country
  on product_formulations(product_id, country_code);

create index if not exists idx_product_refutes_product_id
  on product_refutes(product_id);

create index if not exists idx_product_refutes_status
  on product_refutes(status);

create index if not exists idx_product_refutes_support_votes
  on product_refutes(support_votes desc);

create index if not exists idx_refute_votes_refute_id
  on refute_votes(refute_id);

alter table product_refutes enable row level security;
alter table refute_votes enable row level security;

create policy "Product refutes are publicly readable"
  on product_refutes for select
  using (true);

create policy "Refute votes are publicly readable"
  on refute_votes for select
  using (true);

create policy "Authenticated users can insert product refutes"
  on product_refutes for insert
  to authenticated
  with check (auth.uid() = user_id);

create policy "Authenticated users can insert refute votes"
  on refute_votes for insert
  to authenticated
  with check (auth.uid() = user_id);

create policy "Service role can update product refutes"
  on product_refutes for update
  to service_role
  using (true)
  with check (true);

create policy "Service role can update products for refutes"
  on products for update
  to service_role
  using (true)
  with check (true);
