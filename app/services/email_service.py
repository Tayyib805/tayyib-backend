"""Email delivery for manufacturer inquiries."""

import logging

from app.config import get_settings


async def send_inquiry_email(
    to_email: str,
    brand_name: str,
    product_name: str,
    email_body: str,
    inquiry_id: str,
) -> dict:
    """Send manufacturer inquiry via Resend."""

    del brand_name
    settings = get_settings()
    try:
        import resend

        resend.api_key = settings.RESEND_API_KEY
        response = resend.Emails.send(
            {
                "from": "tayyib. Verification <verify@tayyib.app>",
                "to": [to_email],
                "subject": (
                    f"Ingredient inquiry — {product_name} "
                    f"[Ref: {inquiry_id[:8].upper()}]"
                ),
                "text": email_body,
            }
        )
        return {
            "success": True,
            "message_id": response.get("id"),
            "error": None,
        }
    except Exception as e:
        logging.error(f"Email send failed: {e}")
        return {
            "success": False,
            "message_id": None,
            "error": str(e),
        }
