alter table user_profiles
  add column if not exists is_admin boolean default false;
