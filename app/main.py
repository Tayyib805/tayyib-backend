"""FastAPI application entrypoint for the Tayyib API."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import articles, ingredients, inquiries, leaderboard, maintenance, products, push, refutes, restaurants, scans, spotchecks, users

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
for _noisy in ("httpx", "httpcore", "hpack", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Log whether ingredient analysis works, without delaying startup."""

    from app.services.claude_analysis import self_check

    task = asyncio.create_task(self_check())
    yield
    task.cancel()


app = FastAPI(
    lifespan=lifespan,
    title="Tayyib API",
    description="Halal food intelligence platform backend.",
    version="1.0.0",
)

# Auth uses Bearer tokens, not cookies, so credentials mode is not needed.
# CORS_ORIGINS is a comma-separated allowlist; it defaults to "*" so the
# Lovable preview keeps working. Set it to your real domains before launch.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.CORS_ORIGINS.split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(spotchecks.router, prefix="/products", tags=["spot checks"])
app.include_router(products.router, prefix="/products", tags=["products"])
app.include_router(ingredients.router, prefix="/ingredients", tags=["ingredients"])
app.include_router(restaurants.router, prefix="/restaurants", tags=["restaurants"])
app.include_router(users.router, prefix="/users", tags=["users"])
app.include_router(scans.router, prefix="/scans", tags=["scans"])
app.include_router(inquiries.router, prefix="/inquiries", tags=["inquiries"])
app.include_router(refutes.router, prefix="/refutes", tags=["refutes"])
app.include_router(articles.router, prefix="/articles", tags=["articles"])
app.include_router(push.router, prefix="/push", tags=["push"])
app.include_router(leaderboard.router, prefix="/leaderboard", tags=["leaderboard"])
app.include_router(maintenance.router)


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
