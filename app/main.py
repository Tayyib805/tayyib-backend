"""FastAPI application entrypoint for the Tayyib API."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import ingredients, inquiries, products, restaurants, scans, users

settings = get_settings()

app = FastAPI(
    title="Tayyib API",
    description="Halal food intelligence platform backend.",
    version="1.0.0",
)

if settings.ENVIRONMENT == "development":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(products.router, prefix="/products", tags=["products"])
app.include_router(ingredients.router, prefix="/ingredients", tags=["ingredients"])
app.include_router(restaurants.router, prefix="/restaurants", tags=["restaurants"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(scans.router, prefix="/scans", tags=["scans"])
app.include_router(inquiries.router, prefix="/inquiries", tags=["inquiries"])


@app.get("/health")
async def health() -> dict[str, str]:
    """Return API health status."""

    return {"status": "ok", "service": "tayyib-api"}


@app.get("/")
async def root() -> dict[str, str]:
    """Return API metadata."""

    return {
        "service": "tayyib-api",
        "name": "Tayyib Halal Food Intelligence API",
        "version": "1.0.0",
    }
