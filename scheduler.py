"""
scheduler.py — APScheduler engine that polls MongoDB and fires scheduled posts.

Tick cadence : every 60 seconds (configurable via SCHEDULER_INTERVAL_SECS)
Handles      : one-time, interval, days-of-week, and pool-based posts
Lifecycle    : auto-delete, self-destruct, auto-pin/unpin
Error policy : Forbidden/ChatNotFound → status='failed'
               Network/transient      → log + retry next tick
               max_runs reached       → status='paused' (via increment_run_count)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot, Message
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from database import (
    get_db,
    get_due_posts,
    update_post_status,
    increment_run_count,
    pick_random_pool_item,
    mark_pool_item_posted,
    reset_media_pool,
    COL_POSTS,
    COL_MEDIA_POOLS,
)

logger = logging.getLogger(__name__)

SCHEDULER_INTERVAL_SECS = 60   # how often to poll for due posts

# ---------------------------------------------------------------------------
# Time-window check
# ---------------------------------------------------------------------------

def _is_within_window(post: dict[str, Any], now_utc: datetime) -> bool:
    """
    Return True if `now_utc` falls within the post's time window (or window is disabled).
    Window times are stored as "HH:MM" strings and interpreted in the post's timezone.
    """
    tw = post.get("time_window", {})
    if not tw.get("enabled"):
        return True

    start_str: str | None = tw.get("start_time")
    end_str:   str | None = tw.get("end_time")
    if not start_str or not end_str:
        return True

    tz_str: str = post.get("timezone", "UTC")
    try:
        zone = pytz.timezone(tz_str)
    except pytz.exceptions.UnknownTimeZoneError:
        zone = timezone.utc

    local_now = now_utc.astimezone(zone)
    current_hhmm = f"{local_now.hour:02d}:{local_now.minute:02d}"
    return start_str <= current_hhmm <= end_str


# ---------------------------------------------------------------------------
# Next-run-at computation
# ---------------------------------------------------------------------------

def _compute_next_run(post: dict[str, Any], fired_at: datetime) -> datetime | None:
    """
    Given a post that just fired at `fired_at`, return the next UTC datetime to fire,
    or None if the post should not fire again (one-time / pool exhausted).
    """
    rec = post.get("recurrence", {})
    rec_type: str = rec.get("type", "once")

    if rec_type == "once":
        return None

    tz_str: str = post.get("timezone", "UTC")
    try:
        zone: Any = pytz.timezone(tz_str)
    except pytz.exceptions.UnknownTimeZoneError:
        zone = timezone.utc

    if rec_type == "interval":
        unit: str      = rec.get("interval_unit", "hours")
        value: int     = rec.get("interval_value", 1)
        delta_map      = {"minutes": timedelta(minutes=value),
                          "hours":   timedelta(hours=value),
                          "days":    timedelta(days=value)}
        delta = delta_map.get(unit, timedelta(hours=value))
        return fired_at + delta

    if rec_type == "days_of_week":
        days: list[int] = rec.get("days_of_week", [])
        if not days:
            return None
        local_fired = fired_at.astimezone(zone)
        # Preserve the HH:MM from the original next_run_at
        original_run: datetime | None = rec.get("next_run_at")
        if isinstance(original_run, datetime):
            local_orig = original_run.astimezone(zone)
            h, mi = local_orig.hour, local_orig.minute
        else:
            h, mi = local_fired.hour, local_fired.minute

        # Find the next occurrence among the selected weekdays
        for offset in range(1, 8):
            candidate = local_fired + timedelta(days=offset)
            if candidate.weekday() in days:
                next_local = candidate.replace(hour=h, minute=mi, second=0, microsecond=0)
                if isinstance(zone, pytz.BaseTzInfo):
                    return zone.normalize(zone.localize(next_local.replace(tzinfo=None))).astimezone(timezone.utc)
                return next_local.replace(tzinfo=timezone.utc)

    if rec_type == "pool":
        # Pool posts recur on a fixed interval; next_run uses the same interval logic
        unit  = rec.get("interval_unit", "hours")
        value = rec.get("interval_value", 24)
        delta_map = {"minutes": timedelta(minutes=value),
                     "hours":   timedelta(hours=value),
                     "days":    timedelta(days=value)}
        return fired_at + delta_map.get(unit, timedelta(hours=value))

    return None


# ---------------------------------------------------------------------------
# Sending helpers
# ---------------------------------------------------------------------------

async def _send_post(bot: Bot, post: dict[str, Any]) -> Message | None:
    """
    Send the post content to the target chat.
    Returns the sent Message, or None on unrecoverable error.
    Raises re-raiseable exceptions for transient failures so the caller can log them.
    """
    chat_id: int       = post["chat_id"]
    content: dict      = post.get("content", {})
    text: str | None   = content.get("text")
    file_id: str | None = content.get("media_file_id")
    media_type: str | None = content.get("media_type")

    try:
        if media_type == "photo":
            return await bot.send_photo(chat_id, file_id, caption=text)
        if media_type == "video":
            return await bot.send_video(chat_id, file_id, caption=text)
        if media_type == "document":
            return await bot.send_document(chat_id, file_id, caption=text)
        if media_type == "audio":
            return await bot.send_audio(chat_id, file_id, caption=text)
        if media_type == "animation":
            return await bot.send_animation(chat_id, file_id, caption=text)
        if media_type == "voice":
            return await bot.send_voice(chat_id, file_id, caption=text)
        # Default: plain text
        if text:
            return await bot.send_message(chat_id, text)
        logger.warning("Post %s has neither media nor text — skipping.", post.get("_id"))
        return None

    except Forbidden as exc:
        logger.warning("Bot forbidden in chat %s (post %s): %s", chat_id, post.get("_id"), exc)
        raise
    except BadRequest as exc:
        logger.warning("BadRequest for post %s in chat %s: %s", post.get("_id"), chat_id, exc)
        raise
    except (NetworkError, TimedOut) as exc:
        logger.warning("Transient network error for post %s: %s", post.get("_id"), exc)
        raise
    except RetryAfter as exc:
        logger.warning("Flood control hit — retry after %ss", exc.retry_after)
        raise


# ---------------------------------------------------------------------------
# Lifecycle actions (fire-and-forget with their own error handling)
# ---------------------------------------------------------------------------

async def _handle_lifecycle(bot: Bot, post: dict[str, Any], sent_msg: Message) -> None:
    """Pin, schedule auto-delete, and schedule self-destruct for a sent message."""
    chat_id     = sent_msg.chat_id
    message_id  = sent_msg.message_id
    lifecycle   = post.get("lifecycle_settings", {})

    # Auto-pin
    ap = lifecycle.get("auto_pin", {})
    if ap.get("enabled"):
        try:
            await bot.pin_chat_message(chat_id, message_id, disable_notification=True)
            logger.info("Pinned message %s in chat %s", message_id, chat_id)
        except (Forbidden, BadRequest) as exc:
            logger.warning("Could not pin message %s: %s", message_id, exc)

        unpin_hrs: float | None = ap.get("unpin_after_hours")
        if unpin_hrs and unpin_hrs > 0:
            # Schedule unpin — stored in DB so it survives restarts
            fire_at = datetime.now(tz=timezone.utc) + timedelta(hours=unpin_hrs)
            await _store_lifecycle_task(
                chat_id=chat_id, message_id=message_id,
                action="unpin", fire_at=fire_at,
            )

    # Auto-delete
    ad = lifecycle.get("auto_delete", {})
    if ad.get("enabled"):
        delete_hrs: float | None = ad.get("after_hours")
        if delete_hrs and delete_hrs > 0:
            fire_at = datetime.now(tz=timezone.utc) + timedelta(hours=delete_hrs)
            await _store_lifecycle_task(
                chat_id=chat_id, message_id=message_id,
                action="delete", fire_at=fire_at,
            )

    # Self-destruct (takes priority over auto_delete — fires sooner)
    sd = lifecycle.get("self_destruct", {})
    if sd.get("enabled"):
        sd_secs: int | None = sd.get("after_seconds")
        if sd_secs and sd_secs > 0:
            fire_at = datetime.now(tz=timezone.utc) + timedelta(seconds=sd_secs)
            await _store_lifecycle_task(
                chat_id=chat_id, message_id=message_id,
                action="delete", fire_at=fire_at,
            )


async def _store_lifecycle_task(
    chat_id: int, message_id: int, action: str, fire_at: datetime
) -> None:
    """
    Persist a lifecycle task to MongoDB so it survives bot restarts.
    expire_at = fire_at + 24 h — the TTL index auto-deletes the doc
    once it is no longer needed, keeping the collection small.
    """
    from database import _task_expire_at
    db = await get_db()
    await db["lifecycle_tasks"].insert_one({
        "chat_id":    chat_id,
        "message_id": message_id,
        "action":     action,
        "fire_at":    fire_at,
        "done":       False,
        "expire_at":  _task_expire_at(),   # auto-deleted 24 h after fire_at
    })


async def _run_lifecycle_tasks(bot: Bot) -> None:
    """Process any overdue lifecycle tasks (unpin / delete)."""
    db  = await get_db()
    now = datetime.now(tz=timezone.utc)
    cursor = db["lifecycle_tasks"].find({"done": False, "fire_at": {"$lte": now}})
    tasks = await cursor.to_list(length=None)

    for task in tasks:
        chat_id    = task["chat_id"]
        message_id = task["message_id"]
        action     = task["action"]
        try:
            if action == "delete":
                await bot.delete_message(chat_id, message_id)
                logger.info("Deleted message %s in chat %s (lifecycle)", message_id, chat_id)
            elif action == "unpin":
                await bot.unpin_chat_message(chat_id, message_id)
                logger.info("Unpinned message %s in chat %s (lifecycle)", message_id, chat_id)
        except (Forbidden, BadRequest) as exc:
            logger.warning("Lifecycle %s failed for msg %s: %s", action, message_id, exc)
        except (NetworkError, TimedOut) as exc:
            logger.warning("Transient error during lifecycle %s: %s", action, exc)
            continue   # leave task marked undone — retry next tick
        finally:
            await db["lifecycle_tasks"].update_one(
                {"_id": task["_id"]}, {"$set": {"done": True}}
            )


# ---------------------------------------------------------------------------
# Pool-post executor
# ---------------------------------------------------------------------------

async def _execute_pool_post(bot: Bot, post: dict[str, Any]) -> None:
    """Pick a random un-posted pool item and send it."""
    user_id: int = post["user_id"]
    chat_id: int = post["chat_id"]
    post_id = str(post["_id"])

    item = await pick_random_pool_item(user_id, chat_id)

    if item is None:
        # All items posted — auto-reset and pick again
        logger.info("Pool exhausted for user %s chat %s — resetting.", user_id, chat_id)
        await reset_media_pool(user_id, chat_id)
        item = await pick_random_pool_item(user_id, chat_id)

    if item is None:
        # Pool is completely empty
        logger.warning("Pool is empty for user %s chat %s — pausing pool post.", user_id, chat_id)
        await update_post_status(post_id, "paused")
        return

    # Build a transient post doc just for sending
    ephemeral = dict(post)
    ephemeral["content"] = {
        "text":          item.get("text"),
        "media_file_id": item.get("media_file_id"),
        "media_type":    item.get("media_type"),
    }

    try:
        sent_msg = await _send_post(bot, ephemeral)
    except (Forbidden, BadRequest):
        await update_post_status(post_id, "failed")
        return
    except (NetworkError, TimedOut):
        return   # retry next tick

    if sent_msg is None:
        return

    # Mark item as posted
    await mark_pool_item_posted(user_id, chat_id, item.get("media_file_id"), item.get("text"))

    # Handle lifecycle
    await _handle_lifecycle(bot, post, sent_msg)

    # Compute & store next run
    now = datetime.now(tz=timezone.utc)
    new_count = await increment_run_count(post_id)
    next_run  = _compute_next_run(post, now)
    if next_run:
        db = await get_db()
        await db[COL_POSTS].update_one(
            {"_id": post["_id"]},
            {"$set": {
                "recurrence.next_run_at": next_run,
                "updated_at": now,
            }},
        )
    else:
        await update_post_status(post_id, "posted")


# ---------------------------------------------------------------------------
# Main scheduler tick
# ---------------------------------------------------------------------------

async def scheduler_tick(bot: Bot) -> None:
    """
    Called every SCHEDULER_INTERVAL_SECS seconds.
    1. Process overdue lifecycle tasks.
    2. Fetch all pending posts whose next_run_at <= now.
    3. Execute each one, updating status / next_run_at in MongoDB.
    """
    now = datetime.now(tz=timezone.utc)
    logger.debug("Scheduler tick at %s", now.isoformat())

    try:
        await _run_lifecycle_tasks(bot)
    except Exception as exc:
        logger.exception("Error in lifecycle tasks: %s", exc)

    try:
        due_posts = await get_due_posts(now)
    except Exception as exc:
        logger.exception("Error fetching due posts: %s", exc)
        return

    if due_posts:
        logger.info("Scheduler: %d due post(s) to process.", len(due_posts))

    for post in due_posts:
        post_id = str(post["_id"])
        rec_type = post.get("recurrence", {}).get("type", "once")

        # ── Time-window check ──
        if not _is_within_window(post, now):
            logger.info(
                "Post %s skipped — outside time window at %s",
                post_id, now.strftime("%H:%M"),
            )
            # For recurring posts bump next_run_at so we don't spam the log
            if rec_type not in ("once",):
                next_run = _compute_next_run(post, now)
                if next_run:
                    db = await get_db()
                    await db[COL_POSTS].update_one(
                        {"_id": post["_id"]},
                        {"$set": {"recurrence.next_run_at": next_run, "updated_at": now}},
                    )
            continue

        # ── Pool posts ──
        if rec_type == "pool":
            try:
                await _execute_pool_post(bot, post)
            except Exception as exc:
                logger.exception("Unhandled error in pool post %s: %s", post_id, exc)
            continue

        # ── Regular post ──
        try:
            sent_msg = await _send_post(bot, post)
        except Forbidden:
            logger.error("Bot forbidden for post %s — marking failed.", post_id)
            await update_post_status(post_id, "failed")
            continue
        except BadRequest as exc:
            err = str(exc).lower()
            if "not enough rights" in err or "chat not found" in err or "peer_id_invalid" in err:
                logger.error("Permanent error for post %s (%s) — marking failed.", post_id, exc)
                await update_post_status(post_id, "failed")
            else:
                logger.warning("BadRequest for post %s — will retry: %s", post_id, exc)
            continue
        except ChatMigrated as exc:
            # Update chat_id and retry next tick
            new_chat_id: int = exc.new_chat_id
            logger.info("Chat migrated for post %s — new ID: %s", post_id, new_chat_id)
            db = await get_db()
            await db[COL_POSTS].update_one(
                {"_id": post["_id"]},
                {"$set": {"chat_id": new_chat_id, "updated_at": now}},
            )
            continue
        except (NetworkError, TimedOut, RetryAfter):
            logger.warning("Transient error for post %s — will retry.", post_id)
            continue
        except Exception as exc:
            logger.exception("Unexpected error for post %s: %s", post_id, exc)
            continue

        if sent_msg is None:
            continue

        # ── Lifecycle actions ──
        try:
            await _handle_lifecycle(bot, post, sent_msg)
        except Exception as exc:
            logger.exception("Lifecycle error for post %s: %s", post_id, exc)

        # ── Increment run count (auto-pauses when max_runs is hit) ──
        try:
            await increment_run_count(post_id)
        except Exception as exc:
            logger.exception("increment_run_count failed for %s: %s", post_id, exc)

        # ── Compute & store next run (or mark as posted) ──
        next_run = _compute_next_run(post, now)
        if next_run and rec_type != "once":
            try:
                db = await get_db()
                await db[COL_POSTS].update_one(
                    {"_id": post["_id"]},
                    {"$set": {
                        "recurrence.next_run_at": next_run,
                        "updated_at": now,
                    }},
                )
                logger.info("Post %s next run scheduled for %s", post_id, next_run.isoformat())
            except Exception as exc:
                logger.exception("Failed to update next_run_at for post %s: %s", post_id, exc)
        else:
            try:
                await update_post_status(post_id, "posted")
                logger.info("Post %s completed (one-time).", post_id)
            except Exception as exc:
                logger.exception("Failed to mark post %s as posted: %s", post_id, exc)


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def setup_scheduler(bot: Bot, event_loop: Any = None) -> AsyncIOScheduler:
    """
    Create, configure, and return the AsyncIOScheduler.
    The scheduler is NOT started here — call .start() in main.py post_init.

    Pass `event_loop` (the running asyncio loop) so APScheduler and PTB
    share exactly the same loop. Without it, APScheduler may bind to a
    different loop in Python 3.10+ and silently fail to run async jobs.
    """
    kwargs: dict[str, Any] = {"timezone": timezone.utc}
    if event_loop is not None:
        kwargs["event_loop"] = event_loop
    scheduler = AsyncIOScheduler(**kwargs)

    scheduler.add_job(
        scheduler_tick,
        trigger="interval",
        seconds=SCHEDULER_INTERVAL_SECS,
        args=[bot],
        id="main_tick",
        max_instances=1,          # prevent overlapping ticks
        misfire_grace_time=30,    # tolerate up to 30s of delay before skipping
        coalesce=True,            # collapse missed fires into one
    )

    logger.info(
        "Scheduler configured — tick every %ds", SCHEDULER_INTERVAL_SECS
    )
    return scheduler
