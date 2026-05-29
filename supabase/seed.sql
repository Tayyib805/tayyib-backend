insert into ingredients (
  name,
  e_number,
  default_status,
  has_variants,
  education_card_content
) values
(
  'Gelatin',
  'E441',
  'uncertain',
  true,
  'Can be pork, beef, or fish. Pork is haram. Beef only halal if zabiha. Source almost never specified on labels.'
),
(
  'Carmine',
  'E120',
  'haram',
  false,
  'Red dye from crushed cochineal insects. Near-universal haram ruling. Found in red/pink foods and cosmetics.'
),
(
  'Mono and Diglycerides',
  'E471',
  'uncertain',
  true,
  'Can be plant (halal) or animal including pork (haram). Source rarely specified by manufacturers.'
),
(
  'L-Cysteine',
  'E920',
  'uncertain',
  true,
  'In bread. Can be from human hair, duck feathers, or synthetic. Human hair-derived considered haram by many scholars.'
),
(
  'Lecithin',
  'E322',
  'uncertain',
  true,
  'Usually soy (halal) but can be animal-derived. Verify source.'
),
(
  'Natural Flavors',
  null,
  'uncertain',
  false,
  'FDA catch-all. Can hide animal derivatives including pork. Source never specified on labels.'
),
(
  'Rennet',
  null,
  'uncertain',
  true,
  'Used in cheese. Animal rennet from non-zabiha is haram for most scholars. Microbial/vegetable rennet is halal.'
),
(
  'Vanilla Extract',
  null,
  'uncertain',
  false,
  'Contains 35% alcohol by volume. Many scholars consider this haram. Vanilla powder and vanillin are halal alternatives.'
),
(
  'Riboflavin',
  'E101',
  'halal',
  false,
  'Vitamin B2. Generally plant or synthetic. Considered halal unless specifically animal-derived.'
),
(
  'Citric Acid',
  'E330',
  'halal',
  false,
  'From citrus or fermentation. Universally considered halal.'
)
on conflict (name) do update set
  e_number = excluded.e_number,
  default_status = excluded.default_status,
  has_variants = excluded.has_variants,
  education_card_content = excluded.education_card_content;
