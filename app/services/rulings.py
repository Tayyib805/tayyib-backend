"""Apply sourced madhab rulings to a verdict.

Rulings are entered by an admin with the scholar or body cited (table
ingredient_rulings); nothing here is generated. A ruling applies only when:

- the person chose one of the four schools (or "strict", which takes the most
  cautious ruling across the schools), and
- a flagged ingredient matches a catalogued ingredient exactly (same E-number,
  or the same name/alias once brackets and punctuation are removed), and
- the product's verdict wasn't settled by product-specific evidence (a
  manufacturer's written answer or a scholar's review of that product).

The general verdict is always kept alongside, so the app can show both.
Pure functions: no I/O.
"""

from __future__ import annotations

import re

SCHOOLS = ("hanafi", "shafii", "maliki", "hanbali")
SCHOOL_NAMES = {"hanafi": "Hanafi", "shafii": "Shafi'i", "maliki": "Maliki", "hanbali": "Hanbali", "strict": "strictest"}
SEVERITY = {"halal": 0, "uncertain": 1, "haram": 2}


def name_key(name: str | None) -> str:
    """"Gelatin (E441)" and "gelatin" compare equal; "microbial rennet" and "rennet" don't."""

    text = re.sub(r"\([^)]*\)", " ", (name or "").lower())
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return " ".join(text.split())


def e_key(value: str | None) -> str | None:
    match = re.search(r"\be\s?-?(\d{3,4}[a-z]?)\b", (value or "").lower())
    return f"e{match.group(1)}" if match else None


def build_index(ingredients: list[dict], rulings: list[dict]) -> dict:
    """Look-up tables: ingredient by E-number or name, and current rulings by ingredient and school."""

    by_e: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for ing in ingredients:
        if e_key(ing.get("e_number")):
            by_e[e_key(ing["e_number"])] = ing["id"]
        for name in [ing.get("name"), *(ing.get("common_aliases") or [])]:
            key = name_key(name)
            if key:
                by_name.setdefault(key, ing["id"])
    current: dict[str, dict[str, dict]] = {}
    for r in rulings:
        if r.get("superseded_at") is None and r.get("madhab") in SCHOOLS:
            current.setdefault(r["ingredient_id"], {})[r["madhab"]] = r
    return {"by_e": by_e, "by_name": by_name, "rulings": current}


def find_ingredient(item: dict, index: dict) -> str | None:
    e = e_key(item.get("e_number")) or e_key(item.get("name"))
    if e and e in index["by_e"]:
        return index["by_e"][e]
    return index["by_name"].get(name_key(item.get("name")))


def ruling_for(ingredient_id: str, madhab: str, index: dict) -> dict | None:
    available = index["rulings"].get(ingredient_id) or {}
    if madhab in SCHOOLS:
        return available.get(madhab)
    if madhab == "strict" and available:
        return max(available.values(), key=lambda r: SEVERITY.get(r.get("status"), 1))
    return None


def worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: SEVERITY.get(s, 1)) if statuses else "halal"


def apply_madhab(response: dict, madhab: str | None, index: dict) -> dict:
    """The scan response adjusted for a school. Returns the same dict when nothing applies."""

    madhab = (madhab or "").strip().lower()
    if madhab not in SCHOOLS and madhab != "strict":
        return response
    if response.get("not_found") or response.get("tier") == 1 or response.get("verified_source"):
        return response  # product-specific verification outranks a general ruling about an ingredient
    flagged = response.get("flagged_ingredients") or []
    changed = 0
    adjusted = []
    for item in flagged:
        ingredient_id = find_ingredient(item, index)
        ruling = ruling_for(ingredient_id, madhab, index) if ingredient_id else None
        if not ruling:
            adjusted.append(item)
            continue
        new = dict(item)
        new["general_status"] = item.get("status")
        new["status"] = ruling["status"]
        new["reason"] = ruling["reasoning"]
        new["madhab_ruling"] = {
            "madhab": ruling["madhab"],
            "source": ruling["source"],
            "source_url": ruling.get("source_url"),
        }
        if new["status"] != item.get("status"):
            changed += 1
        adjusted.append(new)
    if not any("madhab_ruling" in i for i in adjusted):
        return response

    out = dict(response)
    out["flagged_ingredients"] = adjusted
    general = response.get("overall_status") or "uncertain"
    # Only re-derive the overall verdict when it was explained by the flagged
    # ingredients in the first place (otherwise something else decided it).
    if general == worst([i.get("status") or "uncertain" for i in flagged]):
        out["overall_status"] = worst([i.get("status") or "uncertain" for i in adjusted])
    out["madhab_applied"] = {
        "madhab": madhab,
        "name": SCHOOL_NAMES.get(madhab, madhab),
        "rulings": sum("madhab_ruling" in i for i in adjusted),
        "changed": changed,
        "general_status": general,
    }
    return out
