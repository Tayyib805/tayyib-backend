"""Claude-powered manufacturer inquiry email generation."""

from app.services.claude_analysis import create_message


async def generate_inquiry_email(
    product_name: str,
    brand_name: str,
    barcode: str,
    flagged_ingredients: list,
    request_ingredient_list: bool = False,
) -> str:
    """Generate professional halal ingredient inquiry email using Claude."""

    ingredients_list = "\n".join(
        [f"- {ing.get('name')}: {ing.get('reason') or 'source not stated'}" for ing in flagged_ingredients]
    ) or "(none flagged yet)"
    list_request = (
        "\nBefore the ingredient questions, ask them to send the complete, current "
        "ingredient list for this product exactly as printed on the pack, and to say "
        "which countries it applies to. Explain in one short clause that we want our "
        "listing to match their official information.\n"
        if request_ingredient_list
        else ""
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
{list_request}
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


def fallback_inquiry_email(
    product_name: str,
    brand_name: str,
    barcode: str,
    flagged_ingredients: list,
    request_ingredient_list: bool = False,
) -> str:
    """A plain, fixed-wording draft for when the writing service is unavailable.

    It is still only a draft: an admin reads, edits and approves every email.
    """

    lines = [
        f"Hello {brand_name} team,",
        "",
        f"Muslim customers ask us whether {product_name} (barcode {barcode or 'not known'}) fits a halal diet, "
        "and we'd like to give them an accurate answer from the source.",
        "",
    ]
    n = 1
    if request_ingredient_list:
        lines.append(
            f"{n}. Could you send the complete, current ingredient list for this product, exactly as printed on the "
            "pack, and tell us which countries it applies to? We want our listing to match your official information."
        )
        n += 1
    names = [str(f.get("name")) for f in flagged_ingredients if f.get("name")]
    if names:
        lines.append(
            f"{n}. For {', '.join(names)}: is each one from a plant, animal or synthetic source? If animal, which "
            "species, and is any alcohol used as a carrier or solvent?"
        )
        n += 1
    lines += [
        f"{n}. Does the answer differ by country of sale or production facility?",
        f"{n + 1}. Is the product covered by a halal certificate? If so, which certifying body?",
        "",
        "A written reply would let us share your answer accurately. Thank you for your help.",
        "",
        "tayyib. Community Verification Team",
        "verify@tayyib.app",
        "tayyib.app",
    ]
    return "\n".join(lines)
