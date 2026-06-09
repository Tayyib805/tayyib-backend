"""Manufacturer website scrapers for pre-seeding Tayyib products.

Run examples:
    python manufacturer_scraper.py --brand fritolay --dry-run
    python manufacturer_scraper.py --brand haribo --limit 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from html import unescape
from io import BytesIO
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

try:
    from .robots_checker import USER_AGENT, can_scrape
except ImportError:  # Allows `cd scrapers && python manufacturer_scraper.py`.
    from robots_checker import USER_AGENT, can_scrape


ProductDict = dict[str, Any]


def clean_text(value: str | None) -> str:
    """Normalize whitespace and common entity noise."""

    if not value:
        return ""
    return re.sub(r"\s+", " ", unescape(value)).strip()


def unique_preserve_order(values: list[str]) -> list[str]:
    """Return de-duplicated values while keeping their first-seen order."""

    seen = set()
    result = []
    for value in values:
        normalized = value.strip()
        key = normalized.lower()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def best_title_part(value: str) -> str:
    """Return the most product-like part of a delimited browser title."""

    parts = [clean_text(part) for part in re.split(r"\s+\|\s+|\s+-\s+", value) if part.strip()]
    if not parts:
        return clean_text(value)
    return max(parts, key=len)


def extract_ingredients_from_text(text: str) -> str:
    """Extract an ingredient statement from visible page text."""

    normalized = clean_text(text)
    if not normalized:
        return ""

    patterns = [
        r"\bINGREDIENTS?\s*:\s*(.*?)(?=\s+(?:CONTAINS?\s*:|MAY CONTAIN\s*:|Nutrition Facts|Nutrition Section|Nutritional values|Product description|For full nutrition|Serving Size|Allergens?)\b|$)",
        r"\bIngredients?\s*:\s*(.*?)(?=\s+(?:Contains?\s*:|May contain\s*:|Nutrition Facts|Nutrition Section|Nutritional values|Product description|For full nutrition|Serving Size|Allergens?)\b|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            return clean_text(match.group(1)).rstrip(".")
    return ""


def extract_ingredients_from_dom(soup: BeautifulSoup) -> str:
    """Extract ingredient text from pages that render ingredients after a heading."""

    heading_text = {"ingredients", "product ingredients"}
    headings = [
        tag
        for tag in soup.find_all(["h2", "h3", "h4", "h5", "strong", "span", "div", "button"])
        if clean_text(tag.get_text(" ")).lower() in heading_text
    ]

    for heading in reversed(headings):
        for sibling in heading.find_all_next():
            if sibling is heading:
                continue
            text = clean_text(sibling.get_text(" ", strip=True))
            if not text or clean_text(text).lower() in heading_text:
                continue
            direct_match = extract_ingredients_from_text(text)
            if direct_match:
                return direct_match

            lower = text.lower()
            ingredient_like = re.search(
                r"\b(glucose|sugar|flour|oil|salt|gelatin|wheat|corn|soy|rice|potato|canola|milk|dextrose)\b",
                lower,
            )
            has_separators = "," in text or ";" in text
            if ingredient_like and has_separators:
                candidate = re.sub(r"^ingredients?\s*:?\s*", "", text, flags=re.IGNORECASE)
                candidate = re.split(
                    r"\s+(?:Please note|For full nutrition|Nutrition Facts|Nutrition Section|Product packs|Go to slide|Have a question|My friends)\b",
                    candidate,
                    maxsplit=1,
                    flags=re.IGNORECASE,
                )[0]
                return clean_text(candidate).rstrip(".")
    return ""


def extract_allergens_from_text(text: str) -> str | None:
    """Extract contains/may-contain allergen statements from visible text."""

    normalized = clean_text(text)
    statements = []
    patterns = [
        ("CONTAINS", r"(?<!MAY\s)\bCONTAINS?\s*:\s*(.*?)(?=\.\s+|\s+MAY CONTAIN\b|\s+Nutrition Facts\b|\s+Nutrition Section\b|\s+Product description\b|\s+For full nutrition\b|\s+Serving Size\b|$)"),
        ("CONTAINS", r"(?<!MAY\s)\bCONTAINS\s+(?!LESS THAN)(.*?INGREDIENTS?)(?=\.\s+|$)"),
        ("MAY CONTAIN", r"\bMAY CONTAIN\s*:\s*(.*?)(?=\.\s+|\s+Please note\b|\s+Nutrition Facts\b|\s+Nutrition Section\b|\s+Product description\b|\s+For full nutrition\b|\s+Serving Size\b|\s+Go to slide\b|\s+Other\b|$)"),
        ("ALLERGENS", r"\bAllergens?\s*[:|]\s*(.*?)(?=\s+(?:Serving Size|Nutrition Facts|Product description)\b|$)"),
    ]
    for label, pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            value = clean_text(match.group(1)).rstrip(".")
            if value:
                statements.append(f"{label}: {value}")
    return "; ".join(unique_preserve_order(statements)) or None


def detect_certifications(text: str) -> list[str]:
    """Detect common product certifications from visible text."""

    normalized = text.lower()
    checks = {
        "Kosher": r"\bkosher\b",
        "Gluten Free": r"\bgluten[- ]free\b",
        "Non-GMO Project Verified": r"\bnon[- ]gmo project verified\b",
        "Non-GMO": r"\bnon[- ]gmo\b",
        "Vegan": r"\bvegan\b",
        "Halal": r"\bhalal\b",
    }
    return [name for name, pattern in checks.items() if re.search(pattern, normalized)]


def extract_image_url(
    soup: BeautifulSoup,
    base_url: str,
    product_name: str | None = None,
) -> str | None:
    """Extract a likely product image URL from metadata or page images."""

    product_tokens = {
        token.lower()
        for token in re.split(r"[^A-Za-z0-9]+", product_name or "")
        if len(token) > 3 and token.lower() not in {"haribo", "kellogg", "kelloggs"}
    }
    best_image: tuple[int, str] | None = None
    for img in soup.find_all("img"):
        alt = clean_text(img.get("alt"))
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        haystack = f"{alt} {src}".lower()
        if re.search(r"\blogo\b|icon|social", haystack):
            continue
        absolute = urljoin(base_url, src)
        score = 1
        if "/products/" in haystack or "pack" in haystack or "product" in haystack:
            score += 5
        if product_tokens and any(token in haystack for token in product_tokens):
            score += 3
        if re.search(r"about-us|career|seo|contact|headquarter|quality", haystack):
            score -= 4
        if best_image is None or score > best_image[0]:
            best_image = (score, absolute)

    for selector in [
        'meta[property="og:image"]',
        'meta[name="twitter:image"]',
        'meta[property="twitter:image"]',
    ]:
        tag = soup.select_one(selector)
        content = tag.get("content") if tag else None
        if content and "logo" not in content.lower():
            return urljoin(base_url, tag["content"])

    return best_image[1] if best_image else None


def extract_name(soup: BeautifulSoup) -> str:
    """Extract a product name from heading or metadata."""

    for selector in [
        'meta[property="og:title"]',
        'meta[name="twitter:title"]',
    ]:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            return best_title_part(tag["content"])

    for selector in ["h1", "h2"]:
        tag = soup.select_one(selector)
        if tag:
            name = clean_text(tag.get_text(" "))
            if name and not re.search(r"skip to|products?$", name, flags=re.IGNORECASE):
                return name
    title = soup.title.string if soup.title else ""
    return best_title_part(title)


def extract_json_ld_products(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Return JSON-LD Product objects when present."""

    products: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict) and node.get("@graph"):
                nodes.extend([item for item in node["@graph"] if isinstance(item, dict)])
                continue
            if isinstance(node, dict) and str(node.get("@type", "")).lower() == "product":
                products.append(node)
    return products


def extract_pdf_text(content: bytes) -> str:
    """Extract text from a PDF byte stream."""

    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


async def get_page_content(url: str) -> str:
    """Fetch JavaScript-rendered page content with Playwright."""

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(user_agent=USER_AGENT)
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception as exc:
            logging.debug("Continuing after networkidle wait failed for %s: %s", url, exc)
        content = await page.content()
        await browser.close()
        return content


class ManufacturerScraper(ABC):
    """Base class for manufacturer website scrapers."""

    def __init__(self, brand_name: str, base_url: str, delay_seconds: float = 2.5):
        self.brand = brand_name
        normalized_base = base_url if base_url.startswith("http") else f"https://{base_url}"
        parsed_base = urlparse(normalized_base)
        if parsed_base.path and not normalized_base.endswith("/"):
            normalized_base = f"{normalized_base}/"
        self.base_url = normalized_base
        self.delay = delay_seconds
        self.session = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=30,
            follow_redirects=True,
        )

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self.session.aclose()

    async def __aenter__(self) -> "ManufacturerScraper":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @abstractmethod
    async def get_product_urls(self) -> list[str]:
        """Override per brand to return list of product page URLs."""

    @abstractmethod
    async def parse_product_page(self, url: str) -> ProductDict | None:
        """Override per brand to parse a single product page."""

    async def scrape_all(self, limit: int | None = None) -> list[ProductDict]:
        """Run full scrape with rate limiting and return product dicts."""

        urls = await self.get_product_urls()
        results = []
        for url in urls:
            if limit and len(results) >= limit:
                break
            try:
                product = await self.parse_product_page(url)
                if product:
                    results.append(product)
                await asyncio.sleep(self.delay)
            except Exception as exc:
                logging.error("Failed %s: %s", url, exc)
        return results

    async def _can_fetch_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return await can_scrape(parsed.netloc, parsed.path or "/")

    async def fetch_html(self, url: str, use_playwright: bool = False) -> str | None:
        """Fetch page HTML after honoring robots.txt."""

        absolute = urljoin(self.base_url, url)
        if not await self._can_fetch_url(absolute):
            return None

        if use_playwright:
            try:
                return await get_page_content(absolute)
            except Exception as exc:
                logging.warning("Playwright failed for %s, falling back to httpx: %s", absolute, exc)

        try:
            response = await self.session.get(absolute)
            response.raise_for_status()
            return response.text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 405} and not use_playwright:
                try:
                    return await get_page_content(absolute)
                except Exception as browser_exc:
                    logging.warning(
                        "Playwright fallback failed for %s after HTTP %s: %s",
                        absolute,
                        exc.response.status_code,
                        browser_exc,
                    )
            raise

    async def fetch_soup(self, url: str, use_playwright: bool = False) -> BeautifulSoup | None:
        """Fetch and parse a URL as BeautifulSoup."""

        html = await self.fetch_html(url, use_playwright=use_playwright)
        if not html:
            return None
        return BeautifulSoup(html, "html.parser")

    async def fetch_bytes(self, url: str) -> bytes | None:
        """Fetch binary content after honoring robots.txt."""

        absolute = urljoin(self.base_url, url)
        if not await self._can_fetch_url(absolute):
            return None

        response = await self.session.get(absolute)
        response.raise_for_status()
        return response.content

    def _absolute(self, href: str) -> str:
        return urljoin(self.base_url, href)

    def _collect_links(
        self,
        soup: BeautifulSoup,
        include: tuple[str, ...],
        exclude: tuple[str, ...] = (),
        base_url: str | None = None,
    ) -> list[str]:
        urls = []
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            absolute = urljoin(base_url or self.base_url, href)
            lowered = absolute.lower()
            if all(term.lower() in lowered for term in include) and not any(
                term.lower() in lowered for term in exclude
            ):
                urls.append(absolute)
        return unique_preserve_order(urls)

    def build_product(
        self,
        *,
        name: str,
        ingredients_text: str,
        product_url: str,
        image_url: str | None,
        allergens: str | None,
        certifications: list[str],
        barcode: str | None = None,
        brand: str | None = None,
    ) -> ProductDict | None:
        """Build the normalized product payload expected by the seeder."""

        cleaned_ingredients = clean_text(ingredients_text)
        cleaned_name = clean_text(name)
        if not cleaned_name or not cleaned_ingredients:
            logging.warning("Skipping %s because name or ingredients are missing", product_url)
            return None

        return {
            "name": cleaned_name,
            "brand": brand or self.brand,
            "barcode": barcode,
            "ingredients_text": cleaned_ingredients,
            "image_url": image_url,
            "product_url": product_url,
            "allergens": allergens,
            "certifications": unique_preserve_order(certifications),
        }

    def parse_generic_product_page(
        self,
        soup: BeautifulSoup,
        url: str,
        brand: str | None = None,
    ) -> ProductDict | None:
        """Parse a product page with shared heuristics used across brands."""

        page_text = soup.get_text(" ")
        products = extract_json_ld_products(soup)
        json_product = products[0] if products else {}
        name = clean_text(json_product.get("name")) or extract_name(soup)
        image = json_product.get("image")
        if isinstance(image, list):
            image_url = image[0] if image else None
        else:
            image_url = image

        ingredients_text = ""
        for key in ["ingredients", "description"]:
            value = json_product.get(key)
            if isinstance(value, str):
                ingredients_text = extract_ingredients_from_text(value) or ingredients_text
        ingredients_text = (
            ingredients_text
            or extract_ingredients_from_text(page_text)
            or extract_ingredients_from_dom(soup)
        )

        barcode = None
        for key in ["gtin", "gtin8", "gtin12", "gtin13", "gtin14", "sku"]:
            value = json_product.get(key)
            if value:
                barcode = re.sub(r"\D+", "", str(value)) or None
                break

        return self.build_product(
            name=name,
            brand=brand,
            barcode=barcode,
            ingredients_text=ingredients_text,
            image_url=urljoin(url, image_url) if image_url else extract_image_url(soup, url, name),
            product_url=url,
            allergens=extract_allergens_from_text(page_text),
            certifications=detect_certifications(page_text),
        )


class FritoLayScraper(ManufacturerScraper):
    """Scraper for Frito-Lay product pages."""

    FALLBACK_URLS = [
        "https://www.pepsicoproductfacts.com/Home/Product?gtin=00028400323826",
        "https://www.pepsicoproductfacts.com/Home/Product?gtin=00028400325059",
        "https://www.pepsicoproductfacts.com/Home/Product?gtin=00028400314091",
        "https://www.pepsicoproductfacts.com/Home/Product?gtin=00028400327305",
        "https://www.pepsicoproductfacts.com/Home/Product?gtin=00028400090148",
    ]

    def __init__(self, delay_seconds: float = 2.5):
        super().__init__("Frito-Lay", "https://www.fritolay.com", delay_seconds)

    async def get_product_urls(self) -> list[str]:
        soup = await self.fetch_soup("products")
        urls = self._collect_links(soup, ("/products/",), ("/dietary-needs/",)) if soup else []
        if not urls:
            soup = await self.fetch_soup("/products", use_playwright=True)
            urls = self._collect_links(soup, ("/products/",), ("/dietary-needs/",)) if soup else []
        return unique_preserve_order(urls + self.FALLBACK_URLS)

    async def parse_product_page(self, url: str) -> ProductDict | None:
        soup = await self.fetch_soup(url)
        if not soup:
            return None

        if "pepsicoproductfacts.com" in urlparse(url).netloc.lower():
            product = await self._parse_pepsico_product_facts(soup, url)
            if product:
                return product

        product = self.parse_generic_product_page(soup, url)
        if product:
            return product

        # Many Frito-Lay pages link ingredient data out to PepsiCo Product Facts.
        product_facts_links = self._collect_links(
            soup,
            ("pepsicoproductfacts.com",),
            (),
        )
        for facts_url in product_facts_links:
            facts_soup = await self.fetch_soup(facts_url)
            if facts_soup:
                facts_product = self.parse_generic_product_page(facts_soup, facts_url)
                if facts_product:
                    facts_product["brand"] = self.brand
                    facts_product["product_url"] = url
                    if not facts_product.get("image_url"):
                        facts_product["image_url"] = extract_image_url(soup, url)
                    return facts_product
        return None

    async def _parse_pepsico_product_facts(
        self,
        soup: BeautifulSoup,
        url: str,
    ) -> ProductDict | None:
        """Parse PepsiCo Product Facts pages and their PDF fact sheets."""

        label = soup.select_one(".pepsico-nf-label")
        if not label:
            return self.parse_generic_product_page(soup, url, brand=self.brand)

        formula = label.get("data-formula")
        form = label.get("data-form")
        size = label.get("data-size")
        gtin = label.get("data-gtin")
        if not formula or not form or not size:
            return self.parse_generic_product_page(soup, url, brand=self.brand)

        pdf_url = urljoin(
            url,
            f"/Home/PrintPdf?formula={formula}&form={form}&size={size}&web_property_cd=PPF",
        )
        pdf_bytes = await self.fetch_bytes(pdf_url)
        if not pdf_bytes:
            return None

        pdf_text = clean_text(extract_pdf_text(pdf_bytes))
        ingredients_text = extract_ingredients_from_text(pdf_text)
        ingredients_text = re.split(
            r"\s+[A-Z0-9'&., -]{10,}\s+Source:\s*PepsiCoProductFacts|"
            r"\s+Source:\s*PepsiCoProductFacts",
            ingredients_text,
            maxsplit=1,
        )[0]

        page_text = soup.get_text(" ")
        image = soup.select_one("#productImg")
        image_url = None
        if image and image.get("src"):
            image_url = urljoin(url, image["src"])

        return self.build_product(
            name=extract_name(soup),
            brand=self.brand,
            barcode=re.sub(r"\D+", "", gtin or "") or None,
            ingredients_text=ingredients_text,
            image_url=image_url or extract_image_url(soup, url),
            product_url=url,
            allergens=extract_allergens_from_text(pdf_text) or extract_allergens_from_text(page_text),
            certifications=detect_certifications(f"{page_text} {pdf_text}"),
        )


class HariboScraper(ManufacturerScraper):
    """Scraper for HARIBO US products."""

    PRIORITY_KEYWORDS = ("goldbears", "sour-goldbears", "happy-cola", "twin-snakes")
    FALLBACK_URLS = [
        "https://www.haribo.com/en-us/products/goldbears",
        "https://www.haribo.com/en-us/products/sour-goldbears",
        "https://www.haribo.com/en-us/products/happy-cola",
        "https://www.haribo.com/en-us/products/twin-snakes",
    ]

    def __init__(self, delay_seconds: float = 2.5):
        super().__init__("Haribo US", "https://www.haribo.com/en-us", delay_seconds)

    async def get_product_urls(self) -> list[str]:
        soup = await self.fetch_soup("products")
        urls = self._collect_links(soup, ("/en-us/products/",), ()) if soup else []
        priority = [
            url
            for url in urls
            if any(keyword in url.lower() for keyword in self.PRIORITY_KEYWORDS)
        ]
        return unique_preserve_order(priority or self.FALLBACK_URLS)

    async def parse_product_page(self, url: str) -> ProductDict | None:
        soup = await self.fetch_soup(url, use_playwright=True)
        if not soup:
            return None
        return self.parse_generic_product_page(soup, url, brand=self.brand)


class HaraboScraper(HariboScraper):
    """Backward-compatible class name matching the original request typo."""


class NabiscoScraper(ManufacturerScraper):
    """Scraper for Nabisco/Mondelez products hosted on official brand sites."""

    BRAND_PAGES = [
        "https://www.snackworks.com/brands/oreo/products",
        "https://www.snackworks.com/brands/ritz/products",
        "https://www.snackworks.com/brands/chips-ahoy/products",
        "https://www.snackworks.com/brands/wheat-thins/products",
        "https://www.snackworks.com/brands/triscuit/products",
    ]
    TARGET_TERMS = ("oreo", "ritz", "chips-ahoy", "wheat-thins", "triscuit")
    FALLBACK_URLS = [
        "https://www.oreo.com/products/chocolate-sandwich-cookies-family-size-2-pack",
        "https://www.snackworks.com/products/ritz-original-crackers-family-size-206-oz/index.html",
        "https://www.snackworks.com/products/chips-ahoy-original-chocolate-chip-cookies-13-oz/",
        "https://www.snackworks.com/products/wheat-thins-original-snacks-whole-grain-wheat-crackers-snack-crackers-91-oz/index.html",
        "https://www.snackworks.com/products/triscuit-original-whole-grain-wheat-crackers-vegan-crackers-healthy-snacks-lunch-snacks-party-size-17-oz/",
    ]

    def __init__(self, delay_seconds: float = 2.5):
        super().__init__("Nabisco", "https://www.nabisco.com", delay_seconds)

    async def get_product_urls(self) -> list[str]:
        urls = []
        for page in self.BRAND_PAGES:
            soup = await self.fetch_soup(page)
            if not soup:
                continue
            for url in self._collect_links(
                soup,
                ("snackworks.com/products/",),
                ("/recipes/",),
                base_url=page,
            ):
                lowered = url.lower()
                if "original" in lowered or any(term in lowered for term in self.TARGET_TERMS):
                    urls.append(url)
        return unique_preserve_order(self.FALLBACK_URLS + urls)

    async def parse_product_page(self, url: str) -> ProductDict | None:
        soup = await self.fetch_soup(url)
        if not soup:
            return None
        return self.parse_generic_product_page(soup, url, brand=self.brand)


class MondelezScraper(NabiscoScraper):
    """Alias for Mondelez-owned Nabisco brand scraping."""


class KelloggsScraper(ManufacturerScraper):
    """Scraper for Kellogg/Kellanova/WK Kellogg products."""

    FALLBACK_URLS = [
        "https://www.kellanovaus.com/us/en/brands/pringles/pringles-the-original-product.html",
        "https://www.poptarts.com/en_US/products/all-flavors/pop-tarts-frosted-strawberry-product.html",
        "https://www.cheezit.com/en-us/products/cheez-it-original-crackers-product.html",
        "https://www.specialkus.com/en_US/products/cereal/original-cereal.html",
        "https://www.kellanovaus.com/us/en/brands/eggo/eggo-homestyle-waffles.html",
    ]

    def __init__(self, delay_seconds: float = 2.5):
        super().__init__("Kelloggs", "https://www.kelloggs.com", delay_seconds)

    async def get_product_urls(self) -> list[str]:
        return self.FALLBACK_URLS

    async def parse_product_page(self, url: str) -> ProductDict | None:
        soup = await self.fetch_soup(url, use_playwright=True)
        if not soup:
            return None
        return self.parse_generic_product_page(soup, url, brand=self.brand)


SCRAPER_REGISTRY: dict[str, type[ManufacturerScraper]] = {
    "fritolay": FritoLayScraper,
    "frito-lay": FritoLayScraper,
    "haribo": HariboScraper,
    "harabo": HaraboScraper,
    "nabisco": NabiscoScraper,
    "mondelez": MondelezScraper,
    "kelloggs": KelloggsScraper,
    "kellogg": KelloggsScraper,
}


def get_scraper_class(brand: str) -> type[ManufacturerScraper]:
    """Resolve a CLI brand name to a scraper class."""

    key = brand.strip().lower()
    if key not in SCRAPER_REGISTRY:
        choices = ", ".join(sorted(SCRAPER_REGISTRY))
        raise ValueError(f"Unknown brand '{brand}'. Choose one of: {choices}")
    return SCRAPER_REGISTRY[key]


async def run_brand(brand: str, limit: int | None, delay: float) -> list[ProductDict]:
    """Scrape a brand and return normalized products."""

    scraper_class = get_scraper_class(brand)
    async with scraper_class(delay_seconds=delay) as scraper:
        return await scraper.scrape_all(limit=limit)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape manufacturer product pages.")
    parser.add_argument("--brand", required=True, help="Brand scraper to run.")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N scraped products.")
    parser.add_argument("--delay", type=float, default=2.5, help="Seconds between product requests.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print results without saving to Supabase.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(message)s")
    products = await run_brand(args.brand, args.limit, args.delay)
    print(json.dumps(products, indent=2, ensure_ascii=True))
    print(f"Scraped {len(products)} products.")


if __name__ == "__main__":
    asyncio.run(main())
