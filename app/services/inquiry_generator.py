"""Claude-powered manufacturer inquiry email generation."""

from app.services.claude_analysis import create_message


async def generate_inquiry_email(
    product_name: str,
    brand_name: str,
    barcode: str,
    flagged_ingredients: list,
) -> str:
    """Generate professional halal ingredient inquiry email using Claude."""

    ingredients_list = "\n".join(
        [f"- {ing['name']}: {ing['reason']}" for ing in flagged_ingredients]
    )

    response = await create_message(
        max_tokens=700,
        temperature=0,
        system=(
            "You write consumer inquiry emails on behalf of tayyib., a halal food "
            "verification platform used by Muslim families. The goal is a written "
            "answer we can document, so every question must be specific and "
            "answerable with a fact (a source, a species, a yes or no, a document). "
            "Tone: warm, respectful, brief, never accusatory. Under 230 words. "
            "Never claim the product is or isn't halal, and never invent facts "
            "about the product or its ingredients."
        ),
        messages=[
            {
                "role": "user",
                "content": f"""Write an inquiry email to {brand_name} about:

Product: {product_name}
Barcode: {barcode}

Ingredients we need clarified, with why each is uncertain:
{ingredients_list}

For each ingredient above, ask only the questions that apply to it, as a short
numbered list grouped under the ingredient's name:
- Gelatin, collagen: Which animal species is it from? If bovine, is the animal
  slaughtered according to halal requirements, and is there a certificate?
- Natural or artificial flavours, vanilla, extracts: Is ethanol or any other
  alcohol used as a carrier or solvent, and does any remain in the finished
  product? Are any components animal-derived?
- Emulsifiers and fats (mono- and diglycerides, E471, E472, glycerin, stearates,
  shortening): Is the fat source plant or animal? If animal, which species?
- Enzymes, rennet, whey, cheese cultures: Are the enzymes microbial, plant or
  animal? If animal, which species?
- Carmine, cochineal, E120, shellac: Please confirm whether it is present.
- L-cysteine, E920: Is it synthetic, plant-fermented, or from feathers or hair?
- Anything else listed: Plant, animal or synthetic? If animal, which species?

Then ask, once for the whole product:
1. Do the answers differ by country of sale or by production facility?
2. Is the product, or are these ingredients, covered by a halal certificate? If
   so, which certifying body?
3. Could they reply in writing so we can share the answer accurately?

Open with one sentence explaining that Muslim customers ask us about this
product and we'd like to give them an accurate answer from the source.

Sign off as:
tayyib. Community Verification Team
verify@tayyib.app
tayyib.app

Return only the email body. No subject line. No markdown.""",
            }
        ],
    )
    return response.content[0].text.strip()
