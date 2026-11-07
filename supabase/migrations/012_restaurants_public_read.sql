-- 012: only reviewed restaurant listings are publicly readable.
-- Suggestions stay unverified (with the suggester's id and notes) until an
-- admin reviews them; the public key must not be able to read those.
drop policy if exists "Restaurants are publicly readable" on restaurants;
create policy "Reviewed restaurants are publicly readable" on restaurants
  for select using (halal_status <> 'unverified');
