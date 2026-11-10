-- 026: cities that have places on the map, so someone who doesn't share their
-- location (or is planning a trip) can pick a city instead. A view over
-- places: nothing is stored, so it is always current. Removes nothing.
-- city_key is lower-case with common accents removed ("montréal" -> "montreal"),
-- matching what the API does to the search text.
create or replace view public.place_cities with (security_invoker = true) as
select
  translate(lower(trim(p.city)), 'áàâäãåéèêëíìîïóòôöõúùûüçñ', 'aaaaaaeeeeiiiiooooouuuucn') as city_key,
  max(trim(p.city)) as city,
  nullif(trim(p.region), '') as region,
  count(*)::int as places,
  avg(p.lat) as lat,
  avg(p.lng) as lng
from public.places p
where p.status in ('active', 'possibly_closed') and nullif(trim(p.city), '') is not null
group by 1, nullif(trim(p.region), '');
