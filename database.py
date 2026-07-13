"""
database.py — Async MongoDB connection and data-access layer.

Collections:
  - scheduled_posts   : every scheduled/recurring post job
  - queue_slots       : per-chat slot-based posting queues
  - media_pools       : random-shuffler content pools
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
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
COL_USERS = "bot_users"
COL_GROUPS = "bot_groups"
COL_ACTIVITY = "chat_activity"

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
    """
    Create all required indexes (idempotent — safe to call on every start).

    Storage-efficiency strategy:
    - TTL index on scheduled_posts.expire_at  → MongoDB auto-deletes posted/failed docs
      after 30 days without any application code.
    - TTL index on lifecycle_tasks.expire_at  → completed tasks auto-deleted after 24 h.
    - Compound indexes cover the hottest query paths so full-collection scans never happen.
    """

    # ── scheduled_posts ──────────────────────────────────────────────────────
    posts: AsyncIOMotorCollection = db[COL_POSTS]
    await posts.create_indexes([
        IndexModel([("user_id", ASCENDING)]),
        IndexModel([("chat_id", ASCENDING)]),
        IndexModel([("status", ASCENDING)]),
        IndexModel([("recurrence.next_run_at", ASCENDING)]),
        IndexModel([("user_id", ASCENDING), ("status", ASCENDING)]),
        # TTL: MongoDB removes the document automatically when expire_at is reached.
        # We set expire_at = now + 30 days whenever status → posted / failed.
        IndexModel(
            [("expire_at", ASCENDING)],
            expireAfterSeconds=0,   # fire exactly at expire_at
            sparse=True,            # ignore docs that don't have expire_at yet
            name="ttl_posts_expire",
        ),
    ])

    # ── queue_slots ───────────────────────────────────────────────────────────
    queues: AsyncIOMotorCollection = db[COL_QUEUE_SLOTS]
    await queues.create_indexes([
        IndexModel([("user_id", ASCENDING)]),
        IndexModel([("chat_id", ASCENDING)]),
        IndexModel([("user_id", ASCENDING), ("chat_id", ASCENDING)], unique=True),
    ])

    # ── media_pools ───────────────────────────────────────────────────────────
    pools: AsyncIOMotorCollection = db[COL_MEDIA_POOLS]
    await pools.create_indexes([
        IndexModel([("user_id", ASCENDING)]),
        IndexModel([("chat_id", ASCENDING)]),
        IndexModel([("user_id", ASCENDING), ("chat_id", ASCENDING)], unique=True),
    ])

    # ── lifecycle_tasks ───────────────────────────────────────────────────────
    tasks: AsyncIOMotorCollection = db["lifecycle_tasks"]
    await tasks.create_indexes([
        IndexModel([("done", ASCENDING)]),
        IndexModel([("fire_at", ASCENDING)]),
        # TTL: auto-delete completed tasks 24 h after they fired.
        # expire_at is set to fire_at + 1 day when the task is stored.
        IndexModel(
            [("expire_at", ASCENDING)],
            expireAfterSeconds=0,
            sparse=True,
            name="ttl_tasks_expire",
        ),
    ])

    # ── bot_users / bot_groups (owner-panel tracking) ───────────────────────
    users: AsyncIOMotorCollection = db[COL_USERS]
    await users.create_indexes([
        IndexModel([("user_id", ASCENDING)], unique=True),
    ])

    groups: AsyncIOMotorCollection = db[COL_GROUPS]
    await groups.create_indexes([
        IndexModel([("chat_id", ASCENDING)], unique=True),
    ])

    # ── chat_activity (smart scheduling heatmap) ─────────────────────────────
    activity: AsyncIOMotorCollection = db[COL_ACTIVITY]
    await activity.create_indexes([
        IndexModel([("chat_id", ASCENDING)]),
        IndexModel([("chat_id", ASCENDING), ("hour", ASCENDING)], unique=True),
    ])

    logger.info("MongoDB indexes verified/created.")


# ---------------------------------------------------------------------------
# Storage-efficiency helpers
# ---------------------------------------------------------------------------

POST_TTL_DAYS = 30   # keep posted/failed posts for 30 days then auto-delete
TASK_TTL_HOURS = 24  # keep completed lifecycle tasks for 24 h then auto-delete


def _post_expire_at(days: int = POST_TTL_DAYS) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(days=days)


def _task_expire_at(hours: int = TASK_TTL_HOURS) -> datetime:
    return datetime.now(tz=timezone.utc) + timedelta(hours=hours)


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
    inline_keyboard: list[list[dict]] | None = None,
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
            "inline_keyboard": inline_keyboard or [],
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
    """
    Update the status field and bump updated_at.
    When status becomes 'posted' or 'failed', also stamp expire_at so the
    TTL index can auto-delete the document after POST_TTL_DAYS days.
    """
    if status not in POST_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {POST_STATUSES}.")
    from bson import ObjectId
    db  = await get_db()
    now = datetime.now(tz=timezone.utc)
    fields: dict = {"status": status, "updated_at": now}
    if status in ("posted", "failed", "paused"):
        fields["expire_at"] = _post_expire_at()   # TTL fires in 30 days
    await db[COL_POSTS].update_one(
        {"_id": ObjectId(post_id)},
        {"$set": fields},
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
    """
    Mark all items in a pool as un-posted (full cycle reset).

    Both fields go inside a SINGLE $set dict.  Two separate "$set" keys in
    the same Python dict causes the first one to be silently overwritten by
    the second before the document ever reaches the driver.
    """
    db = await get_db()
    now = datetime.now(tz=timezone.utc)
    await db[COL_MEDIA_POOLS].update_one(
        {"user_id": user_id, "chat_id": chat_id},
        {
            "$set": {
                "items.$[].posted": False,
                "updated_at": now,
            },
        },
    )


# ---------------------------------------------------------------------------
# Owner-panel tracking & tools — bot_users / bot_groups
# ---------------------------------------------------------------------------

async def upsert_user(user_id: int, username: str | None, first_name: str | None) -> None:
    """Record/refresh a known user (called opportunistically on any update)."""
    db = await get_db()
    now = datetime.now(tz=timezone.utc)
    await db[COL_USERS].update_one(
        {"user_id": user_id},
        {
            "$set": {
                "username": username,
                "first_name": first_name,
                "last_seen_at": now,
            },
            "$setOnInsert": {"first_seen_at": now},
        },
        upsert=True,
    )


async def upsert_group(chat_id: int, title: str | None, chat_type: str) -> None:
    """Record/refresh a known group/channel (called opportunistically on any update)."""
    db = await get_db()
    now = datetime.now(tz=timezone.utc)
    await db[COL_GROUPS].update_one(
        {"chat_id": chat_id},
        {
            "$set": {
                "title": title,
                "chat_type": chat_type,
                "last_seen_at": now,
            },
            "$setOnInsert": {"first_seen_at": now},
        },
        upsert=True,
    )


async def remove_group(chat_id: int) -> None:
    """Drop a group record (e.g. the bot was kicked/removed)."""
    db = await get_db()
    await db[COL_GROUPS].delete_one({"chat_id": chat_id})


async def list_user_ids() -> list[int]:
    db = await get_db()
    cursor = db[COL_USERS].find({}, {"user_id": 1})
    return [doc["user_id"] async for doc in cursor]


async def list_group_ids() -> list[int]:
    db = await get_db()
    cursor = db[COL_GROUPS].find({}, {"chat_id": 1})
    return [doc["chat_id"] async for doc in cursor]


async def list_users() -> list[dict]:
    """Full records (id + username + first_name) for the owner panel's User List."""
    db = await get_db()
    cursor = db[COL_USERS].find({}, {"user_id": 1, "username": 1, "first_name": 1})
    return [doc async for doc in cursor]


async def list_groups() -> list[dict]:
    """Full records (id + title + chat_type) for the owner panel's Group List."""
    db = await get_db()
    cursor = db[COL_GROUPS].find({}, {"chat_id": 1, "title": 1, "chat_type": 1})
    return [doc async for doc in cursor]


async def get_bot_stats() -> dict[str, int]:
    """Aggregate counts used by the owner panel's Status screen."""
    db = await get_db()
    users_count = await db[COL_USERS].count_documents({})
    groups_count = await db[COL_GROUPS].count_documents({})
    posts_total = await db[COL_POSTS].count_documents({})
    pending = await db[COL_POSTS].count_documents({"status": "pending"})
    paused = await db[COL_POSTS].count_documents({"status": "paused"})
    posted = await db[COL_POSTS].count_documents({"status": "posted"})
    failed = await db[COL_POSTS].count_documents({"status": "failed"})
    queues = await db[COL_QUEUE_SLOTS].count_documents({})
    pools = await db[COL_MEDIA_POOLS].count_documents({})
    return {
        "users": users_count,
        "groups": groups_count,
        "posts_total": posts_total,
        "posts_pending": pending,
        "posts_paused": paused,
        "posts_posted": posted,
        "posts_failed": failed,
        "queues": queues,
        "pools": pools,
    }


async def record_chat_activity(chat_id: int, hour: int) -> None:
    """Increment the message count for a chat/hour bucket (0–23 UTC)."""
    db = await get_db()
    await db[COL_ACTIVITY].update_one(
        {"chat_id": chat_id, "hour": hour},
        {"$inc": {"count": 1}},
        upsert=True,
    )


async def get_best_hours(chat_id: int, top_n: int = 3) -> list[dict]:
    """
    Return the top-N busiest hours for a chat, sorted by message count desc.
    Each item: {"hour": int, "count": int}.  Empty list if no data yet.
    """
    db = await get_db()
    cursor = db[COL_ACTIVITY].find(
        {"chat_id": chat_id},
        {"hour": 1, "count": 1},
    ).sort("count", -1).limit(top_n)
    return [{"hour": doc["hour"], "count": doc["count"]} async for doc in cursor]


async def clear_all_bot_data() -> dict[str, int]:
    """
    Owner-only hard reset: wipes ALL scheduled posts, queue slots, and media
    pools. Does NOT delete the tracked users/groups lists (those are needed
    to keep broadcasting working). Returns the number of documents removed
    from each collection so the caller can report exact numbers.
    """
    db = await get_db()
    posts_res = await db[COL_POSTS].delete_many({})
    queues_res = await db[COL_QUEUE_SLOTS].delete_many({})
    pools_res = await db[COL_MEDIA_POOLS].delete_many({})
    return {
        "posts": posts_res.deleted_count,
        "queues": queues_res.deleted_count,
        "pools": pools_res.deleted_count,
    }
