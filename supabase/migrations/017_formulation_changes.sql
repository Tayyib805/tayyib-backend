-- 017: label spot-checks.
--
-- When someone photographs a label and it no longer matches the stored
-- ingredients, the old formulation is closed (effective_to), a new one is
-- added, and a row here records the change for review: what was added and
-- removed, the verdict before and after, and whether it was applied
-- automatically (only for unreviewed, tier 3 products). Read and written only
-- by the API (row security on, no policies).

create table if not exists public.formulation_changes (
  id uuid primary key default gen_random_uuid(),
  product_id uuid not null references public.products (id) on delete cascade,
  country_code varchar(8) not null default 'US',
  old_formulation_id uuid references public.product_formulations (id) on delete set null,
  new_formulation_id uuid references public.product_formulations (id) on delete set null,
  user_id uuid references auth.users (id) on delete set null,
  added jsonb not null default '[]'::jsonb,
  removed jsonb not null default '[]'::jsonb,
  old_status text check (old_status is null or old_status in ('halal', 'haram', 'uncertain')),
  new_status text check (new_status is null or new_status in ('halal', 'haram', 'uncertain')),
  analysis jsonb,
  applied boolean not null default false,
  status text not null default 'pending' check (status in ('pending', 'confirmed', 'reverted')),
  review_notes text check (review_notes is null or char_length(review_notes) <= 1000),
  created_at timestamptz not null default now(),
  reviewed_at timestamptz
);
create index if not exists formulation_changes_pending_idx on public.formulation_changes (status, created_at desc);
create index if not exists formulation_changes_product_idx on public.formulation_changes (product_id);
alter table public.formulation_changes enable row level security;
revoke all on public.formulation_changes from anon, authenticated;
