-- 013: one profile table, and favourites that follow the account.
--
-- The app wrote to `profiles` and the API read `user_profiles`, so madhab,
-- points and companion settings lived in different places. `user_profiles`
-- (which already guards points, admin and subscription columns) now holds
-- everything; `profiles` becomes a view onto it so existing code keeps
-- working. The old table is kept as profiles_backup_013, unreachable from
-- the app.

alter table public.user_profiles add column if not exists madhab_set boolean not null default false;
alter table public.user_profiles add column if not exists avatar_url text;

-- Deleting an account removes its profile (needed for account deletion).
alter table public.user_profiles drop constraint if exists user_profiles_id_fkey;
alter table public.user_profiles
  add constraint user_profiles_id_fkey foreign key (id) references auth.users (id) on delete cascade;

-- Copy what the app saved. An explicit madhab choice wins.
insert into public.user_profiles (id, display_name, avatar_url, madhab_preference, madhab_set, created_at)
select p.id, p.display_name, p.avatar_url, coalesce(p.madhab, 'general'), coalesce(p.madhab_set, false), p.created_at
from public.profiles p
on conflict (id) do update set
  display_name = coalesce(user_profiles.display_name, excluded.display_name),
  avatar_url = coalesce(user_profiles.avatar_url, excluded.avatar_url),
  madhab_preference = case
    when excluded.madhab_set then excluded.madhab_preference
    else coalesce(user_profiles.madhab_preference, excluded.madhab_preference)
  end,
  madhab_set = user_profiles.madhab_set or excluded.madhab_set;

-- Every account gets a row.
insert into public.user_profiles (id, display_name)
select u.id, coalesce(u.raw_user_meta_data ->> 'display_name', split_part(u.email, '@', 1))
from auth.users u
on conflict (id) do nothing;

update public.user_profiles set madhab_preference = 'general' where madhab_preference is null;
alter table public.user_profiles alter column madhab_preference set default 'general';
alter table public.user_profiles alter column madhab_preference set not null;

-- Keep the old table as a backup, out of reach of clients.
alter table public.profiles rename to profiles_backup_013;
revoke all on public.profiles_backup_013 from anon, authenticated;

-- The old name, as a view. security_invoker keeps user_profiles' row
-- security (own row only) and its guard on privileged columns.
create view public.profiles with (security_invoker = true) as
select id, display_name, avatar_url, madhab_preference as madhab, madhab_set, created_at, updated_at
from public.user_profiles;
revoke all on public.profiles from anon;
grant select, insert, update on public.profiles to authenticated;
grant all on public.profiles to service_role;

-- New accounts get the one row.
create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path to 'public'
as $$
begin
  insert into public.user_profiles (id, display_name)
  values (new.id, coalesce(new.raw_user_meta_data ->> 'display_name', split_part(new.email, '@', 1)))
  on conflict (id) do nothing;
  return new;
end;
$$;

-- Favourites, so saved products follow the account to a new phone.
create table if not exists public.user_favourites (
  user_id uuid not null references auth.users (id) on delete cascade,
  product_id uuid not null references public.products (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (user_id, product_id)
);
alter table public.user_favourites enable row level security;
create policy "Read own favourites" on public.user_favourites
  for select to authenticated using ((select auth.uid()) = user_id);
create policy "Add own favourites" on public.user_favourites
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy "Remove own favourites" on public.user_favourites
  for delete to authenticated using ((select auth.uid()) = user_id);
revoke all on public.user_favourites from anon;
grant select, insert, delete on public.user_favourites to authenticated;
