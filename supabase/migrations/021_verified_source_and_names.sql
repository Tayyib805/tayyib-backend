-- 021: who verified a tier 1 verdict; public names only when chosen.
--
-- Tier 1 means the verdict was settled by someone who knows: a scholar, an
-- admin review, or the manufacturer's own written answer. The app labels
-- them differently ("Confirmed by the maker" vs "Scholar verified").
--
-- New accounts got the part of their email before the @ as a display name,
-- and the leaderboard showed it. A name is now public only once the person
-- chose it (in Profile, at sign-up or on a comment); everyone else shows as
-- "Shopper ABCD".

alter table public.products add column if not exists verified_source text
  check (verified_source is null or verified_source in ('manufacturer', 'scholar', 'admin'));

alter table public.user_profiles add column if not exists name_chosen boolean not null default false;

update public.user_profiles up
set name_chosen = true
from auth.users u
where u.id = up.id
  and up.display_name is not null
  and up.display_name not like '%@%'
  and lower(up.display_name) <> lower(split_part(u.email, '@', 1));

create or replace function public.handle_new_user()
returns trigger
language plpgsql security definer set search_path = public
as $$
begin
  insert into public.user_profiles (id, display_name, name_chosen)
  values (
    new.id,
    coalesce(new.raw_user_meta_data ->> 'display_name', split_part(new.email, '@', 1)),
    (new.raw_user_meta_data ->> 'display_name') is not null
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create or replace function public.scan_leaderboard(p_period text, p_tz text default 'UTC', p_limit int default 20, p_user uuid default null)
returns table (rank bigint, tag text, display_name text, active_companion text, products bigint, is_me boolean)
language sql stable security definer set search_path = public
as $$
  with tz as (
    select coalesce((select name from pg_timezone_names where name = p_tz limit 1), 'UTC') as name
  ),
  bounds as (
    select case p_period
      when 'day'   then date_trunc('day',   now() at time zone tz.name) at time zone tz.name
      when 'week'  then date_trunc('week',  now() at time zone tz.name) at time zone tz.name
      when 'month' then date_trunc('month', now() at time zone tz.name) at time zone tz.name
      else '-infinity'::timestamptz end as since
    from tz
  ),
  counts as (
    select s.user_id, count(distinct s.product_id) as products, max(s.scanned_at) as last_scan
    from scan_logs s, bounds b
    where s.user_id is not null and s.product_id is not null and s.scanned_at >= b.since
    group by s.user_id
  ),
  ranked as (
    select rank() over (order by c.products desc) as rnk,
           row_number() over (order by c.products desc, c.last_scan asc) as pos,
           c.user_id, c.products, case when p.name_chosen then p.display_name end as display_name, p.active_companion
    from counts c join user_profiles p on p.id = c.user_id
    where p.leaderboard_visible or c.user_id = p_user
  )
  select rnk, upper(left(md5(user_id::text), 4)), display_name, active_companion, products, user_id = p_user
  from ranked
  where pos <= greatest(1, least(p_limit, 50)) or user_id = p_user
  order by pos;
$$;

revoke all on function public.scan_leaderboard(text, text, int, uuid) from public, anon, authenticated;
