-- 024: small tidy after the Halal Maps and subscriptions migrations. Removes nothing.

-- Trigger functions aren't meant to be callable through the API (security advisor 0028/0029).
revoke all on function public.place_evidence_changed() from public, anon, authenticated;

-- Foreign keys without an index: deleting an account or merging two places
-- would otherwise scan these tables as they grow.
create index if not exists place_reviews_user_idx on public.place_reviews (user_id);
create index if not exists place_claims_user_idx on public.place_claims (user_id);
create index if not exists place_flags_user_idx on public.place_flags (user_id) where user_id is not null;
create index if not exists places_created_by_idx on public.places (created_by) where created_by is not null;
create index if not exists subscription_events_user_idx on public.subscription_events (user_id);
create index if not exists place_duplicates_b_idx on public.place_duplicates (place_b);
