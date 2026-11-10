-- 025: remove the first version of the place tables. Halal Maps moved to
-- places / place_evidence / place_reviews / place_flags in migration 022 and
-- nothing reads these any more. All four were empty when this was written.
-- Not yet applied to the live database as of 9 Nov 2026: a DROP needs the owner's
-- approval in Supabase (run this file in the SQL editor, or approve it when asked).
-- Without CASCADE on purpose: if anything still depends on them, this fails
-- and changes nothing.
drop view if exists public.hidden_places;
drop table if exists public.restaurant_reviews;
drop table if exists public.restaurants;
drop table if exists public.place_reports;
