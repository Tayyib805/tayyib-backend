-- 014: phone notifications (web push).
--
-- push_subscriptions: one row per browser or installed app that allowed
-- notifications. product_watchers: who asked us to contact a product's
-- manufacturer, so they hear when it answers. Both are read and written
-- only by the API (row security on, no policies).

create table if not exists public.push_subscriptions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  endpoint text not null unique check (char_length(endpoint) <= 1000 and endpoint like 'https://%'),
  p256dh text not null check (char_length(p256dh) <= 200),
  auth text not null check (char_length(auth) <= 100),
  user_agent text check (user_agent is null or char_length(user_agent) <= 300),
  created_at timestamptz not null default now(),
  last_sent_at timestamptz
);
create index if not exists push_subscriptions_user_idx on public.push_subscriptions (user_id);
alter table public.push_subscriptions enable row level security;
revoke all on public.push_subscriptions from anon, authenticated;

create table if not exists public.product_watchers (
  product_id uuid not null references public.products (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (product_id, user_id)
);
create index if not exists product_watchers_user_idx on public.product_watchers (user_id);
alter table public.product_watchers enable row level security;
revoke all on public.product_watchers from anon, authenticated;
