-- 011: community reports that hide wrong map results, and who suggested a listing.

create table if not exists place_reports (
  place_key text not null check (place_key ~ '^(osm:(node|way|relation)/[0-9]+|tayyib:[0-9a-f-]{36})$'),
  user_id uuid not null references auth.users (id) on delete cascade,
  reason text not null check (reason in ('not_halal', 'not_restaurant', 'closed', 'wrong_location')),
  note text check (note is null or char_length(note) <= 300),
  created_at timestamptz not null default now(),
  primary key (place_key, user_id)
);
create index if not exists place_reports_key_idx on place_reports (place_key);

alter table place_reports enable row level security;
-- No policies: reports are written and read only by the API.

-- A place disappears from the map after three reports that it is not halal,
-- not a place to eat, or closed.
create or replace view hidden_places
with (security_invoker = true) as
select place_key, count(*) as reports
from place_reports
where reason in ('not_halal', 'not_restaurant', 'closed')
group by place_key
having count(*) >= 3;

revoke all on hidden_places from anon, authenticated;

alter table restaurants add column if not exists submitted_by uuid references auth.users (id) on delete set null;
alter table restaurants add column if not exists notes text;
