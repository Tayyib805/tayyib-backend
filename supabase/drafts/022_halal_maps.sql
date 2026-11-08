-- 022 (DRAFT, not applied): Halal Maps data model.
--
-- Design doc: "Halal Maps: data design" (claude.ai artifact CUyYwJP9meC2ZZVsxyzCEr).
--
-- Principles
-- - Places come from free open data (OpenStreetMap, Foursquare Open Source
--   Places), certifier lists and shoppers. Nothing is typed in by staff.
-- - Every halal label is DERIVED from dated evidence rows by refresh_place().
--   Nobody edits places.halal_menu etc. directly; add or retract evidence.
-- - Evidence ages: older evidence counts for less, so stale places fade.
-- - Unknown is a real value. A label shows only when evidence supports it.
-- - Replaces public.restaurants (empty), restaurant_reviews (empty),
--   place_reports and hidden_places (empty) once the app is switched over.

create extension if not exists postgis;
create extension if not exists pg_trgm;

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
  phone text,
  website text,
  cuisine text[] not null default '{}',
  opening_hours text,                       -- OSM syntax when known
  attrs jsonb not null default '{}',        -- extras (delivery, parking, ...) without new columns

  -- Lifecycle, maintained by sync jobs, reports and refresh_place().
  status text not null default 'active'
    check (status in ('active', 'possibly_closed', 'closed', 'hidden')),

  -- Derived from place_evidence by refresh_place(). Never written by hand.
  halal_menu text not null default 'unknown' check (halal_menu in ('full', 'partial', 'none', 'unknown')),
  slaughter text not null default 'unknown' check (slaughter in ('hand', 'machine', 'mixed', 'unknown')),
  alcohol text not null default 'unknown' check (alcohol in ('yes', 'no', 'unknown')),
  pork text not null default 'unknown' check (pork in ('yes', 'no', 'unknown')),
  prayer text not null default 'unknown' check (prayer in ('designated', 'on_request', 'none', 'unknown')),
  certifier_id uuid references public.certifiers (id) on delete set null,
  certified_until date,
  confidence smallint not null default 0 check (confidence between 0 and 100),
  tier text not null default 'listed' check (tier in ('certified', 'confirmed', 'listed', 'disputed')),
  last_confirmed_at timestamptz,            -- newest supporting evidence from a person
  derived_at timestamptz,

  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index places_location_idx on public.places using gist (location);
create index places_name_trgm_idx on public.places using gin (name gin_trgm_ops);
create index places_visible_idx on public.places (status, tier) where status in ('active', 'possibly_closed');

-- ── Where each place was seen ───────────────────────────────────────────────
-- One row per place per outside record. Lets every sync upsert by
-- (source, external_id) and notice when a record disappears.
create table public.place_sources (
  place_id uuid not null references public.places (id) on delete cascade,
  source text not null check (source in ('osm', 'fsq', 'certifier', 'user', 'owner')),
  external_id text not null,                -- osm "node/123", fsq id, certifier id + branch, user id
  data jsonb not null default '{}',         -- latest raw fields we use (small: name, tags, category)
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  missing_since timestamptz,                -- set when a sync no longer finds it
  primary key (source, external_id)
);
create index place_sources_place_idx on public.place_sources (place_id);

-- ── Evidence: the only way a label changes ──────────────────────────────────
-- Append-mostly. A person's latest answer to a question replaces their
-- earlier one (unique index below); sources re-assert on every sync.
create table public.place_evidence (
  id uuid primary key default gen_random_uuid(),
  place_id uuid not null references public.places (id) on delete cascade,
  attribute text not null
    check (attribute in ('halal', 'halal_menu', 'slaughter', 'alcohol', 'pork', 'prayer', 'certified', 'open')),
  value text not null check (char_length(value) between 1 and 64),
    -- halal: yes|no · halal_menu: full|partial|none · slaughter: hand|machine|mixed
    -- alcohol/pork: yes|no · prayer: designated|on_request|none
    -- certified: <certifier uuid> | revoked · open: yes|closed
  source text not null check (source in ('osm', 'fsq', 'certifier', 'user', 'owner', 'admin', 'manufacturer')),
  source_ref text not null default '',      -- user id, osm id, certifier id ... (who said it)
  note text check (char_length(note) <= 500),
  observed_at timestamptz not null default now(),
  expires_at timestamptz,                   -- a certificate's end date, for example
  retracted_at timestamptz,                 -- admin or the author withdrew it
  created_at timestamptz not null default now()
);
create unique index place_evidence_one_voice_idx
  on public.place_evidence (place_id, attribute, source, source_ref)
  where retracted_at is null;
create index place_evidence_place_idx on public.place_evidence (place_id) where retracted_at is null;

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

-- ── Sync bookkeeping ────────────────────────────────────────────────────────
-- Areas people actually look at get refreshed from OSM; nothing else does.
create table public.map_cells (
  cell text primary key,                    -- geohash, 5 characters (about 5 km)
  last_viewed_at timestamptz not null default now(),
  last_synced_at timestamptz,
  place_count integer not null default 0
);

create table public.sync_runs (
  id uuid primary key default gen_random_uuid(),
  source text not null check (source in ('osm', 'fsq', 'certifier')),
  scope text not null default '',           -- a cell, a country, a certifier
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  seen integer not null default 0,
  added integer not null default 0,
  updated integer not null default 0,
  went_missing integer not null default 0,
  error text
);

-- ── Deriving labels ─────────────────────────────────────────────────────────
-- Weight of one evidence row: source strength × age decay.
create or replace function public.evidence_weight(src text, observed timestamptz, expires timestamptz)
returns numeric language sql immutable as $$
  select case
    when expires is not null and expires < now() then 0
    else (case src
            when 'certifier' then 50 when 'admin' then 45 when 'manufacturer' then 40
            when 'owner' then 25 when 'user' then 10 when 'osm' then 12 when 'fsq' then 8 else 0 end)
         * power(0.5, extract(epoch from now() - observed) / 86400.0
                 / (case src when 'osm' then 730 when 'fsq' then 365 when 'certifier' then 365 else 365 end))
  end
$$;

-- Recompute a place's labels, confidence, tier and status from its evidence.
create or replace function public.refresh_place(p uuid)
returns void language plpgsql security definer set search_path = public as $$
declare
  w record;
  sup numeric; con numeric; people int; closed_votes int;
  pick text;
begin
  -- Halal yes vs no.
  select coalesce(sum(evidence_weight(source, observed_at, expires_at)) filter (where (attribute = 'halal' and value = 'yes') or (attribute = 'certified' and value <> 'revoked')), 0),
         coalesce(sum(evidence_weight(source, observed_at, expires_at)) filter (where (attribute = 'halal' and value = 'no') or (attribute = 'certified' and value = 'revoked')), 0),
         count(distinct source_ref) filter (where source = 'user' and attribute = 'halal' and value = 'yes' and observed_at > now() - interval '12 months'),
         count(distinct source_ref) filter (where source = 'user' and attribute = 'open' and value = 'closed' and observed_at > now() - interval '6 months')
    into sup, con, people, closed_votes
  from place_evidence where place_id = p and retracted_at is null;

  update places set
    -- Each label = the value with the most current weight; unknown when nothing supports one.
    halal_menu = coalesce((select value from place_evidence where place_id = p and retracted_at is null and attribute = 'halal_menu'
                           group by value having sum(evidence_weight(source, observed_at, expires_at)) > 0
                           order by sum(evidence_weight(source, observed_at, expires_at)) desc limit 1), 'unknown'),
    slaughter  = coalesce((select value from place_evidence where place_id = p and retracted_at is null and attribute = 'slaughter'
                           group by value having sum(evidence_weight(source, observed_at, expires_at)) > 0
                           order by sum(evidence_weight(source, observed_at, expires_at)) desc limit 1), 'unknown'),
    alcohol    = coalesce((select value from place_evidence where place_id = p and retracted_at is null and attribute = 'alcohol'
                           group by value having sum(evidence_weight(source, observed_at, expires_at)) > 0
                           order by sum(evidence_weight(source, observed_at, expires_at)) desc limit 1), 'unknown'),
    pork       = coalesce((select value from place_evidence where place_id = p and retracted_at is null and attribute = 'pork'
                           group by value having sum(evidence_weight(source, observed_at, expires_at)) > 0
                           order by sum(evidence_weight(source, observed_at, expires_at)) desc limit 1), 'unknown'),
    prayer     = coalesce((select value from place_evidence where place_id = p and retracted_at is null and attribute = 'prayer'
                           group by value having sum(evidence_weight(source, observed_at, expires_at)) > 0
                           order by sum(evidence_weight(source, observed_at, expires_at)) desc limit 1), 'unknown'),
    certifier_id = (select value::uuid from place_evidence where place_id = p and retracted_at is null and attribute = 'certified'
                    and value <> 'revoked' and (expires_at is null or expires_at > now()) order by observed_at desc limit 1),
    certified_until = (select expires_at::date from place_evidence where place_id = p and retracted_at is null and attribute = 'certified'
                       and value <> 'revoked' and (expires_at is null or expires_at > now()) order by observed_at desc limit 1),
    confidence = least(100, greatest(0, round(sup - con)))::smallint,
    tier = case
      when con > 0 and con >= sup * 0.5 then 'disputed'
      when exists (select 1 from place_evidence where place_id = p and retracted_at is null and attribute = 'certified'
                   and value <> 'revoked' and (expires_at is null or expires_at > now())) then 'certified'
      when people >= 2 then 'confirmed'
      else 'listed' end,
    status = case
      when status = 'hidden' then 'hidden'
      when closed_votes >= 2 then 'closed'
      when closed_votes = 1 then 'possibly_closed'
      when not exists (select 1 from place_sources where place_id = p and missing_since is null) then 'possibly_closed'
      else 'active' end,
    last_confirmed_at = (select max(observed_at) from place_evidence where place_id = p and retracted_at is null
                         and source in ('user', 'owner', 'admin', 'certifier', 'manufacturer') and value not in ('no', 'closed', 'revoked')),
    derived_at = now(),
    updated_at = now()
  where id = p;
end $$;

create or replace function public.place_evidence_changed()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  perform refresh_place(coalesce(new.place_id, old.place_id));
  return null;
end $$;

create trigger place_evidence_refresh
after insert or update or delete on public.place_evidence
for each row execute function public.place_evidence_changed();

-- Nightly (pg_cron or the API's scheduler): decay changes scores even with no new evidence.
--   select refresh_place(id) from places where derived_at < now() - interval '7 days';

-- ── Finding duplicates before inserting ─────────────────────────────────────
-- A candidate within 75 m whose name is similar is the same place.
create or replace function public.match_place(p_name text, p_lat double precision, p_lng double precision)
returns uuid language sql stable set search_path = public as $$
  select id from places
  where st_dwithin(location, st_makepoint(p_lng, p_lat)::geography, 75)
    and similarity(lower(name), lower(p_name)) > 0.45
  order by similarity(lower(name), lower(p_name)) desc,
           st_distance(location, st_makepoint(p_lng, p_lat)::geography)
  limit 1
$$;

-- ── Map query ───────────────────────────────────────────────────────────────
create or replace function public.places_near(p_lat double precision, p_lng double precision, p_radius_m integer default 8000, p_limit integer default 200)
returns setof public.places language sql stable set search_path = public as $$
  select * from places
  where status in ('active', 'possibly_closed')
    and st_dwithin(location, st_makepoint(p_lng, p_lat)::geography, least(p_radius_m, 40000))
    and (tier <> 'listed' or confidence > 8)   -- a bare Foursquare name match alone isn't shown
  order by tier = 'certified' desc, confidence desc,
           st_distance(location, st_makepoint(p_lng, p_lat)::geography)
  limit least(p_limit, 500)
$$;

-- ── Admin queue: only what needs a person ───────────────────────────────────
create or replace view public.places_needing_review as
  select p.*, 'disputed' as reason from public.places p where p.tier = 'disputed' and p.status <> 'hidden'
  union all
  select p.*, 'new_from_shopper' from public.places p
  where p.status = 'active' and p.tier = 'listed'
    and not exists (select 1 from public.place_sources s where s.place_id = p.id and s.source in ('osm', 'fsq', 'certifier'))
    and p.created_at < now() - interval '14 days';

-- ── Access ──────────────────────────────────────────────────────────────────
alter table public.places enable row level security;
alter table public.place_sources enable row level security;
alter table public.place_evidence enable row level security;
alter table public.place_reviews enable row level security;
alter table public.certifiers enable row level security;
alter table public.map_cells enable row level security;
alter table public.sync_runs enable row level security;

create policy "Places are public" on public.places for select using (status <> 'hidden');
create policy "Certifiers are public" on public.certifiers for select using (true);
create policy "Reviews are public" on public.place_reviews for select using (true);
-- Evidence, sources, cells and runs are written and read through the API (service role) only.
