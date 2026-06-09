"""Lightweight robots.txt checks for manufacturer scrapers."""

from __future__ import annotations

import logging
from functools import lru_cache
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

import httpx

USER_AGENT = "tayyib/1.0 halal-food-research"


def _normalize_domain(domain: str) -> str:
    """Return a host name from a domain or URL string."""

    parsed = urlparse(domain if "://" in domain else f"https://{domain}")
    return parsed.netloc or parsed.path


def _normalize_path(path: str) -> str:
    """Return a robots-compatible path from a path or URL string."""

    parsed = urlparse(path)
    if parsed.scheme and parsed.netloc:
        normalized = parsed.path or "/"
        if parsed.query:
            normalized = f"{normalized}?{parsed.query}"
        return normalized
    return path or "/"


@lru_cache(maxsize=128)
def _robots_parser(domain: str, body: str) -> RobotFileParser:
    """Build a cached RobotFileParser for a robots.txt body."""

    parser = RobotFileParser()
    parser.set_url(f"https://{domain}/robots.txt")
    parser.parse(body.splitlines())
    return parser


async def can_scrape(domain: str, path: str) -> bool:
    """Return True when robots.txt permits scraping this path.

    If robots.txt cannot be fetched, the scraper assumes the path is permitted.
    This keeps seeding resilient while still honoring explicit Disallow rules.
    """

    normalized_domain = _normalize_domain(domain)
    normalized_path = _normalize_path(path)
    robots_url = f"https://{normalized_domain}/robots.txt"

    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=15) as client:
            response = await client.get(robots_url)
        if response.status_code == 404:
            return True
        if response.status_code in {401, 403}:
            logging.warning("robots.txt denied access for %s", normalized_domain)
            return False
        response.raise_for_status()
    except Exception as exc:
        logging.info("Could not fetch %s: %s", robots_url, exc)
        return True

    parser = _robots_parser(normalized_domain, response.text)
    allowed = parser.can_fetch(USER_AGENT, normalized_path)
    if not allowed:
        logging.warning("robots.txt disallows %s%s", normalized_domain, normalized_path)
    return allowed
