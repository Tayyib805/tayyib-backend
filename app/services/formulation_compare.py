"""Compare a stored ingredient list with one read from a label photo.

Deterministic on purpose: it reports exactly which ingredients appear on
one list and not the other, so a reviewer can see why a label was judged
changed. Small reading differences (spacing, punctuation, percentages,
"contains 2% or less of", processing notes like "processed with alkali", a
letter or two misread) are tolerated; a genuinely new or missing
ingredient is not. Source notes such as "(from pork)" are kept: they
matter for a halal verdict.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_NOISE_PHRASES = [
    r"contains\s+(?:less\s+than\s+)?\d+(?:\.\d+)?\s*%\s+or\s+less\s+of",
    r"contains\s+less\s+than\s+\d+(?:\.\d+)?\s*%\s+of",
    r"less\s+than\s+\d+(?:\.\d+)?\s*%\s+of",
    r"\d+(?:\.\d+)?\s*%\s+or\s+less\s+of",
    r"\bingredients?\b",
    r"\bingrédients?\b",
    r"\band/or\b",
    r"&/or",
    r"\bprocessed\s+with\s+alkali\b",
    r"\bdutch(?:ed)?(?:\s+processed)?\b",
    r"\bfor\s+colou?r\b",
    r"\bcolou?r\s+added\b",
    r"\bfor\s+freshness\b",
    r"\bto\s+preserve\s+freshness\b",
    r"\bto\s+maintain\s+freshness\b",
]
_STOP = {"contains", "contain", "of", "or", "and", "less", "than", "the", "a", "an", "with", "added"}
_SIMILAR = 0.84


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    # Advisory statements describe the factory, not the recipe.
    text = re.sub(r"(?:may\s+contain|produced\s+in\s+a\s+facility|manufactured\s+in\s+a\s+facility)[^.]*\.?", " ", text)
    for phrase in _NOISE_PHRASES:
        text = re.sub(phrase, ",", text)
    text = re.sub(r"\d+(?:[.,]\d+)?\s*%", " ", text)
    return text


def ingredients(text: str) -> list[str]:
    """The individual ingredients in a label's text, normalized."""

    out: list[str] = []
    for part in re.split(r"[,;:()\[\]{}*•·|]|\.(?!\d)|\n", _plain(text)):
        token = re.sub(r"[^a-z0-9\- ]+", " ", part)
        token = re.sub(r"\s+", " ", token).strip(" -")
        if len(token) < 2 or token in _STOP:
            continue
        if token not in out:
            out.append(token)
    return out


def _same(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        return True
    return SequenceMatcher(None, a, b).ratio() >= _SIMILAR


def compare(stored_text: str, label_text: str) -> dict:
    """Which ingredients were added or removed between the stored list and the label."""

    old = ingredients(stored_text)
    new = ingredients(label_text)
    unmatched_old = list(old)
    added: list[str] = []
    for token in new:
        hit = next((o for o in unmatched_old if _same(token, o)), None)
        if hit is None:
            # It may still match something already paired (a split difference).
            if any(_same(token, o) for o in old):
                continue
            added.append(token)
        else:
            unmatched_old.remove(hit)
    removed = [o for o in unmatched_old if not any(_same(o, n) for n in new)]
    total = max(len(old), len(new), 1)
    similarity = round(1 - (len(added) + len(removed)) / (2 * total), 3)
    return {
        "matched": not added and not removed,
        "added": added,
        "removed": removed,
        "similarity": max(similarity, 0.0),
        "readable": bool(new),
    }
