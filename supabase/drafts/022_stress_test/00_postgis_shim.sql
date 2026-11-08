create schema auth;
create table auth.users (id uuid primary key default gen_random_uuid(), email text, created_at timestamptz not null default now());
create type public.geopt as (lng float8, lat float8);
create function public.st_makepoint(x float8, y float8) returns geopt language sql immutable as $$ select row(x, y)::geopt $$;
create function public.st_distance(a geopt, b geopt) returns float8 language sql immutable as $$
  select 2 * 6371000 * asin(sqrt(power(sin(radians((b).lat - (a).lat) / 2), 2) + cos(radians((a).lat)) * cos(radians((b).lat)) * power(sin(radians((b).lng - (a).lng) / 2), 2))) $$;
create function public.st_dwithin(a geopt, b geopt, m float8) returns boolean language sql immutable as $$ select st_distance(a, b) <= m $$;
