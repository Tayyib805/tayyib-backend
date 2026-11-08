-- 020: scan leaderboard.
--
-- Ranks people by how many different products they scanned in a period
-- (today, this week, this month, all time) in their own time zone, so
-- re-scanning the same item doesn't climb the board. People appear under
-- their display name or an anonymous tag, never their account id, and can
-- leave the board (user_profiles.leaderboard_visible). Called only by the API.

alter table public.user_profiles add column if not exists leaderboard_visible boolean not null default true;
create index if not exists scan_logs_scanned_idx on public.scan_logs (scanned_at);

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
           c.user_id, c.products, p.display_name, p.active_companion
    from counts c join user_profiles p on p.id = c.user_id
    where p.leaderboard_visible or c.user_id = p_user
  )
  select rnk, upper(left(md5(user_id::text), 4)), display_name, active_companion, products, user_id = p_user
  from ranked
  where pos <= greatest(1, least(p_limit, 50)) or user_id = p_user
  order by pos;
$$;

revoke all on function public.scan_leaderboard(text, text, int, uuid) from public, anon, authenticated;
