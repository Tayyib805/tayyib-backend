"""Claude-powered manufacturer inquiry email generation."""

import anthropic

from app.config import get_settings


async def generate_inquiry_email(
    product_name: str,
    brand_name: str,
    barcode: str,
    flagged_ingredients: list,
) -> str:
    """Generate professional halal ingredient inquiry email using Claude."""

    settings = get_settings()
    ingredients_list = "\n".join(
        [f"- {ing['name']}: {ing['reason']}" for ing in flagged_ingredients]
    )

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        temperature=0,
        system=(
            "You write professional, polite consumer inquiry emails on behalf of "
            "tayyib., a halal food intelligence platform serving the Muslim "
            "community. Emails are warm, respectful, specific, and under 200 "
            "words. Never accusatory."
        ),
        messages=[
            {
                "role": "user",
                "content": f"""Write a consumer inquiry email to {brand_name} about the following product:

Product: {product_name}
Barcode: {barcode}

We need clarification on these ingredients for our Muslim community:
{ingredients_list}

For each uncertain ingredient, ask:
- Is it plant-derived, animal-derived, or synthetic?
- If animal-derived, what species?
- Is there halal certification available?

Sign off as:
tayyib. Community Verification Team
verify@tayyib.app
tayyib.app

Return only the email body.
No subject line. No markdown.""",
            }
        ],
    )
    return response.content[0].text.strip()
