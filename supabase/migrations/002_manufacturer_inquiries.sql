create table if not exists manufacturers (
  id uuid primary key default gen_random_uuid(),
  brand_name text not null,
  domain text,
  contact_email text,
  email_verified boolean default false,
  email_source text check (email_source in ('open_food_facts', 'admin_manual', 'user_submitted')),
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists manufacturer_inquiries (
  id uuid primary key default gen_random_uuid(),
  product_id uuid references products(id) on delete cascade,
  manufacturer_id uuid references manufacturers(id),
  brand_name text not null,
  product_name text not null,
  barcode text not null,
  flagged_ingredients jsonb not null,
  email_body text,
  recipient_email text,
  sent_at timestamptz,
  send_status text default 'pending_email' check (
    send_status in ('pending_email', 'pending_send', 'sent', 'failed', 'responded')
  ),
  response_received_at timestamptz,
  response_body text,
  resolved boolean default false,
  resolution_notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists idx_manufacturers_brand_name on manufacturers(brand_name);
create index if not exists idx_manufacturer_inquiries_product_id on manufacturer_inquiries(product_id);
create index if not exists idx_manufacturer_inquiries_send_status on manufacturer_inquiries(send_status);

alter table manufacturers enable row level security;
alter table manufacturer_inquiries enable row level security;

create policy "Manufacturers are publicly readable"
  on manufacturers for select
  using (true);

create policy "Authenticated users can insert manufacturers"
  on manufacturers for insert
  to authenticated
  with check (true);

create policy "Manufacturer inquiries are publicly readable"
  on manufacturer_inquiries for select
  using (true);

create policy "Service role can insert manufacturer inquiries"
  on manufacturer_inquiries for insert
  to service_role
  with check (true);

create policy "Service role can update manufacturer inquiries"
  on manufacturer_inquiries for update
  to service_role
  using (true)
  with check (true);
