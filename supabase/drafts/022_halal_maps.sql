-- 022 (DRAFT v2, not applied): Halal Maps data model.
--
-- Design doc: "Halal Maps: data design" (claude.ai artifact CUyYwJP9meC2ZZVsxyzCEr).
-- Stress-tested on Postgres 16 (PostGIS replaced by a distance shim):
-- scenarios and results are in the doc's "Stress test" section.
--
-- Principles
-- - Places come from free open data (OpenStreetMap, Foursquare Open Source
--   Places), certifier lists and shoppers. Nothing is typed in by staff.
-- - Two separate questions, never mixed:
--     Is it there?  -> status, from sources still listing it and closure reports.
--     Can I trust the halal claim? -> tier + confidence, from dated evidence.
--   Old evidence lowers trust; it never removes an open place from the map.
-- - Every halal label is DERIVED from evidence rows by refresh_place().
--   Nobody edits places.halal_menu etc. directly; add or retract evidence.
-- - One person can't swing a place: new accounts count for less, a dispute
--   or closure needs several people, and a later visit outweighs old reports.
-- - Unknown is a real value. A label shows only when evidence supports it.
-- - Replaces public.restaurants (empty), restaurant_reviews (empty),
--   place_reports and hidden_places (empty) once the app is switched over.

-- Supabase puts extensions in the "extensions" schema; functions below set
-- search_path = public, extensions so they find similarity(), unaccent() and PostGIS.
create extension if not exists postgis with schema extensions;
create extension if not exists pg_trgm with schema extensions;
create extension if not exists unaccent with schema extensions;

-- ── Certifiers ──────────────────────────────────────────────────────────────
create table public.certifiers (
  id uuid primary key default gen_random_uuid(),
  name text not null unique check (char_length(name) between 2 and 120),
  website text,
  requires_hand_slaughter boolean,          -- null = not stated
  list_url text,                            -- where its list of places lives
  list_permission boolean not null default false,  -- may we import its list?
  created_at timestamptz not null default now()
);

-- ── Places ──────────────────────────────────────────────────────────────────
create table public.places (
  id uuid primary key default gen_random_uuid(),
  name text not null check (char_length(name) between 1 and 160),
  kind text not null default 'restaurant'
    check (kind in ('restaurant', 'fast_food', 'cafe', 'food_court', 'food_truck', 'bakery', 'grocery', 'butcher', 'prayer_space')),
  location geography(point, 4326) not null,
  address text,
  city text,
  region text,
  country_code text not null default 'US' check (char_length(country_code) = 2),
  phone text,                               -- digits only, for matching
  website text,
  cuisine text[] not null default '{}',
  opening_hours text,                       -- OSM syntax when known
  attrs jsonb not null default '{}',        -- extras (delivery, parking, ...) without new columns

  -- Is it there? Maintained by refresh_place() from sources and reports.
  status text not null default 'active'
    check (status in ('active', 'possibly_closed', 'closed', 'hidden')),

  -- Can I trust it? Derived from place_evidence by refresh_place(). Never written by hand.
  halal_menu text not null default 'unknown' check (halal_menu in ('full', 'partial', 'none', 'unknown')),
  slaughter text not null default 'unknown' check (slaughter in ('hand', 'machine', 'mixed', 'unknown')),
  alcohol text not null default 'unknown' check (alcohol in ('yes', 'no', 'unknown')),
  pork text not null default 'unknown' check (pork in ('yes', 'no', 'unknown')),
  prayer text not null default 'unknown' check (prayer in ('designated', 'on_request', 'none', 'unknown')),
  certifier_id uuid references public.certifiers (id) on delete set null,
  certified_until date,
  confidence smallint not null default 0 check (confidence between 0 and 100),
  tier text not null default 'listed' check (tier in ('certified', 'confirmed', 'listed', 'disputed')),
  disagreeing_people smallint not null default 0,  -- "1 shopper says it isn't halal" note
  closed_reports smallint not null default 0,      -- open reports since the last positive visit
  last_confirmed_at timestamptz,            -- newest supporting evidence from a person
  derived_at timestamptz,

  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index places_location_idx on public.places using gist (location);
create index places_name_trgm_idx on public.places using gin (name extensions.gin_trgm_ops);
create index places_visible_idx on public.places (status) where status in ('active', 'possibly_closed');

-- ── Where each place was seen ───────────────────────────────────────────────
create table public.place_sources (
  place_id uuid not null references public.places (id) on delete cascade,
  source text not null check (source in ('osm', 'fsq', 'certifier', 'user', 'owner')),
  external_id text not null,                -- osm "node/123", fsq id, certifier id + branch, user hash
  scope text not null default '',           -- what one sync run covers: a map cell, a country, a certifier
  data jsonb not null default '{}',         -- latest raw fields we use (small: name, tags, category)
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  missing_runs smallint not null default 0, -- consecutive healthy runs that didn't list it
  missing_since timestamptz,
  primary key (source, external_id)
);
create index place_sources_place_idx on public.place_sources (place_id);
create index place_sources_scope_idx on public.place_sources (source, scope);

-- ── Owner claims: an owner's word counts as an owner's only once verified ───
create table public.place_claims (
  place_id uuid not null references public.places (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  method text not null check (method in ('phone_callback', 'website_email', 'document', 'admin')),
  verified_at timestamptz,
  created_at timestamptz not null default now(),
  primary key (place_id, user_id)
);

-- ── Evidence: the only way a label changes ──────────────────────────────────
-- One current row per (place, question, source, who). Re-asserting the same
-- claim is a plain upsert on that key (PostgREST-compatible: full unique).
create table public.place_evidence (
  id uuid primary key default gen_random_uuid(),
  place_id uuid not null references public.places (id) on delete cascade,
  attribute text not null
    check (attribute in ('halal', 'halal_menu', 'slaughter', 'alcohol', 'pork', 'prayer', 'certified', 'open')),
  value text not null,
  source text not null check (source in ('osm', 'fsq', 'certifier', 'user', 'owner', 'admin', 'manufacturer')),
  source_ref text not null default '',      -- who said it: external id, or a hash of the user id
  user_id uuid references auth.users (id) on delete set null,  -- unlinked when the account is deleted
  certifier_id uuid references public.certifiers (id) on delete cascade,
  note text check (char_length(note) <= 500),
  observed_at timestamptz not null default now(),
  expires_at timestamptz,                   -- a certificate's end date, for example
  retracted_at timestamptz,                 -- admin or the author withdrew it; re-imports don't undo this
  created_at timestamptz not null default now(),
  unique (place_id, attribute, source, source_ref),
  -- Values are checked here so a bad import fails loudly instead of corrupting a label.
  check (case attribute
    when 'halal' then value in ('yes', 'no')
    when 'halal_menu' then value in ('full', 'partial', 'none')
    when 'slaughter' then value in ('hand', 'machine', 'mixed')
    when 'alcohol' then value in ('yes', 'no')
    when 'pork' then value in ('yes', 'no')
    when 'prayer' then value in ('designated', 'on_request', 'none')
    when 'certified' then value in ('yes', 'revoked') and certifier_id is not null
    when 'open' then value in ('yes', 'closed')
  end)
);
create index place_evidence_place_idx on public.place_evidence (place_id) where retracted_at is null;
create index place_evidence_user_idx on public.place_evidence (user_id, created_at);

-- ── Reviews (replaces restaurant_reviews) ───────────────────────────────────
create table public.place_reviews (
  id uuid primary key default gen_random_uuid(),
  place_id uuid not null references public.places (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  rating smallint not null check (rating between 1 and 5),
  body text check (char_length(body) <= 1000),
  created_at timestamptz not null default now(),
  unique (place_id, user_id)
);

-- ── Possible duplicates the matcher wasn't sure about ───────────────────────
create table public.place_duplicates (
  place_a uuid not null references public.places (id) on delete cascade,
  place_b uuid not null references public.places (id) on delete cascade,
  score real not null,
  decided text check (decided in ('merged', 'different')),
  created_at timestamptz not null default now(),
  primary key (place_a, place_b),
  check (place_a < place_b)
);

-- ── Sync bookkeeping ────────────────────────────────────────────────────────
create table public.map_cells (
  cell text primary key,                    -- geohash, 5 characters (about 5 km)
  last_viewed_at timestamptz not null default now(),
  last_synced_at timestamptz,
  place_count integer not null default 0
);

create table public.sync_runs (
  id uuid primary key default gen_random_uuid(),
  source text not null check (source in ('osm', 'fsq', 'certifier')),
  scope text not null default '',
  status text not null default 'running' check (status in ('running', 'ok', 'suspect', 'failed')),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  seen integer not null default 0,
  added integer not null default 0,
  updated integer not null default 0,
  went_missing integer not null default 0,
  error text
);

-- ── Trust ───────────────────────────────────────────────────────────────────
-- How much one person's word counts. New accounts count for less, so a
-- burst of throwaway accounts can't swing a place. Extend with flags later.
create or replace function public.contributor_trust(uid uuid)
returns numeric language sql stable set search_path = public, extensions as $$
  select case
    when uid is null then 1                 -- anonymised (account deleted): keep its weight
    when (select created_at from auth.users where id = uid) > now() - interval '14 days' then 0.3
    else 1 end
$$;

-- Weight of one evidence row: source strength × age decay × trust.
-- Person evidence halves every year; dataset evidence every three years
-- (it is re-asserted on every sync that still lists the place).
create or replace function public.evidence_weight(src text, observed timestamptz, expires timestamptz)
returns numeric language sql stable set search_path = public as $$
  select case
    when expires is not null and expires < now() then 0
    else (case src
            when 'certifier' then 50 when 'admin' then 45 when 'manufacturer' then 40
            when 'owner' then 25 when 'user' then 10 when 'osm' then 12 when 'fsq' then 8 else 0 end)
         * power(0.5, extract(epoch from now() - observed) / 86400.0
                 / (case when src in ('osm', 'fsq', 'certifier') then 1095 else 365 end))
  end
$$;

-- ── Deriving labels and status ──────────────────────────────────────────────
create or replace function public.refresh_place(p uuid)
returns void language plpgsql security definer set search_path = public, extensions as $$
declare
  sup numeric; con numeric; supporters int; disagreeing int; contra_authority boolean;
  last_positive timestamptz; reports int; dataset_sources int; dataset_missing int;
begin
  -- Effective rows: unretracted, with an unverified "owner" demoted to a shopper.
  if to_regclass('pg_temp._ev') is null then
    create temporary table _ev (attribute text, value text, src text, who text, observed timestamptz, w numeric) on commit drop;
  else
    truncate _ev;
  end if;
  insert into _ev
  select e.attribute, e.value, s.src, e.source_ref, e.observed_at,
         evidence_weight(s.src, e.observed_at, e.expires_at)
           * case when s.src in ('user', 'owner') then contributor_trust(e.user_id) else 1 end
  from place_evidence e
  cross join lateral (select case
      when e.source = 'owner' and not exists (select 1 from place_claims c where c.place_id = e.place_id
                                              and c.user_id = e.user_id and c.verified_at is not null) then 'user'
      else e.source end as src) s
  where e.place_id = p and e.retracted_at is null;

  select coalesce(sum(w) filter (where (attribute = 'halal' and value = 'yes') or (attribute = 'certified' and value = 'yes')), 0),
         coalesce(sum(w) filter (where (attribute = 'halal' and value = 'no') or (attribute = 'certified' and value = 'revoked')), 0),
         count(distinct who) filter (where src in ('user', 'owner') and attribute = 'halal' and value = 'yes' and observed > now() - interval '12 months'),
         count(distinct who) filter (where src in ('user', 'owner') and attribute = 'halal' and value = 'no' and observed > now() - interval '12 months'),
         bool_or((src in ('admin', 'certifier', 'manufacturer')) and ((attribute = 'halal' and value = 'no') or (attribute = 'certified' and value = 'revoked'))),
         max(observed) filter (where src in ('user', 'owner', 'admin') and ((attribute = 'open' and value = 'yes') or (attribute = 'halal' and value = 'yes')))
    into sup, con, supporters, disagreeing, contra_authority, last_positive
  from _ev;

  -- Closure reports only count if made after the last time a person said it was open (or halal).
  select count(distinct who) into reports from _ev
  where attribute = 'open' and value = 'closed' and observed > now() - interval '90 days'
    and (last_positive is null or observed > last_positive);

  select count(*), count(*) filter (where missing_runs >= 2)
    into dataset_sources, dataset_missing
  from place_sources where place_id = p and source in ('osm', 'fsq', 'certifier');

  update places set
    halal_menu = case when con > sup then 'none' else coalesce((select value from _ev where attribute = 'halal_menu'
                   group by value having sum(w) > 0 order by sum(w) desc limit 1), 'unknown') end,
    slaughter  = coalesce((select value from _ev where attribute = 'slaughter' group by value having sum(w) > 0 order by sum(w) desc limit 1), 'unknown'),
    alcohol    = coalesce((select value from _ev where attribute = 'alcohol' group by value having sum(w) > 0 order by sum(w) desc limit 1), 'unknown'),
    pork       = coalesce((select value from _ev where attribute = 'pork' group by value having sum(w) > 0 order by sum(w) desc limit 1), 'unknown'),
    prayer     = coalesce((select value from _ev where attribute = 'prayer' group by value having sum(w) > 0 order by sum(w) desc limit 1), 'unknown'),
    certifier_id = (select e.certifier_id from place_evidence e where e.place_id = p and e.retracted_at is null and e.attribute = 'certified'
                    and e.value = 'yes' and (e.expires_at is null or e.expires_at > now()) order by e.observed_at desc limit 1),
    certified_until = (select e.expires_at::date from place_evidence e where e.place_id = p and e.retracted_at is null and e.attribute = 'certified'
                       and e.value = 'yes' and (e.expires_at is null or e.expires_at > now()) order by e.observed_at desc limit 1),
    confidence = least(100, greatest(0, round(sup - con)))::smallint,
    disagreeing_people = disagreeing,
    closed_reports = reports,
    tier = case
      -- A dispute needs real weight against: an authority, or two or more people outweighing half the support.
      when (contra_authority or (disagreeing >= 2 and con >= 15)) and con >= sup * 0.5 then 'disputed'
      when exists (select 1 from place_evidence e where e.place_id = p and e.retracted_at is null and e.attribute = 'certified'
                   and e.value = 'yes' and (e.expires_at is null or e.expires_at > now())) then 'certified'
      when supporters >= 2 then 'confirmed'
      else 'listed' end,
    status = case
      when status = 'hidden' then 'hidden'
      when exists (select 1 from _ev where src = 'admin' and attribute = 'open' and value = 'closed') then 'closed'
      when reports >= 3 or (reports >= 1 and dataset_sources > 0 and dataset_missing = dataset_sources) then 'closed'
      when reports >= 2 or (dataset_sources > 0 and dataset_missing = dataset_sources) then 'possibly_closed'
      else 'active' end,
    last_confirmed_at = (select max(observed) from _ev where src in ('user', 'owner', 'admin', 'certifier', 'manufacturer')
                         and value not in ('no', 'closed', 'revoked')),
    derived_at = now(),
    updated_at = now()
  where id = p;
end $$;

-- Statement-level: a bulk import touching many rows refreshes each place once.
create or replace function public.place_evidence_changed()
returns trigger language plpgsql security definer set search_path = public, extensions as $$
begin
  if tg_op = 'INSERT' then
    perform refresh_place(x) from (select distinct place_id as x from changed_new) d;
  elsif tg_op = 'UPDATE' then
    perform refresh_place(x) from (select place_id as x from changed_new union select place_id from changed_old) d;
  else
    perform refresh_place(x) from (select distinct place_id as x from changed_old
                                   where place_id in (select id from places)) d;
  end if;
  return null;
end $$;

create trigger place_evidence_refresh_ins after insert on public.place_evidence
  referencing new table as changed_new for each statement execute function public.place_evidence_changed();
create trigger place_evidence_refresh_upd after update on public.place_evidence
  referencing new table as changed_new old table as changed_old for each statement execute function public.place_evidence_changed();
create trigger place_evidence_refresh_del after delete on public.place_evidence
  referencing old table as changed_old for each statement execute function public.place_evidence_changed();

-- Imports: send evidence in chunks of about 1,000 rows per request (each place
-- refresh takes about 1 ms), so no request runs into the API's statement timeout.

-- Weekly (pg_cron): decay changes confidence even with no new evidence.
--   select refresh_place(id) from places where derived_at < now() - interval '7 days';

-- ── Sync safety: a broken or partial feed never closes places ──────────────
-- Call when a run has upserted everything it saw (last_seen_at >= started_at).
-- Records not seen are counted; if too many went missing at once, the run is
-- marked suspect and nothing is changed. A place needs two healthy runs in a
-- row without it before it shows as possibly closed.
create or replace function public.finish_sync_run(run uuid, max_missing_share numeric default 0.10)
returns text language plpgsql security definer set search_path = public, extensions as $$
declare r sync_runs; total int; gone int; touched uuid[];
begin
  select * into r from sync_runs where id = run for update;
  select count(*), count(*) filter (where last_seen_at < r.started_at)
    into total, gone from place_sources where source = r.source and scope = r.scope;
  if total > 20 and gone > total * max_missing_share then
    update sync_runs set status = 'suspect', finished_at = now(), went_missing = gone where id = run;
    return 'suspect';
  end if;
  update place_sources set missing_runs = 0, missing_since = null
    where source = r.source and scope = r.scope and last_seen_at >= r.started_at and missing_runs > 0;
  with m as (
    update place_sources set missing_runs = missing_runs + 1, missing_since = coalesce(missing_since, now())
    where source = r.source and scope = r.scope and last_seen_at < r.started_at
    returning place_id)
  select array_agg(distinct place_id) into touched from m;
  perform refresh_place(x) from unnest(coalesce(touched, '{}')) x;
  update sync_runs set status = 'ok', finished_at = now(), went_missing = gone where id = run;
  return 'ok';
end $$;

-- ── Finding duplicates ──────────────────────────────────────────────────────
-- Compare names without accents, punctuation and filler words.
create or replace function public.place_name_key(n text)
returns text language sql immutable set search_path = public, extensions as $$
  select coalesce(nullif(trim(regexp_replace(
           regexp_replace(lower(unaccent('unaccent', n)), '[^a-z0-9 ]+', ' ', 'g'),
           '\m(the|restaurant|restaurants|grill|grille|cafe|kitchen|halal|food|foods|and|bar|house of|llc|inc|co)\M', ' ', 'g')), ''),
         lower(n))
$$;

-- Same place if: same phone or website within 300 m, or a similar name within 150 m.
-- Unsure (similar-ish name close by) -> returns null and the caller records a possible duplicate.
create or replace function public.match_place(p_name text, p_lat double precision, p_lng double precision,
                                              p_phone text default null, p_website text default null)
returns uuid language sql stable set search_path = public, extensions as $$
  select id from places
  where st_dwithin(location, st_makepoint(p_lng, p_lat)::geography, 300)
    and (
      (p_phone is not null and phone = p_phone)
      or (p_website is not null and website is not null
          and lower(regexp_replace(website, '^https?://(www\.)?([^/]+).*$', '\2')) = lower(regexp_replace(p_website, '^https?://(www\.)?([^/]+).*$', '\2')))
      or (st_dwithin(location, st_makepoint(p_lng, p_lat)::geography, 150)
          and similarity(place_name_key(name), place_name_key(p_name)) >= 0.5)
    )
  order by similarity(place_name_key(name), place_name_key(p_name)) desc,
           st_distance(location, st_makepoint(p_lng, p_lat)::geography)
  limit 1
$$;

create or replace function public.near_duplicates(p_name text, p_lat double precision, p_lng double precision)
returns table (place_id uuid, score real) language sql stable set search_path = public, extensions as $$
  select id, similarity(place_name_key(name), place_name_key(p_name)) from places
  where st_dwithin(location, st_makepoint(p_lng, p_lat)::geography, 150)
    and similarity(place_name_key(name), place_name_key(p_name)) between 0.3 and 0.5
$$;

-- Merge two places that turned out to be one (admin, one tap).
create or replace function public.merge_places(keep uuid, drop_id uuid)
returns void language plpgsql security definer set search_path = public, extensions as $$
begin
  delete from place_evidence d using place_evidence k
    where d.place_id = drop_id and k.place_id = keep
      and (d.attribute, d.source, d.source_ref) = (k.attribute, k.source, k.source_ref)
      and d.observed_at <= k.observed_at;
  delete from place_evidence k using place_evidence d
    where d.place_id = drop_id and k.place_id = keep
      and (d.attribute, d.source, d.source_ref) = (k.attribute, k.source, k.source_ref);
  update place_evidence set place_id = keep where place_id = drop_id;
  update place_sources set place_id = keep where place_id = drop_id;
  delete from place_reviews d using place_reviews k where d.place_id = drop_id and k.place_id = keep and d.user_id = k.user_id;
  update place_reviews set place_id = keep where place_id = drop_id;
  update place_duplicates set decided = 'merged' where (place_a, place_b) in ((least(keep, drop_id), greatest(keep, drop_id)));
  delete from places where id = drop_id;
  perform refresh_place(keep);
end $$;

-- ── Map query ───────────────────────────────────────────────────────────────
-- Every open place that claims halal is shown; trust is shown, not used to hide.
create or replace function public.places_near(p_lat double precision, p_lng double precision, p_radius_m integer default 8000, p_limit integer default 200)
returns setof public.places language sql stable set search_path = public, extensions as $$
  select * from places
  where status in ('active', 'possibly_closed')
    and st_dwithin(location, st_makepoint(p_lng, p_lat)::geography, least(p_radius_m, 40000))
  order by (tier = 'certified') desc, (tier = 'confirmed') desc, (tier = 'disputed') asc, confidence desc,
           st_distance(location, st_makepoint(p_lng, p_lat)::geography)
  limit least(p_limit, 500)
$$;

-- ── Admin queue: only what needs a person ───────────────────────────────────
create or replace view public.places_needing_review as
  select p.id, p.name, 'disputed' as reason, p.updated_at from public.places p where p.tier = 'disputed' and p.status <> 'hidden'
  union all
  select p.id, p.name, 'new_from_shopper', p.created_at from public.places p
  where p.status = 'active' and p.tier = 'listed'
    and not exists (select 1 from public.place_sources s where s.place_id = p.id and s.source in ('osm', 'fsq', 'certifier'))
    and p.created_at < now() - interval '14 days'
  union all
  select d.place_a, 'possible duplicate', 'duplicate', d.created_at from public.place_duplicates d where d.decided is null
  union all
  select null, r.source || ' ' || r.scope, 'suspect_sync', r.started_at from public.sync_runs r where r.status = 'suspect';

-- ── Access ──────────────────────────────────────────────────────────────────
alter table public.places enable row level security;
alter table public.place_sources enable row level security;
alter table public.place_evidence enable row level security;
alter table public.place_claims enable row level security;
alter table public.place_reviews enable row level security;
alter table public.place_duplicates enable row level security;
alter table public.certifiers enable row level security;
alter table public.map_cells enable row level security;
alter table public.sync_runs enable row level security;

create policy "Places are public" on public.places for select using (status <> 'hidden');
create policy "Certifiers are public" on public.certifiers for select using (true);
create policy "Reviews are public" on public.place_reviews for select using (true);
-- Evidence, sources, claims, duplicates, cells and runs go through the API (service role) only.
