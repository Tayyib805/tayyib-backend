-- 010: Learn articles with likes, threaded comments and reports.
--
-- Articles are written and published by admins (people, not generated).
-- All writes go through the API, which validates, rate-limits and moderates;
-- the only direct reads allowed with the public key are published articles
-- and a user's own likes.

create table if not exists articles (
  id uuid primary key default gen_random_uuid(),
  slug text not null unique,
  title text not null check (char_length(title) between 3 and 140),
  summary text check (summary is null or char_length(summary) <= 280),
  body text not null default '',
  category text not null default 'ingredients'
    check (category in ('ingredients', 'rulings', 'labels', 'community', 'news')),
  ingredient_tag text check (ingredient_tag is null or char_length(ingredient_tag) <= 40),
  author_name text check (author_name is null or char_length(author_name) <= 80),
  author_role text check (author_role is null or char_length(author_role) <= 80),
  reviewed_by text check (reviewed_by is null or char_length(reviewed_by) <= 120),
  sources jsonb not null default '[]'::jsonb,
  read_minutes integer not null default 1,
  status text not null default 'draft' check (status in ('draft', 'published', 'archived')),
  published_at timestamptz,
  like_count integer not null default 0,
  comment_count integer not null default 0,
  created_by uuid references auth.users (id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists articles_published_idx on articles (status, published_at desc);

create table if not exists article_comments (
  id uuid primary key default gen_random_uuid(),
  article_id uuid not null references articles (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  parent_id uuid references article_comments (id) on delete cascade,
  display_name text not null check (char_length(display_name) between 1 and 40),
  body text not null check (char_length(body) between 2 and 1000),
  is_hidden boolean not null default false,
  -- Deleted comments that still have replies stay as a placeholder.
  is_deleted boolean not null default false,
  report_count integer not null default 0,
  created_at timestamptz not null default now(),
  edited_at timestamptz
);
create index if not exists article_comments_article_idx on article_comments (article_id, created_at);
create index if not exists article_comments_user_idx on article_comments (user_id, created_at desc);

create table if not exists article_likes (
  article_id uuid not null references articles (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (article_id, user_id)
);

create table if not exists comment_reports (
  comment_id uuid not null references article_comments (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,
  reason text check (reason is null or char_length(reason) <= 200),
  created_at timestamptz not null default now(),
  primary key (comment_id, user_id)
);

-- Keep like_count and comment_count exact, whatever path changes the rows.
create or replace function refresh_article_counts() returns trigger
language plpgsql
set search_path = public
as $$
declare
  target uuid := coalesce(new.article_id, old.article_id);
begin
  if tg_table_name = 'article_likes' then
    update articles
       set like_count = (select count(*) from article_likes where article_id = target)
     where id = target;
  else
    update articles
       set comment_count = (
         select count(*) from article_comments
          where article_id = target and not is_hidden and not is_deleted
       )
     where id = target;
  end if;
  return null;
end;
$$;

drop trigger if exists article_likes_count on article_likes;
create trigger article_likes_count
  after insert or delete on article_likes
  for each row execute function refresh_article_counts();

drop trigger if exists article_comments_count on article_comments;
create trigger article_comments_count
  after insert or delete or update of is_hidden, is_deleted on article_comments
  for each row execute function refresh_article_counts();

revoke execute on function refresh_article_counts() from public, anon, authenticated;

alter table articles enable row level security;
alter table article_comments enable row level security;
alter table article_likes enable row level security;
alter table comment_reports enable row level security;

drop policy if exists "Published articles are public" on articles;
create policy "Published articles are public" on articles
  for select using (status = 'published');

drop policy if exists "Users read own article likes" on article_likes;
create policy "Users read own article likes" on article_likes
  for select using (auth.uid() = user_id);

-- article_comments and comment_reports have no policies on purpose: comments
-- are served by the API, which hides user ids and moderated comments.
