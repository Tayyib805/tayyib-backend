-- 018: ingredients read from label photos need confirming.
--
-- product_formulations.confirmed_at: when this ingredient list became
-- trusted. Lists from Open Food Facts, manufacturers and admins are trusted
-- when stored. A list read from a label photo stays unconfirmed until three
-- different accounts photograph a matching label, an admin confirms it, or the
-- manufacturer's official list matches it.
--
-- label_scans.formulation_id: which ingredient list a photo matched, so
-- confirmations are counted per list and per account.
--
-- manufacturer_inquiries.request_ingredients / official_ingredients: the
-- email also asks for the official ingredient list, and the list the
-- manufacturer sent back.

alter table public.product_formulations add column if not exists confirmed_at timestamptz;
update public.product_formulations
   set confirmed_at = coalesce(created_at, now())
 where confirmed_at is null and data_source <> 'label_scan';

alter table public.label_scans
  add column if not exists formulation_id uuid references public.product_formulations (id) on delete set null;
create index if not exists label_scans_formulation_idx on public.label_scans (formulation_id);

alter table public.manufacturer_inquiries add column if not exists request_ingredients boolean not null default false;
alter table public.manufacturer_inquiries add column if not exists official_ingredients text
  check (official_ingredients is null or char_length(official_ingredients) <= 6000);
