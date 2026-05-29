create extension if not exists pgcrypto;

create table if not exists products (
  id uuid primary key default gen_random_uuid(),
  barcode text unique,
  name text not null,
  brand text,
  image_url text,
  label_image_url text,
  verification_tier integer default 3 check (verification_tier in (1, 2, 3)),
  scan_count integer default 0,
  vote_count integer default 0,
  votes_needed_to_elevate integer default 25,
  overall_status text default 'uncertain' check (overall_status in ('halal', 'haram', 'uncertain')),
  confidence_level text default 'low' check (confidence_level in ('high', 'medium', 'low')),
  country_of_origin text,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  reviewed_by uuid references auth.users(id)
);

create table if not exists ingredients (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  e_number text,
  common_aliases text[],
  default_status text default 'uncertain' check (default_status in ('halal', 'haram', 'uncertain')),
  has_variants boolean default false,
  ruling_source text,
  ruling_notes text,
  madhab_exceptions jsonb default '{}',
  education_card_content text,
  created_at timestamptz default now()
);

create table if not exists product_ingredients (
  id uuid primary key default gen_random_uuid(),
  product_id uuid references products(id) on delete cascade,
  ingredient_id uuid references ingredients(id),
  variant_risk boolean default false,
  notes text
);

create table if not exists restaurants (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  address text,
  lat decimal(10,8),
  lng decimal(11,8),
  cuisine_type text,
  phone text,
  website text,
  halal_status text default 'unverified' check (halal_status in ('fully_halal', 'halal_options', 'unverified')),
  certification_doc_url text,
  alcohol_on_premises boolean default false,
  prayer_space boolean default false,
  listing_tier text default 'free' check (listing_tier in ('free', 'promoted')),
  listing_expires_at timestamptz,
  avg_rating decimal(3,2),
  review_count integer default 0,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists restaurant_reviews (
  id uuid primary key default gen_random_uuid(),
  restaurant_id uuid references restaurants(id) on delete cascade,
  user_id uuid references auth.users(id),
  rating integer check (rating between 1 and 5),
  halal_confidence integer check (halal_confidence between 1 and 5),
  body text,
  created_at timestamptz default now()
);

create table if not exists scan_logs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id),
  product_id uuid references products(id),
  scanned_at timestamptz default now(),
  result_shown text,
  tier_at_time_of_scan integer
);

create table if not exists user_profiles (
  id uuid primary key references auth.users(id),
  display_name text,
  madhab_preference text default 'general' check (
    madhab_preference in ('general', 'hanafi', 'shafi', 'maliki', 'hanbali', 'strict')
  ),
  active_theme text default 'andalusian',
  active_companion text default 'bee',
  points integer default 0,
  subscription_tier text default 'free' check (subscription_tier in ('free', 'standard', 'pro')),
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists tier_votes (
  id uuid primary key default gen_random_uuid(),
  product_id uuid references products(id) on delete cascade,
  user_id uuid references auth.users(id),
  reason text check (reason in ('conflicting_info', 'regular_user', 'label_discrepancy', 'other')),
  created_at timestamptz default now(),
  unique(product_id, user_id)
);

alter table products enable row level security;
alter table ingredients enable row level security;
alter table product_ingredients enable row level security;
alter table restaurants enable row level security;
alter table restaurant_reviews enable row level security;
alter table scan_logs enable row level security;
alter table user_profiles enable row level security;
alter table tier_votes enable row level security;

create policy "Products are publicly readable"
  on products for select
  using (true);

create policy "Authenticated users can insert products"
  on products for insert
  to authenticated
  with check (true);

create policy "Ingredients are publicly readable"
  on ingredients for select
  using (true);

create policy "Product ingredients are publicly readable"
  on product_ingredients for select
  using (true);

create policy "Restaurants are publicly readable"
  on restaurants for select
  using (true);

create policy "Restaurant reviews are publicly readable"
  on restaurant_reviews for select
  using (true);

create policy "Users can read own scan logs"
  on scan_logs for select
  to authenticated
  using (auth.uid() = user_id);

create policy "Users can insert own scan logs"
  on scan_logs for insert
  to authenticated
  with check (auth.uid() = user_id);

create policy "Users can update own scan logs"
  on scan_logs for update
  to authenticated
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy "Users can delete own scan logs"
  on scan_logs for delete
  to authenticated
  using (auth.uid() = user_id);

create policy "Users can read own profile"
  on user_profiles for select
  to authenticated
  using (auth.uid() = id);

create policy "Users can insert own profile"
  on user_profiles for insert
  to authenticated
  with check (auth.uid() = id);

create policy "Users can update own profile"
  on user_profiles for update
  to authenticated
  using (auth.uid() = id)
  with check (auth.uid() = id);

create index if not exists idx_products_barcode on products(barcode);
create index if not exists idx_products_verification_tier on products(verification_tier);
create index if not exists idx_product_ingredients_product_id on product_ingredients(product_id);
create index if not exists idx_restaurants_lat_lng on restaurants(lat, lng);
create index if not exists idx_scan_logs_user_id on scan_logs(user_id);
create index if not exists idx_tier_votes_product_id on tier_votes(product_id);
