-- Migration 008: security hardening (applied to production 2026-11-06)
--
-- The publishable (anon) key ships inside the frontend, so anyone can call
-- Supabase directly and skip the FastAPI backend. Row-level security is the
-- only boundary for that traffic. The backend uses the service-role key,
-- which bypasses RLS, so none of these changes affect the API.

-- 1. Users cannot grant themselves admin, a paid tier, or points.
create or replace function public.protect_privileged_profile_columns()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if coalesce(auth.role(), '') in ('anon', 'authenticated') then
    if tg_op = 'INSERT' then
      new.is_admin := false;
      new.subscription_tier := 'free';
      new.points := 0;
    elsif new.is_admin is distinct from old.is_admin
       or new.subscription_tier is distinct from old.subscription_tier
       or new.points is distinct from old.points then
      raise exception 'is_admin, subscription_tier and points can only be changed by the server';
    end if;
  end if;
  return new;
end;
$$;

drop trigger if exists protect_privileged_profile_columns on public.user_profiles;
create trigger protect_privileged_profile_columns
  before insert or update on public.user_profiles
  for each row execute function public.protect_privileged_profile_columns();

-- 2. Products and manufacturers are created by the backend only.
drop policy if exists "Authenticated users can insert products" on public.products;
drop policy if exists "Authenticated users can insert manufacturers" on public.manufacturers;

-- 3. Contact emails, inquiry drafts and responses are not public.
drop policy if exists "Manufacturers are publicly readable" on public.manufacturers;
drop policy if exists "Manufacturer inquiries are publicly readable" on public.manufacturer_inquiries;

-- 4. Label scans: private to the scanner, inserted by the backend only.
drop policy if exists "Public read label_scans" on public.label_scans;
drop policy if exists "Auth insert label_scans" on public.label_scans;
drop policy if exists "Users can read own label scans" on public.label_scans;
create policy "Users can read own label scans"
  on public.label_scans for select to authenticated
  using (auth.uid() = user_id);

-- 5. Refutes and votes are written by the backend only. Direct inserts let a
--    user post as someone else or pre-load vote counts to force a re-analysis.
--    (Production used the policy names from the chat-applied version of 006.)
drop policy if exists "Auth insert refutes" on public.product_refutes;
drop policy if exists "Authenticated users can insert product refutes" on public.product_refutes;
drop policy if exists "Auth insert refute votes" on public.refute_votes;
drop policy if exists "Authenticated users can insert refute votes" on public.refute_votes;
drop policy if exists "Public read refute votes" on public.refute_votes;
drop policy if exists "Refute votes are publicly readable" on public.refute_votes;
drop policy if exists "Users can read own refute votes" on public.refute_votes;
create policy "Users can read own refute votes"
  on public.refute_votes for select to authenticated
  using (auth.uid() = user_id);

-- 6. Trigger functions should not be callable through /rest/v1/rpc.
revoke execute on function public.handle_new_user() from public, anon, authenticated;
revoke execute on function public.protect_privileged_profile_columns() from public, anon, authenticated;
