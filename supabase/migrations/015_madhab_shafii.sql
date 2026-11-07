-- 015: the app saves Shafi'i as 'shafii', but the original check only allowed
-- 'shafi', so choosing Shafi'i failed at setup. Allow 'shafii' and move any
-- old 'shafi' rows to it.

alter table public.user_profiles drop constraint if exists user_profiles_madhab_preference_check;
update public.user_profiles set madhab_preference = 'shafii' where madhab_preference = 'shafi';
alter table public.user_profiles add constraint user_profiles_madhab_preference_check
  check (madhab_preference in ('general', 'hanafi', 'shafii', 'maliki', 'hanbali', 'strict'));
