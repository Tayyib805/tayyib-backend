alter table product_ingredients
add column if not exists is_flagged boolean default false;

alter table product_ingredients
add column if not exists flag_reason text;

alter table product_ingredients
add column if not exists confidence text;

alter table product_ingredients
drop constraint if exists product_ingredients_confidence_check;

alter table product_ingredients
add constraint product_ingredients_confidence_check
  check (confidence is null or confidence in ('high', 'medium', 'low'));

create index if not exists idx_product_ingredients_ingredient_id
  on product_ingredients(ingredient_id);

create index if not exists idx_product_ingredients_is_flagged
  on product_ingredients(is_flagged);
