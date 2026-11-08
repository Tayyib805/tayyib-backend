-- 019: account deletion, data integrity, indexes.
--
-- 1. Deleting an account failed whenever the person had scanned, voted or
--    sent a label photo ("no action" foreign keys). Personal records now go
--    with the account; community contributions stay, unattributed.
-- 2. Scan history is written only by the API. Direct inserts let anyone add
--    fake scans, and three scans count toward whose votes are trusted.
-- 3. Indexes for foreign keys the API filters on, and row-security
--    policies that evaluate auth.uid() once per query.
-- 4. Removes an ingredient row an analysis created by itself; the shared
--    ingredient list is for curated entries only.

-- 1. Foreign keys
alter table public.scan_logs drop constraint scan_logs_user_id_fkey,
  add constraint scan_logs_user_id_fkey foreign key (user_id) references auth.users (id) on delete cascade;
alter table public.scan_logs drop constraint scan_logs_product_id_fkey,
  add constraint scan_logs_product_id_fkey foreign key (product_id) references public.products (id) on delete set null;
alter table public.tier_votes drop constraint tier_votes_user_id_fkey,
  add constraint tier_votes_user_id_fkey foreign key (user_id) references auth.users (id) on delete cascade;
alter table public.refute_votes drop constraint refute_votes_user_id_fkey,
  add constraint refute_votes_user_id_fkey foreign key (user_id) references auth.users (id) on delete cascade;
alter table public.restaurant_reviews drop constraint restaurant_reviews_user_id_fkey,
  add constraint restaurant_reviews_user_id_fkey foreign key (user_id) references auth.users (id) on delete cascade;
alter table public.label_scans drop constraint label_scans_user_id_fkey,
  add constraint label_scans_user_id_fkey foreign key (user_id) references auth.users (id) on delete set null;
alter table public.product_refutes drop constraint product_refutes_user_id_fkey,
  add constraint product_refutes_user_id_fkey foreign key (user_id) references auth.users (id) on delete set null;
alter table public.product_refutes drop constraint product_refutes_reviewed_by_fkey,
  add constraint product_refutes_reviewed_by_fkey foreign key (reviewed_by) references auth.users (id) on delete set null;
alter table public.products drop constraint products_reviewed_by_fkey,
  add constraint products_reviewed_by_fkey foreign key (reviewed_by) references auth.users (id) on delete set null;

-- 2. Scan history: API only
drop policy if exists "Users can insert own scan logs" on public.scan_logs;
drop policy if exists "Users can update own scan logs" on public.scan_logs;
revoke insert, update on public.scan_logs from anon, authenticated;

-- 3. Policies that read auth.uid() once
drop policy if exists "Users can read own scan logs" on public.scan_logs;
create policy "Users can read own scan logs" on public.scan_logs for select to authenticated using ((select auth.uid()) = user_id);
drop policy if exists "Users can delete own scan logs" on public.scan_logs;
create policy "Users can delete own scan logs" on public.scan_logs for delete to authenticated using ((select auth.uid()) = user_id);
drop policy if exists "Users can read own profile" on public.user_profiles;
create policy "Users can read own profile" on public.user_profiles for select to authenticated using ((select auth.uid()) = id);
drop policy if exists "Users can insert own profile" on public.user_profiles;
create policy "Users can insert own profile" on public.user_profiles for insert to authenticated with check ((select auth.uid()) = id);
drop policy if exists "Users can update own profile" on public.user_profiles;
create policy "Users can update own profile" on public.user_profiles for update to authenticated using ((select auth.uid()) = id) with check ((select auth.uid()) = id);
drop policy if exists "Users can read own label scans" on public.label_scans;
create policy "Users can read own label scans" on public.label_scans for select to authenticated using ((select auth.uid()) = user_id);
drop policy if exists "Users can read own refute votes" on public.refute_votes;
create policy "Users can read own refute votes" on public.refute_votes for select to authenticated using ((select auth.uid()) = user_id);
drop policy if exists "Users read own article likes" on public.article_likes;
create policy "Users read own article likes" on public.article_likes for select using ((select auth.uid()) = user_id);

create index if not exists formulation_changes_new_idx on public.formulation_changes (new_formulation_id);
create index if not exists formulation_changes_old_idx on public.formulation_changes (old_formulation_id);
create index if not exists scan_logs_product_idx on public.scan_logs (product_id);
create index if not exists label_scans_user_idx on public.label_scans (user_id);
create index if not exists refute_votes_user_idx on public.refute_votes (user_id);
create index if not exists user_favourites_product_idx on public.user_favourites (product_id);
create index if not exists restaurant_reviews_restaurant_idx on public.restaurant_reviews (restaurant_id);
create index if not exists restaurant_reviews_user_idx on public.restaurant_reviews (user_id);
create index if not exists tier_votes_user_idx on public.tier_votes (user_id);
create index if not exists manufacturer_inquiries_manufacturer_idx on public.manufacturer_inquiries (manufacturer_id);
create index if not exists place_reports_user_idx on public.place_reports (user_id);
create index if not exists article_likes_user_idx on public.article_likes (user_id);
create index if not exists comment_reports_user_idx on public.comment_reports (user_id);
create index if not exists article_comments_parent_idx on public.article_comments (parent_id);

-- 4. Ingredient row created by an analysis, not curated
delete from public.product_ingredients where ingredient_id in (select id from public.ingredients where name = 'lécithines [SOJA]');
delete from public.ingredients where name = 'lécithines [SOJA]';
