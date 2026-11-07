-- Migration 008: security hardening
--
-- The publishable (anon) key ships inside the frontend, so anyone can call
-- Supabase directly and skip the FastAPI backend. Row-level security is the
-- only thing standing between them and the data. This migration closes the
-- holes found in the security audit. The backend uses the service-role key,
-- which bypasses RLS, so none of these changes affect the API.

-- 1. CRITICAL: users could make themselves admin, grant themselves Pro, or
--    set their own points by updating their own user_profiles row.
create or replace function public.protect_privileged_profile_columns()
returns trigger
language plpgsql
as $$
begin
  -- Only signed-in app users (JWT role anon/authenticated) are restricted.
  -- The backend (service_role) and the SQL editor are unaffected.
  if coalesce(auth.role(), '') in ('anon', 'authenticated') then
    if tg_op = 'INSERT' then
      new.is_admin := false;
      new.subscription_tier := 'free';
      new.points := 0;
    else
      if new.is_admin is distinct from old.is_admin
        or new.subscription_tier is distinct from old.subscription_tier
        or new.points is distinct from old.points then
        raise exception 'is_admin, subscription_tier and points can only be changed by the server';
      end if;
    end if;
  end if;
  return new;
end;
$$;

drop trigger if exists protect_privileged_profile_columns on public.user_profiles;
create trigger protect_privileged_profile_columns
  before insert or update on public.user_profiles
  for each row execute function public.protect_privileged_profile_columns();

-- 2. HIGH: any signed-in user could insert products directly, including ones
--    marked halal and "Scholar Verified". Products are created by the backend.
drop policy if exists "Authenticated users can insert products" on products;

-- 3. HIGH: any signed-in user could create manufacturer records with their
--    own contact email, redirecting inquiry emails to themselves.
drop policy if exists "Authenticated users can insert manufacturers" on manufacturers;

-- 4. MEDIUM: manufacturer contact emails, inquiry drafts and responses were
--    publicly readable. Serve a curated public feed from the backend instead.
drop policy if exists "Manufacturers are publicly readable" on manufacturers;
drop policy if exists "Manufacturer inquiries are publicly readable" on manufacturer_inquiries;

-- 5. MEDIUM: label scans were publicly readable (exposing who scanned what)
--    and any signed-in user could insert them on behalf of anyone.
drop policy if exists "Public read label_scans" on label_scans;
drop policy if exists "Auth insert label_scans" on label_scans;
create policy "Users can read own label scans"
  on label_scans for select
  to authenticated
  using (auth.uid() = user_id);

-- 6. LOW: every user's individual refute votes were publicly readable.
drop policy if exists "Refute votes are publicly readable" on refute_votes;
create policy "Users can read own refute votes"
  on refute_votes for select
  to authenticated
  using (auth.uid() = user_id);
