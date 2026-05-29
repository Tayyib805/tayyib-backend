"""Local halal ruling helpers."""


def summarize_status(flagged_ingredients: list[dict]) -> tuple[str, str]:
    """Return an overall status and confidence from flagged ingredients."""

    statuses = {item.get("status") for item in flagged_ingredients}
    if "haram" in statuses:
        return "haram", "high"
    if "uncertain" in statuses:
        return "uncertain", "medium"
    if statuses == {"halal"}:
        return "halal", "medium"
    return "uncertain", "low"
