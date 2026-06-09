"""Priority manufacturer brands for Tayyib scrape seeding."""

PRIORITY_BRANDS = [
    {
        "brand": "Frito-Lay",
        "scraper": "FritoLayScraper",
        "priority": 1,
        "note": "Lay's, Cheetos, Doritos, Fritos, Ruffles - highest US Muslim scan volume",
    },
    {
        "brand": "Haribo US",
        "scraper": "HaraboScraper",
        "priority": 1,
        "note": "Most searched halal question in Muslim community - pork gelatin concern",
    },
    {
        "brand": "Nabisco",
        "scraper": "NabiscoScraper",
        "priority": 1,
        "note": "Oreo, Ritz - frequent uncertain ingredient questions",
    },
    {
        "brand": "Kelloggs",
        "scraper": "KelloggsScraper",
        "priority": 2,
        "note": "Cereals and snacks",
    },
]

