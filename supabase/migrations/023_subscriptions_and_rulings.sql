-- 023: subscription bookkeeping and madhab rulings.
--
-- Subscriptions: RevenueCat tells the API when a subscription starts, renews,
-- changes or ends (POST /webhooks/revenuecat). The API keeps the user's tier
-- and expiry on the profile, and a small log of events so repeats are ignored.
--
-- Madhab rulings: sourced, versioned rulings per ingredient and school. They
-- are entered by an admin with the scholar or body cited; nothing here is
-- generated. A user's verdict is adjusted for their madhab only when a
-- current ruling exists. Changing a ruling supersedes the old row (kept).

alter table public.user_profiles add column if not exists subscription_expires_at timestamptz;
alter table public.user_profiles add column if not exists subscription_store text;

create table if not exists public.subscription_events (
  id text primary key,                      -- RevenueCat's event id
  user_id uuid references auth.users (id) on delete set null,
  type text not null,
  product_id text,
  entitlements text[] not null default '{}',
  store text,
  environment text,
  expires_at timestamptz,
  event_at timestamptz,
  applied_tier text,
  received_at timestamptz not null default now()
);
alter table public.subscription_events enable row level security;

-- Only the server may change tier, expiry, admin flag or points.
create or replace function public.protect_privileged_profile_columns()
returns trigger language plpgsql set search_path = public as $$
begin
  if coalesce(auth.role(), '') in ('anon', 'authenticated') then
    if tg_op = 'INSERT' then
      new.is_admin := false;
      new.subscription_tier := 'free';
      new.subscription_expires_at := null;
      new.subscription_store := null;
      new.points := 0;
    elsif new.is_admin is distinct from old.is_admin
       or new.subscription_tier is distinct from old.subscription_tier
       or new.subscription_expires_at is distinct from old.subscription_expires_at
       or new.subscription_store is distinct from old.subscription_store
       or new.points is distinct from old.points then
      raise exception 'is_admin, subscription fields and points can only be changed by the server';
    end if;
  end if;
  return new;
end;
$$;

create table if not exists public.ingredient_rulings (
  id uuid primary key default gen_random_uuid(),
  ingredient_id uuid not null references public.ingredients (id) on delete cascade,
  madhab text not null check (madhab in ('hanafi', 'shafii', 'maliki', 'hanbali')),
  status text not null check (status in ('halal', 'haram', 'uncertain')),
  reasoning text not null check (char_length(reasoning) between 10 and 2000),
  source text not null check (char_length(source) between 3 and 300),  -- scholar or body, and the work cited
  source_url text check (source_url is null or source_url ~ '^https?://'),
  effective_from timestamptz not null default now(),
  superseded_at timestamptz,
  superseded_by uuid references public.ingredient_rulings (id) on delete set null,
  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now()
);
create unique index if not exists ingredient_rulings_current_idx
  on public.ingredient_rulings (ingredient_id, madhab) where superseded_at is null;
alter table public.ingredient_rulings enable row level security;
