"""
database.py — Async MongoDB connection and data-access layer.

Collections:
  - scheduled_posts   : every scheduled/recurring post job
  - queue_slots       : per-chat slot-based posting queues
  - media_pools       : random-shuffler content pools
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------
_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None

# Collection names
COL_POSTS = "scheduled_posts"
COL_QUEUE_SLOTS = "queue_slots"
COL_MEDIA_POOLS = "media_pools"

# ---------------------------------------------------------------------------
# Valid enum values (used in validation helpers)
# ---------------------------------------------------------------------------
POST_STATUSES = {"pending", "posted", "failed", "paused"}
RECURRENCE_TYPES = {"once", "interval", "days_of_week", "cron", "pool"}
INTERVAL_UNITS = {"minutes", "hours", "days"}
MEDIA_TYPES = {"photo", "video", "document", "audio", "animation", "voice", None}


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

async def connect_db(
    mongo_uri: str,
    db_name: str = "scheduler_bot",
    timeout_ms: int = 5000,
) -> AsyncIOMotorDatabase:
    """
    Connect to MongoDB and return the database handle.
    Raises on connection failure so the caller can decide to abort startup.
    """
    global _client, _db
    try:
        _client = AsyncIOMotorClient(
            mongo_uri,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
            socketTimeoutMS=timeout_ms,
        )
        # Ping to verify connection is alive before accepting traffic
        await _client.admin.command("ping")
        _db = _client[db_name]
        logger.info("Connected to MongoDB — database: '%s'", db_name)
        await _ensure_indexes(_db)
        return _db
    except (ConnectionFailure, ServerSelectionTimeoutError) as exc:
        logger.critical("MongoDB connection failed: %s", exc)
        raise


async def get_db() -> AsyncIOMotorDatabase:
    """Return the active database handle. Raises if not yet initialised."""
    if _db is None:
        raise RuntimeError("Database not initialised — call connect_db() first.")
    return _db


async def close_db() -> None:
    """Gracefully close the MongoDB connection."""
    global _client, _db
    if _client is not None:
        _client.close()
        _client = None
        _db = None
        logger.info("MongoDB connection closed.")


# ---------------------------------------------------------------------------
# Index setup
# ---------------------------------------------------------------------------

async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """Create all required indexes (idempotent — safe to call on every start)."""

    # scheduled_posts
    posts: AsyncIOMotorCollection = db[COL_POSTS]
    await posts.create_indexes([
        IndexModel([("user_id", ASCENDING)]),
        IndexModel([("chat_id", ASCENDING)]),
        IndexModel([("status", ASCENDING)]),
        IndexModel([("recurrence.next_run_at", ASCENDING)]),
        IndexModel([("user_id", ASCENDING), ("status", ASCENDING)]),
    ])

    # queue_slots
    queues: AsyncIOMotorCollection = db[COL_QUEUE_SLOTS]
    await queues.create_indexes([
        IndexModel([("user_id", ASCENDING)]),
        IndexModel([("chat_id", ASCENDING)]),
        IndexModel([("user_id", ASCENDING), ("chat_id", ASCENDING)], unique=True),
    ])

    # media_pools
    pools: AsyncIOMotorCollection = db[COL_MEDIA_POOLS]
    await pools.create_indexes([
        IndexModel([("user_id", ASCENDING)]),
        IndexModel([("chat_id", ASCENDING)]),
        IndexModel([("user_id", ASCENDING), ("chat_id", ASCENDING)], unique=True),
    ])

    logger.info("MongoDB indexes verified/created.")


# ---------------------------------------------------------------------------
# Schema factory helpers
# ---------------------------------------------------------------------------

def build_scheduled_post(
    user_id: int,
    chat_id: int,
    content_text: str | None = None,
    content_media_file_id: str | None = None,
    content_media_type: str | None = None,
    timezone_str: str = "UTC",
    recurrence_type: str = "once",
    interval_value: int | None = None,
    interval_unit: str | None = None,
    days_of_week: list[int] | None = None,
    next_run_at: datetime | None = None,
    cron_expression: str | None = None,
    max_runs: int | None = None,
    time_window_enabled: bool = False,
    time_window_start: str | None = None,
    time_window_end: str | None = None,
    auto_delete_enabled: bool = False,
    auto_delete_after_hours: float | None = None,
    self_destruct_enabled: bool = False,
    self_destruct_after_seconds: int | None = None,
    auto_pin_enabled: bool = False,
    auto_pin_unpin_after_hours: float | None = None,
) -> dict[str, Any]:
    """
    Build a validated scheduled-post document ready to insert into MongoDB.

    Raises ValueError for invalid field combinations so callers get
    immediate feedback before any database write.
    """
    if recurrence_type not in RECURRENCE_TYPES:
        raise ValueError(f"recurrence_type must be one of {RECURRENCE_TYPES}")

    if recurrence_type == "interval":
        if interval_value is None or interval_value <= 0:
            raise ValueError("interval_value must be a positive integer for 'interval' recurrence.")
        if interval_unit not in INTERVAL_UNITS:
            raise ValueError(f"interval_unit must be one of {INTERVAL_UNITS}")

    if recurrence_type == "days_of_week":
        if not days_of_week:
            raise ValueError("days_of_week must be a non-empty list for 'days_of_week' recurrence.")
        invalid_days = [d for d in days_of_week if d not in range(7)]
        if invalid_days:
            raise ValueError("days_of_week values must be integers 0–6 (Mon=0, Sun=6).")

    if time_window_enabled:
        if time_window_start is None or time_window_end is None:
            raise ValueError("time_window_start and time_window_end are required when time_window is enabled.")

    if content_media_type not in MEDIA_TYPES:
        raise ValueError(f"content_media_type must be one of {MEDIA_TYPES}")

    now = datetime.now(tz=timezone.utc)

    return {
        "user_id": user_id,
        "chat_id": chat_id,
        "content": {
            "text": content_text,
            "media_file_id": content_media_file_id,
            "media_type": content_media_type,
        },
        "timezone": timezone_str,
        "status": "pending",
        "max_runs": max_runs,
        "run_count": 0,
        "recurrence": {
            "type": recurrence_type,
            "interval_value": interval_value,
            "interval_unit": interval_unit,
            "days_of_week": days_of_week or [],
            "next_run_at": next_run_at,
            "cron_expression": cron_expression,
        },
        "time_window": {
            "enabled": time_window_enabled,
            "start_time": time_window_start,
            "end_time": time_window_end,
        },
        "lifecycle_settings": {
            "auto_delete": {
                "enabled": auto_delete_enabled,
                "after_hours": auto_delete_after_hours,
            },
            "self_destruct": {
                "enabled": self_destruct_enabled,
                "after_seconds": self_destruct_after_seconds,
            },
            "auto_pin": {
                "enabled": auto_pin_enabled,
                "unpin_after_hours": auto_pin_unpin_after_hours,
            },
        },
        "created_at": now,
        "updated_at": now,
    }


def build_queue_slot_doc(
    user_id: int,
    chat_id: int,
    slots: list[str] | None = None,
    contents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build a queue-slot document.

    - slots    : list of time strings, e.g. ["09:00", "14:00", "20:00"]
    - contents : list of content dicts queued for those slots
    """
    now = datetime.now(tz=timezone.utc)
    return {
        "user_id": user_id,
        "chat_id": chat_id,
        "slots": slots or [],
        "contents": contents or [],
        "created_at": now,
        "updated_at": now,
    }


def build_media_pool_doc(
    user_id: int,
    chat_id: int,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build a media-pool document for the random-shuffler.

    Each item in `items` should be a dict with at least:
      { "text": str|None, "media_file_id": str|None, "media_type": str|None, "posted": bool }
    """
    now = datetime.now(tz=timezone.utc)
    return {
        "user_id": user_id,
        "chat_id": chat_id,
        "items": items or [],
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# CRUD helpers — scheduled_posts
# ---------------------------------------------------------------------------

async def insert_post(post_doc: dict[str, Any]) -> str:
    """Insert a post document and return its string _id."""
    db = await get_db()
    result = await db[COL_POSTS].insert_one(post_doc)
    return str(result.inserted_id)


async def get_post(post_id: str) -> dict[str, Any] | None:
    """Fetch a single post by its string _id."""
    from bson import ObjectId
    db = await get_db()
    return await db[COL_POSTS].find_one({"_id": ObjectId(post_id)})


async def update_post_status(post_id: str, status: str) -> None:
    """Update the status field and bump updated_at."""
    if status not in POST_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {POST_STATUSES}.")
    from bson import ObjectId
    db = await get_db()
    await db[COL_POSTS].update_one(
        {"_id": ObjectId(post_id)},
        {"$set": {"status": status, "updated_at": datetime.now(tz=timezone.utc)}},
    )


async def increment_run_count(post_id: str) -> int:
    """
    Atomically increment run_count and return the new value.
    If the new run_count equals max_runs, status is set to 'paused'.
    """
    from bson import ObjectId
    db = await get_db()
    doc = await db[COL_POSTS].find_one_and_update(
        {"_id": ObjectId(post_id)},
        {
            "$inc": {"run_count": 1},
            "$set": {"updated_at": datetime.now(tz=timezone.utc)},
        },
        return_document=True,
    )
    if doc is None:
        raise ValueError(f"Post {post_id} not found.")
    new_count: int = doc["run_count"]
    max_runs: int | None = doc.get("max_runs")
    if max_runs is not None and new_count >= max_runs:
        await update_post_status(post_id, "paused")
        logger.info("Post %s reached max_runs (%d) — paused.", post_id, max_runs)
    return new_count


async def get_due_posts(now: datetime) -> list[dict[str, Any]]:
    """Return all pending posts whose next_run_at is <= now."""
    db = await get_db()
    cursor = db[COL_POSTS].find({
        "status": "pending",
        "recurrence.next_run_at": {"$lte": now},
    }).sort("recurrence.next_run_at", ASCENDING)
    return await cursor.to_list(length=None)


async def get_user_posts(user_id: int, status: str | None = None) -> list[dict[str, Any]]:
    """Return all posts for a user, optionally filtered by status."""
    db = await get_db()
    query: dict[str, Any] = {"user_id": user_id}
    if status is not None:
        if status not in POST_STATUSES:
            raise ValueError(f"Invalid status '{status}'.")
        query["status"] = status
    cursor = db[COL_POSTS].find(query).sort("created_at", DESCENDING)
    return await cursor.to_list(length=None)


async def delete_post(post_id: str) -> bool:
    """Delete a post. Returns True if a document was deleted."""
    from bson import ObjectId
    db = await get_db()
    result = await db[COL_POSTS].delete_one({"_id": ObjectId(post_id)})
    return result.deleted_count > 0


# ---------------------------------------------------------------------------
# CRUD helpers — queue_slots
# ---------------------------------------------------------------------------

async def upsert_queue_slots(user_id: int, chat_id: int, slots: list[str]) -> None:
    """Set (replace) the daily slot times for a chat."""
    db = await get_db()
    now = datetime.now(tz=timezone.utc)
    await db[COL_QUEUE_SLOTS].update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {
            "$set": {"slots": sorted(slots), "updated_at": now},
            "$setOnInsert": {"contents": [], "created_at": now},
        },
        upsert=True,
    )


async def add_to_queue(user_id: int, chat_id: int, content: dict[str, Any]) -> None:
    """Append a content item to a queue."""
    db = await get_db()
    now = datetime.now(tz=timezone.utc)
    await db[COL_QUEUE_SLOTS].update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {
            "$push": {"contents": content},
            "$set": {"updated_at": now},
            "$setOnInsert": {"slots": [], "created_at": now},
        },
        upsert=True,
    )


async def get_queue(user_id: int, chat_id: int) -> dict[str, Any] | None:
    """Fetch the queue document for a user+chat pair."""
    db = await get_db()
    return await db[COL_QUEUE_SLOTS].find_one({"user_id": user_id, "chat_id": chat_id})


# ---------------------------------------------------------------------------
# CRUD helpers — media_pools (random shuffler)
# ---------------------------------------------------------------------------

async def add_to_media_pool(
    user_id: int,
    chat_id: int,
    item: dict[str, Any],
) -> None:
    """Add a single item to the media pool for a chat."""
    item.setdefault("posted", False)
    db = await get_db()
    now = datetime.now(tz=timezone.utc)
    await db[COL_MEDIA_POOLS].update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {
            "$push": {"items": item},
            "$set": {"updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


async def pick_random_pool_item(
    user_id: int,
    chat_id: int,
) -> dict[str, Any] | None:
    """
    Return a random un-posted item from the pool using MongoDB $sample.
    Returns None when the pool is empty or all items are posted.
    """
    db = await get_db()
    pipeline = [
        {"$match": {"user_id": user_id, "chat_id": chat_id}},
        {"$project": {"items": {"$filter": {
            "input": "$items",
            "as": "item",
            "cond": {"$eq": ["$$item.posted", False]},
        }}}},
        {"$unwind": "$items"},
        {"$sample": {"size": 1}},
        {"$replaceRoot": {"newRoot": "$items"}},
    ]
    results = await db[COL_MEDIA_POOLS].aggregate(pipeline).to_list(length=1)
    return results[0] if results else None


async def mark_pool_item_posted(
    user_id: int,
    chat_id: int,
    item_media_file_id: str | None,
    item_text: str | None,
) -> None:
    """Mark a specific pool item as posted (matched by file_id or text)."""
    db = await get_db()
    now = datetime.now(tz=timezone.utc)
    match_field = "items.media_file_id" if item_media_file_id else "items.text"
    match_value = item_media_file_id if item_media_file_id else item_text
    await db[COL_MEDIA_POOLS].update_one(
        {"user_id": user_id, "chat_id": chat_id, match_field: match_value},
        {
            "$set": {"items.$.posted": True, "updated_at": now},
        },
    )


async def reset_media_pool(user_id: int, chat_id: int) -> None:
    """Mark all items in a pool as un-posted (full cycle reset)."""
    db = await get_db()
    now = datetime.now(tz=timezone.utc)
    await db[COL_MEDIA_POOLS].update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {
            "$set": {"updated_at": now},
            "$set": {"items.$[].posted": False},
        },
    )
