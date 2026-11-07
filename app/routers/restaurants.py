"""Restaurant discovery and review endpoints."""

from math import asin, cos, radians, sin, sqrt

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client

from app.database import get_current_user, get_supabase
from app.models.restaurant import RestaurantCreate, RestaurantReviewCreate

router = APIRouter()


def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate haversine distance between two points in kilometers."""

    earth_radius_km = 6371
    delta_lat = radians(lat2 - lat1)
    delta_lng = radians(lng2 - lng1)
    a = (
        sin(delta_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(delta_lng / 2) ** 2
    )
    return 2 * earth_radius_km * asin(sqrt(a))


@router.get("/nearby")
async def nearby_restaurants(
    lat: float,
    lng: float,
    radius_km: float = 5,
    supabase: Client = Depends(get_supabase),
) -> list[dict]:
    """Return restaurants inside a simple bounding box with calculated distance."""

    lat_delta = radius_km / 111.0
    lng_delta = radius_km / (111.0 * max(cos(radians(lat)), 0.01))
    response = (
        supabase.table("restaurants")
        .select("*")
        .gte("lat", lat - lat_delta)
        .lte("lat", lat + lat_delta)
        .gte("lng", lng - lng_delta)
        .lte("lng", lng + lng_delta)
        .execute()
    )

    restaurants = []
    for restaurant in response.data or []:
        distance = _distance_km(lat, lng, float(restaurant["lat"]), float(restaurant["lng"]))
        if distance <= radius_km:
            restaurant["distance_km"] = round(distance, 2)
            restaurants.append(restaurant)
    return sorted(restaurants, key=lambda item: item["distance_km"])


@router.get("/{restaurant_id}")
async def get_restaurant(
    restaurant_id: str,
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Return a restaurant with reviews."""

    response = (
        supabase.table("restaurants")
        .select("*, restaurant_reviews(*)")
        .eq("id", restaurant_id)
        .limit(1)
        .execute()
    )
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Restaurant not found.",
        )
    return response.data[0]


@router.post("")
async def create_restaurant(
    payload: RestaurantCreate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Create a restaurant listing."""

    del user
    listing = payload.model_dump()
    # Users can suggest a listing, but only an admin can mark it certified or halal.
    listing["halal_status"] = "unverified"
    response = supabase.table("restaurants").insert(listing).execute()
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create restaurant.",
        )
    return response.data[0]


@router.post("/{restaurant_id}/reviews")
async def add_restaurant_review(
    restaurant_id: str,
    payload: RestaurantReviewCreate,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Add a restaurant review and update average rating."""

    review_payload = payload.model_dump()
    review_payload.update({"restaurant_id": restaurant_id, "user_id": user["id"]})
    review = supabase.table("restaurant_reviews").insert(review_payload).execute()
    if not review.data:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create review.",
        )

    reviews = (
        supabase.table("restaurant_reviews")
        .select("rating")
        .eq("restaurant_id", restaurant_id)
        .execute()
    )
    ratings = [int(item["rating"]) for item in reviews.data or []]
    avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
    supabase.table("restaurants").update(
        {"avg_rating": avg_rating, "review_count": len(ratings)}
    ).eq("id", restaurant_id).execute()
    return review.data[0]
