\pset footer off
\set QUIET on
create or replace function t_place(n text, lat float8, lng float8) returns uuid language sql as $$ insert into places (name, location) values (n, st_makepoint(lng, lat)) returning id $$;
create or replace function t_src(p uuid, s text, ext text, sc text default 'US') returns void language sql as $$ insert into place_sources (place_id, source, external_id, scope) values (p, s, ext, sc) $$;
create or replace function t_user(days int) returns uuid language sql as $$ insert into auth.users (created_at) values (now() - (days || ' days')::interval) returning id $$;
create or replace function t_ev(p uuid, a text, v text, s text, ref text, age interval default '0', uid uuid default null) returns void language sql as $$
  insert into place_evidence (place_id, attribute, value, source, source_ref, observed_at, user_id) values (p, a, v, s, ref, now() - age, uid) $$;
create or replace function t_uev(p uuid, a text, v text, uid uuid, age interval default '0') returns void language sql as $$
  select t_ev(p, a, v, 'user', md5(uid::text), age, uid) $$;
create or replace view t_show as select name, status, tier, confidence as conf, halal_menu as menu, disagreeing_people as disagree, closed_reports as closed_rep,
  status in ('active','possibly_closed') as on_map from places;
select t_user(400) as vet1 \gset
select t_user(400) as vet2 \gset
select t_user(400) as vet3 \gset
select t_user(1) as new1 \gset
select t_user(1) as new2 \gset
select t_user(1) as new3 \gset

\echo '=== S1 Foursquare place in the Halal category, nothing else'
select t_place('Al Amir Restaurant', 33.8353, -117.9145) as p1 \gset
select t_src(:'p1', 'fsq', 'fsq1') \g /dev/null
select t_ev(:'p1', 'halal', 'yes', 'fsq', 'fsq1') \g /dev/null
select * from t_show where name = 'Al Amir Restaurant';
\echo '=== S2 also tagged in OSM'
select t_src(:'p1', 'osm', 'node/1', 'cell1') \g /dev/null
select t_ev(:'p1', 'halal', 'yes', 'osm', 'node/1') \g /dev/null
select * from t_show where name = 'Al Amir Restaurant';
\echo '=== S3a one brand-new account says "not halal"'
select t_uev(:'p1', 'halal', 'no', :'new1') \g /dev/null
select * from t_show where name = 'Al Amir Restaurant';
\echo '=== S3b three brand-new accounts say "not halal" (a sock-puppet burst)'
select t_uev(:'p1', 'halal', 'no', :'new2') \g /dev/null
select t_uev(:'p1', 'halal', 'no', :'new3') \g /dev/null
select * from t_show where name = 'Al Amir Restaurant';
\echo '=== S3c two established accounts say "not halal"'
select t_uev(:'p1', 'halal', 'no', :'vet1') \g /dev/null
select t_uev(:'p1', 'halal', 'no', :'vet2') \g /dev/null
select * from t_show where name = 'Al Amir Restaurant';

\echo '=== S4a one established account reports "closed"'
select t_place('Kabob Express', 33.8360, -117.9150) as p2 \gset
select t_src(:'p2', 'fsq', 'fsq2') \g /dev/null
select t_ev(:'p2', 'halal', 'yes', 'fsq', 'fsq2') \g /dev/null
select t_uev(:'p2', 'open', 'closed', :'vet1') \g /dev/null
select * from t_show where name = 'Kabob Express';
\echo '=== S4b a second account reports "closed"'
select t_uev(:'p2', 'open', 'closed', :'vet2') \g /dev/null
select * from t_show where name = 'Kabob Express';
\echo '=== S4c next day a shopper eats there and taps "still halal"'
select t_uev(:'p2', 'halal', 'yes', :'vet3') \g /dev/null
update place_evidence set observed_at = now() - interval '1 day' where place_id = :'p2' and attribute = 'open';
select refresh_place(:'p2') \g /dev/null
select * from t_show where name = 'Kabob Express';

\echo '=== S5 two shoppers confirmed 13 months ago, nobody since'
select t_place('Zaytoon Grill', 33.8370, -117.9160) as p3 \gset
select t_src(:'p3', 'osm', 'node/3', 'cell1') \g /dev/null
select t_uev(:'p3', 'halal', 'yes', :'vet1', '13 months') \g /dev/null
select t_uev(:'p3', 'halal', 'yes', :'vet2', '13 months') \g /dev/null
select *, (select last_confirmed_at::date from places where id = :'p3') as last_confirmed from t_show where name = 'Zaytoon Grill';
\echo '=== S6 shopper-added place, one confirmation 5 months ago'
select t_place('Sahara Grill', 33.8400, -117.9200) as p4 \gset
select t_src(:'p4', 'user', md5(:'vet1')) \g /dev/null
select t_uev(:'p4', 'halal', 'yes', :'vet1', '5 months') \g /dev/null
select * from t_show where name = 'Sahara Grill';
\echo '=== S7 OSM-only place nobody has opened in 18 months'
select t_place('Quiet Suburb Halal', 34.0, -117.5) as p5 \gset
select t_src(:'p5', 'osm', 'node/5', 'cell9') \g /dev/null
select t_ev(:'p5', 'halal', 'yes', 'osm', 'node/5', '18 months') \g /dev/null
select * from t_show where name = 'Quiet Suburb Halal';
\echo '=== S8 contradiction: menu "full" but halal net "no"'
select t_place('Mixed Signals', 33.85, -117.93) as p6 \gset
select t_src(:'p6', 'fsq', 'fsq6') \g /dev/null
select t_uev(:'p6', 'halal_menu', 'full', :'vet3') \g /dev/null
select t_uev(:'p6', 'halal', 'no', :'vet1') \g /dev/null
select t_uev(:'p6', 'halal', 'no', :'vet2') \g /dev/null
select t_ev(:'p6', 'halal', 'yes', 'fsq', 'fsq6') \g /dev/null
select * from t_show where name = 'Mixed Signals';

\echo '=== S9 monthly re-import upserts; an admin-retracted claim stays retracted'
update place_evidence set retracted_at = now() where place_id = :'p1' and source = 'osm';
insert into place_evidence (place_id, attribute, value, source, source_ref) values (:'p1', 'halal', 'yes', 'osm', 'node/1')
  on conflict (place_id, attribute, source, source_ref) do update set observed_at = excluded.observed_at;
select source, (retracted_at is not null) as still_retracted, observed_at > now() - interval '1 minute' as refreshed from place_evidence where place_id = :'p1' and source in ('osm', 'fsq') order by 1;

\echo '=== S10 bad certifier value is refused; a proper one certifies'
\set ON_ERROR_STOP 0
insert into place_evidence (place_id, attribute, value, source, source_ref) values (:'p3', 'certified', 'HMS', 'certifier', 'hms-1');
insert into certifiers (name) values ('HMS') returning id as hms \gset
insert into place_evidence (place_id, attribute, value, source, source_ref, certifier_id) values (:'p3', 'certified', 'yes', 'certifier', 'hms-1', :'hms');
select * from t_show where name = 'Zaytoon Grill';

\echo '=== S11 duplicate matching'
update places set phone = '7145550100' where id = :'p4';
select 'Al-Amir (OSM spelling), 20 m' as candidate, (match_place('Al-Amir', 33.8354, -117.9146) = :'p1') as same_place
union all select 'Al Amir Restaurant & Grill, 20 m', (match_place('Al Amir Restaurant & Grill', 33.8354, -117.9146) = :'p1')
union all select 'Kabob House (a different shop), 40 m', (match_place('Kabob House', 33.8363, -117.9150) = :'p2')
union all select 'Sahara Grill, pin 120 m off', (match_place('Sahara Grill', 33.8411, -117.9200) = :'p4')
union all select 'Sahara Mediterranean (renamed), same phone, 250 m', (match_place('Sahara Mediterranean', 33.8422, -117.9200, '7145550100') = :'p4');
select 'Kabob House flagged as possible duplicate of Kabob Express' as check, exists (select 1 from near_duplicates('Kabob House', 33.8363, -117.9150) where place_id = :'p2') as flagged;

\echo '=== S12 an unverified "owner" vs a verified one'
select t_place('Owner Test', 33.86, -117.94) as p7 \gset
select t_src(:'p7', 'fsq', 'fsq7') \g /dev/null
select t_ev(:'p7', 'halal', 'yes', 'owner', md5(:'vet3'), '0', :'vet3') \g /dev/null
select 'unverified owner' as who, confidence from places where id = :'p7';
insert into place_claims (place_id, user_id, method, verified_at) values (:'p7', :'vet3', 'phone_callback', now());
select refresh_place(:'p7') \g /dev/null
select 'verified owner' as who, confidence from places where id = :'p7';

\echo '=== S13 sync safety: a broken monthly feed, then healthy runs'
insert into places (name, location) select 'Bulk ' || g, st_makepoint(-117.9 + g * 0.001, 33.9) from generate_series(1, 100) g;
insert into place_sources (place_id, source, external_id, scope, last_seen_at) select id, 'fsq', 'bulk-' || id, 'CA', now() - interval '40 days' from places where name like 'Bulk %';
insert into sync_runs (source, scope) values ('fsq', 'CA') returning id as run1 \gset
update place_sources set last_seen_at = now() where scope = 'CA' and external_id in (select 'bulk-' || id from places where name in ('Bulk 1','Bulk 2','Bulk 3'));
select 'feed returned 3 of 100' as run, finish_sync_run(:'run1') as result, (select count(*) from places where name like 'Bulk %' and status <> 'active') as places_affected;
insert into sync_runs (source, scope) values ('fsq', 'CA') returning id as run2 \gset
update place_sources set last_seen_at = now() where scope = 'CA' and external_id not in (select 'bulk-' || id from places where name = 'Bulk 50');
select 'healthy run, 1 missing' as run, finish_sync_run(:'run2') as result, (select status from places where name = 'Bulk 50') as bulk_50;
insert into sync_runs (source, scope) values ('fsq', 'CA') returning id as run3 \gset
update place_sources set last_seen_at = now() where scope = 'CA' and external_id not in (select 'bulk-' || id from places where name = 'Bulk 50');
select 'second healthy run, still missing' as run, finish_sync_run(:'run3') as result, (select status from places where name = 'Bulk 50') as bulk_50;

\echo '=== S14 account deletion: their answers stay, unlinked'
delete from auth.users where id = :'vet3';
select count(*) as answers_kept, count(user_id) as still_linked from place_evidence where source_ref = md5(:'vet3');
select * from t_show where name = 'Kabob Express';
