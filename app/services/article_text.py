"""Text helpers for Learn articles and comments.

Pure functions (no I/O) so they can be unit tested without the app stack.
"""

from __future__ import annotations

import re
import unicodedata

WORDS_PER_MINUTE = 220
MAX_LINKS_PER_COMMENT = 1
URL_PATTERN = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
CONTROL_CHARS = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏‪-‮⁦-⁩]")


def slugify(title: str, max_length: int = 80) -> str:
    """Make a URL slug from a title: "E471: Mono- & Diglycerides" -> "e471-mono-diglycerides"."""

    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "article"


def read_minutes(body: str) -> int:
    """Estimated reading time in whole minutes, at least one."""

    words = len(re.findall(r"\w+", body or ""))
    return max(1, round(words / WORDS_PER_MINUTE))


def clean_comment(body: str) -> str:
    """Strip invisible/control characters and collapse runs of blank lines."""

    text = CONTROL_CHARS.sub("", body or "").replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def comment_problem(body: str) -> str | None:
    """Return a reason to reject a cleaned comment, or None if it is fine."""

    if len(body) < 2:
        return "Write a little more before posting."
    if len(body) > 1000:
        return "Comments can be up to 1,000 characters."
    if len(URL_PATTERN.findall(body)) > MAX_LINKS_PER_COMMENT:
        return "Comments can include one link at most."
    letters = [c for c in body if c.isalpha()]
    if len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return "Please don't post in all capitals."
    return None


def clean_display_name(name: str | None) -> str | None:
    """Return a safe public name (2-40 chars, no links or emails), or None."""

    if not name:
        return None
    text = CONTROL_CHARS.sub("", name).strip()
    text = re.sub(r"\s+", " ", text)
    if not 2 <= len(text) <= 40 or "@" in text or URL_PATTERN.search(text):
        return None
    return text


def public_name(profile_name: str | None, email: str | None) -> str:
    """Name shown on comments. Never reveal an email or its username part."""

    name = clean_display_name(profile_name)
    local_part = (email or "").split("@")[0].lower()
    if not name or (local_part and name.lower() == local_part):
        return "tayyib. member"
    return name


def clean_sources(sources: list | None) -> list[dict]:
    """Keep up to 12 sources with a title and an http(s) URL."""

    cleaned = []
    for item in sources or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:200]
        url = str(item.get("url") or "").strip()[:500]
        if title and re.match(r"^https?://\S+\.\S+", url):
            cleaned.append({"title": title, "url": url})
    return cleaned[:12]
