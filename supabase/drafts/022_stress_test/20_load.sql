set client_min_messages = warning;
\timing on
\echo '--- P1 first import: 20,000 places + source + evidence (row trigger refreshes each)'
insert into places (name, location) select 'Place ' || g, st_makepoint(-118 + random() * 1.5, 33.5 + random() * 1.0) from generate_series(1, 20000) g;
insert into place_sources (place_id, source, external_id, scope) select id, 'fsq', 'f-' || id, 'US' from places where name like 'Place %';
insert into place_evidence (place_id, attribute, value, source, source_ref) select id, 'halal', 'yes', 'fsq', 'f-' || id from places where name like 'Place %';
\echo '--- P2 30,000 shopper answers from 500 accounts'
insert into auth.users (created_at) select now() - interval '200 days' from generate_series(1, 500);
with u as (select id, row_number() over () rn from auth.users)
insert into place_evidence (place_id, attribute, value, source, source_ref, user_id, observed_at)
  select p.id, a.attr, case a.attr when 'halal' then 'yes' when 'alcohol' then 'no' else 'full' end, 'user', md5(u.id::text), u.id, now() - (random() * 400 || ' days')::interval
  from generate_series(1, 30000) g
  join u on u.rn = 1 + g % 500
  cross join lateral (select id from places where name = 'Place ' || (1 + (g * 7919) % 20000)) p
  cross join lateral (select (array['halal','alcohol','halal_menu'])[1 + g % 3] as attr) a
  on conflict do nothing;
\echo '--- P3 monthly re-import: upsert 20,000 re-asserted claims'
insert into place_evidence (place_id, attribute, value, source, source_ref) select id, 'halal', 'yes', 'fsq', 'f-' || id from places where name like 'Place %'
  on conflict (place_id, attribute, source, source_ref) do update set observed_at = excluded.observed_at;
\echo '--- P4 weekly decay refresh of every place'
select count(*) from (select refresh_place(id) from places) s;
\echo '--- P5 storage after all of the above'
vacuum full places; vacuum full place_evidence;
select pg_size_pretty(pg_total_relation_size('places')) places, pg_size_pretty(pg_total_relation_size('place_evidence')) evidence, pg_size_pretty(pg_total_relation_size('place_sources')) sources;
