"""Learn articles: reading, likes, threaded comments, reports, and admin publishing.

Articles are written by people (admins) and published deliberately. Comments
are moderated: rate limited, cleaned, auto-hidden after three reports, and
served without user ids or emails.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from supabase import Client

from app.database import get_current_user, get_optional_user, get_supabase, is_admin
from app.ratelimit import rate_limit
from app.services.article_text import (
    clean_comment,
    clean_display_name,
    clean_sources,
    comment_problem,
    public_name,
    read_minutes,
    slugify,
)

router = APIRouter()

LIST_FIELDS = (
    "id,slug,title,summary,category,ingredient_tag,author_name,author_role,"
    "reviewed_by,read_minutes,published_at,like_count,comment_count,status"
)
HIDE_AFTER_REPORTS = 3
COMMENTS_PER_USER_PER_DAY = 40

Category = Literal["ingredients", "rulings", "labels", "community", "news"]


class Source(BaseModel):
    title: str = Field(max_length=200)
    url: str = Field(max_length=500)


class ArticleIn(BaseModel):
    title: str = Field(min_length=3, max_length=140)
    summary: str | None = Field(default=None, max_length=280)
    body: str = Field(default="", max_length=40_000)
    category: Category = "ingredients"
    ingredient_tag: str | None = Field(default=None, max_length=40)
    author_name: str | None = Field(default=None, max_length=80)
    author_role: str | None = Field(default=None, max_length=80)
    reviewed_by: str | None = Field(default=None, max_length=120)
    sources: list[Source] = Field(default_factory=list, max_length=12)
    status: Literal["draft", "published", "archived"] = "draft"


class CommentIn(BaseModel):
    body: str = Field(max_length=2000)
    parent_id: str | None = None
    # Optional public name; saved to the profile for next time.
    display_name: str | None = Field(default=None, max_length=60)


class ReportIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_admin(user: dict, supabase: Client) -> None:
    if not is_admin(user["id"], supabase):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only.")


def _unique_slug(supabase: Client, title: str, exclude_id: str | None = None) -> str:
    base = slugify(title)
    taken = {
        row["slug"]
        for row in (
            supabase.table("articles").select("id,slug").like("slug", f"{base}%").execute()
        ).data
        or []
        if row["id"] != exclude_id
    }
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def _article_row(payload: ArticleIn) -> dict:
    data = payload.model_dump()
    data["sources"] = clean_sources(data.get("sources"))
    for key in ("summary", "ingredient_tag", "author_name", "author_role", "reviewed_by"):
        value = (data.get(key) or "").strip()
        data[key] = value or None
    data["title"] = data["title"].strip()
    data["read_minutes"] = read_minutes(data.get("body") or "")
    return data


def _get_published(supabase: Client, article_id: str) -> dict:
    rows = (
        supabase.table("articles")
        .select("id,status,created_by")
        .eq("id", article_id)
        .limit(1)
        .execute()
    ).data
    if not rows or rows[0]["status"] != "published":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")
    return rows[0]


def _safe_query(q: str | None) -> str | None:
    """Keep search text to letters, digits, spaces and hyphens (PostgREST-safe)."""

    if not q:
        return None
    cleaned = re.sub(r"[^\w\s-]", " ", q, flags=re.UNICODE).strip()[:60]
    return cleaned or None


# ── Reading ────────────────────────────────────────────────────────────────


@router.get("")
async def list_articles(
    category: Category | None = None,
    q: str | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Published articles, newest first."""

    query = supabase.table("articles").select(LIST_FIELDS).eq("status", "published")
    if category:
        query = query.eq("category", category)
    term = _safe_query(q)
    if term:
        query = query.or_(f"title.ilike.%{term}%,summary.ilike.%{term}%,ingredient_tag.ilike.%{term}%")
    rows = query.order("published_at", desc=True).range(offset, offset + limit - 1).execute().data or []
    return {"items": rows, "limit": limit, "offset": offset}


@router.get("/admin/all")
async def admin_list_articles(
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Every article including drafts, for the editor."""

    _require_admin(user, supabase)
    rows = supabase.table("articles").select(LIST_FIELDS + ",updated_at").order("updated_at", desc=True).execute().data or []
    return {"items": rows}


@router.get("/admin/reported")
async def admin_reported_comments(
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Comments with reports, most reported first."""

    _require_admin(user, supabase)
    rows = (
        supabase.table("article_comments")
        .select("id,article_id,display_name,body,is_hidden,report_count,created_at")
        .gt("report_count", 0)
        .order("report_count", desc=True)
        .limit(100)
        .execute()
    ).data or []
    return {"items": rows}


@router.get("/{slug}")
async def read_article(
    slug: str,
    user: dict | None = Depends(get_optional_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """One article with the reader's like state. Admins can preview drafts."""

    rows = supabase.table("articles").select("*").eq("slug", slug).limit(1).execute().data
    admin = bool(user) and is_admin(user["id"], supabase)
    if not rows or (rows[0]["status"] != "published" and not admin):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")
    article = rows[0]
    article.pop("created_by", None)

    liked = False
    if user:
        liked = bool(
            supabase.table("article_likes")
            .select("article_id")
            .eq("article_id", article["id"])
            .eq("user_id", user["id"])
            .limit(1)
            .execute()
            .data
        )
    return {**article, "liked": liked, "can_edit": admin}


# ── Likes ──────────────────────────────────────────────────────────────────


@router.post("/{article_id}/like", dependencies=[Depends(rate_limit("article_like", 30, 60))])
async def toggle_like(
    article_id: str,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Like or unlike an article."""

    _get_published(supabase, article_id)
    existing = (
        supabase.table("article_likes")
        .select("article_id")
        .eq("article_id", article_id)
        .eq("user_id", user["id"])
        .limit(1)
        .execute()
    ).data
    if existing:
        supabase.table("article_likes").delete().eq("article_id", article_id).eq("user_id", user["id"]).execute()
    else:
        supabase.table("article_likes").insert({"article_id": article_id, "user_id": user["id"]}).execute()
    count = supabase.table("articles").select("like_count").eq("id", article_id).limit(1).execute().data
    return {"liked": not existing, "like_count": (count[0]["like_count"] if count else 0)}


# ── Comments ───────────────────────────────────────────────────────────────


def _present_comment(row: dict, viewer_id: str | None, author_id: str | None) -> dict:
    deleted = row.get("is_deleted")
    return {
        "id": row["id"],
        "parent_id": row.get("parent_id"),
        "display_name": "Deleted" if deleted else row["display_name"],
        "body": "" if deleted else row["body"],
        "deleted": bool(deleted),
        "created_at": row["created_at"],
        "is_mine": bool(viewer_id) and row["user_id"] == viewer_id and not deleted,
        "is_author": bool(author_id) and row["user_id"] == author_id and not deleted,
    }


@router.get("/{article_id}/comments")
async def list_comments(
    article_id: str,
    user: dict | None = Depends(get_optional_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Visible comments, oldest first; replies carry their parent_id."""

    article = _get_published(supabase, article_id)
    rows = (
        supabase.table("article_comments")
        .select("id,parent_id,user_id,display_name,body,is_deleted,created_at")
        .eq("article_id", article_id)
        .eq("is_hidden", False)
        .order("created_at")
        .limit(500)
        .execute()
    ).data or []
    viewer = user["id"] if user else None
    return {"items": [_present_comment(r, viewer, article.get("created_by")) for r in rows]}


@router.post(
    "/{article_id}/comments",
    dependencies=[
        Depends(rate_limit("comment_minute", 5, 60)),
        Depends(rate_limit("comment_day", 60, 86_400)),
    ],
)
async def add_comment(
    article_id: str,
    payload: CommentIn,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Post a comment or a reply (replies are one level deep)."""

    article = _get_published(supabase, article_id)
    body = clean_comment(payload.body)
    problem = comment_problem(body)
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    recent = (
        supabase.table("article_comments")
        .select("body,created_at")
        .eq("user_id", user["id"])
        .gte("created_at", since)
        .order("created_at", desc=True)
        .limit(COMMENTS_PER_USER_PER_DAY)
        .execute()
    ).data or []
    if len(recent) >= COMMENTS_PER_USER_PER_DAY:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="You've reached today's comment limit.")
    if any(r["body"] == body for r in recent[:10]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You already posted that.")

    parent_id = None
    if payload.parent_id:
        parent = (
            supabase.table("article_comments")
            .select("id,article_id,parent_id,is_hidden")
            .eq("id", payload.parent_id)
            .limit(1)
            .execute()
        ).data
        if not parent or parent[0]["article_id"] != article_id or parent[0]["is_hidden"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That comment is no longer available.")
        # Keep threads one level deep: a reply to a reply joins the top-level thread.
        parent_id = parent[0]["parent_id"] or parent[0]["id"]

    profile = supabase.table("profiles").select("display_name").eq("id", user["id"]).limit(1).execute().data
    chosen = clean_display_name(payload.display_name)
    if payload.display_name and not chosen:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Names can be 2–40 characters, without emails or links.",
        )
    if chosen:
        supabase.table("profiles").update({"display_name": chosen}).eq("id", user["id"]).execute()
    name = chosen or public_name(profile[0].get("display_name") if profile else None, user.get("email"))

    row = (
        supabase.table("article_comments")
        .insert(
            {
                "article_id": article_id,
                "user_id": user["id"],
                "parent_id": parent_id,
                "display_name": name,
                "body": body,
            }
        )
        .execute()
    ).data
    if not row:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Couldn't post your comment.")
    return _present_comment(row[0], user["id"], article.get("created_by"))


@router.delete("/comments/{comment_id}")
async def delete_comment(
    comment_id: str,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Delete your own comment (admins can delete any)."""

    rows = supabase.table("article_comments").select("id,user_id").eq("id", comment_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found.")
    if rows[0]["user_id"] != user["id"] and not is_admin(user["id"], supabase):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete your own comments.")

    has_replies = (
        supabase.table("article_comments").select("id").eq("parent_id", comment_id).limit(1).execute().data
    )
    if has_replies:
        # Keep the thread readable: leave a placeholder instead of removing replies.
        supabase.table("article_comments").update({"is_deleted": True, "body": "[deleted]"}).eq("id", comment_id).execute()
    else:
        supabase.table("article_comments").delete().eq("id", comment_id).execute()
    return {"deleted": True}


@router.post("/comments/{comment_id}/report", dependencies=[Depends(rate_limit("comment_report", 10, 3600))])
async def report_comment(
    comment_id: str,
    payload: ReportIn,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Report a comment. Three reports hide it until an admin reviews it."""

    rows = supabase.table("article_comments").select("id,user_id").eq("id", comment_id).limit(1).execute().data
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found.")
    if rows[0]["user_id"] == user["id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You can't report your own comment.")

    supabase.table("comment_reports").upsert(
        {"comment_id": comment_id, "user_id": user["id"], "reason": (payload.reason or "").strip() or None},
        on_conflict="comment_id,user_id",
    ).execute()
    reports = (
        supabase.table("comment_reports").select("user_id", count="exact").eq("comment_id", comment_id).limit(1).execute()
    ).count or 0
    update = {"report_count": reports}
    if reports >= HIDE_AFTER_REPORTS:
        update["is_hidden"] = True
    supabase.table("article_comments").update(update).eq("id", comment_id).execute()
    return {"reported": True}


@router.post("/comments/{comment_id}/restore")
async def restore_comment(
    comment_id: str,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Admin: un-hide a reported comment and clear its reports."""

    _require_admin(user, supabase)
    supabase.table("comment_reports").delete().eq("comment_id", comment_id).execute()
    supabase.table("article_comments").update({"is_hidden": False, "report_count": 0}).eq("id", comment_id).execute()
    return {"restored": True}


# ── Publishing (admins) ────────────────────────────────────────────────────


@router.post("")
async def create_article(
    payload: ArticleIn,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Create an article (draft or published)."""

    _require_admin(user, supabase)
    data = _article_row(payload)
    data["slug"] = _unique_slug(supabase, data["title"])
    data["created_by"] = user["id"]
    if data["status"] == "published":
        data["published_at"] = _now()
    row = supabase.table("articles").insert(data).execute().data
    if not row:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Couldn't save the article.")
    return row[0]


@router.put("/{article_id}")
async def update_article(
    article_id: str,
    payload: ArticleIn,
    user: dict = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict:
    """Update an article. The slug follows the title until first publication."""

    _require_admin(user, supabase)
    current = (
        supabase.table("articles").select("id,status,published_at").eq("id", article_id).limit(1).execute()
    ).data
    if not current:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Article not found.")

    data = _article_row(payload)
    data["updated_at"] = _now()
    if not current[0]["published_at"]:
        data["slug"] = _unique_slug(supabase, data["title"], exclude_id=article_id)
        if data["status"] == "published":
            data["published_at"] = _now()
    row = supabase.table("articles").update(data).eq("id", article_id).execute().data
    return row[0] if row else {"id": article_id, **data}
