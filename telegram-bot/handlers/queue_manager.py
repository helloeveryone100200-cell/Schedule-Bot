"""
handlers/queue_manager.py — Two fully independent sub-systems:

  A) QUEUE MANAGER (slot-based)
     • Set named daily time slots for a chat
     • Drop content in → auto-assigned to next free chronological slot
     • View / clear the queue

  B) MEDIA POOL (random shuffler)
     • Add multiple content items to a pool
     • Bot picks one randomly at each scheduled interval
     • Pool resets automatically once all items are posted
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import (
    build_scheduled_post,
    insert_post,
    upsert_queue_slots,
    add_to_queue,
    get_queue,
    add_to_media_pool,
    pick_random_pool_item,
    reset_media_pool,
    build_queue_slot_doc,
    build_media_pool_doc,
    get_db,
    COL_QUEUE_SLOTS,
    COL_MEDIA_POOLS,
)
from handlers.base import CB_CANCEL, CB_MAIN_MENU, cmd_cancel, nav_keyboard, confirm_keyboard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
(
    QM_MENU,
    QM_CHAT_ID,
    QM_SET_SLOTS,
    QM_ADD_CONTENT,
    QM_ADD_CHAT_ID,
    QM_CONFIRM_SLOT,
) = range(100, 106)

(
    MP_MENU,
    MP_CHAT_ID,
    MP_ADD_ITEM,
    MP_SET_INTERVAL_VALUE,
    MP_SET_INTERVAL_UNIT,
    MP_SET_FIRST_RUN,
    MP_SET_TIMEZONE,
    MP_CONFIRM,
) = range(200, 208)

# ---------------------------------------------------------------------------
# Callback-data constants  (all ≤ 64 bytes)
# ---------------------------------------------------------------------------
CB_MANAGE_QUEUE  = "action:manage_queue"
CB_MEDIA_POOL    = "action:media_pool"

# Queue manager
CB_QM_SET_SLOTS    = "qm:set_slots"
CB_QM_ADD_CONTENT  = "qm:add_content"
CB_QM_VIEW         = "qm:view"
CB_QM_CLEAR        = "qm:clear"
CB_QM_CLEAR_YES    = "qm:clear_yes"
CB_QM_CONFIRM_ADD  = "qm:confirm_add"

# Media pool
CB_MP_ADD_ITEM     = "mp:add"
CB_MP_VIEW         = "mp:view"
CB_MP_RESET        = "mp:reset"
CB_MP_RESET_YES    = "mp:reset_yes"
CB_MP_CLEAR        = "mp:clear"
CB_MP_CLEAR_YES    = "mp:clear_yes"
CB_MP_CONFIRM      = "mp:confirm"
CB_MP_IU_MINUTES   = "mp:iu:minutes"
CB_MP_IU_HOURS     = "mp:iu:hours"
CB_MP_IU_DAYS      = "mp:iu:days"

# Back targets
CB_BACK_QM_MENU    = "back:qm_menu"
CB_BACK_MP_MENU    = "back:mp_menu"
CB_BACK_MP_IV      = "back:mp_iv"
CB_BACK_MP_IU      = "back:mp_iu"
CB_BACK_MP_FR      = "back:mp_fr"

# Timezone constants (reuse same set as wizard)
CB_MP_TZ_PAGE = "mptz:page:"
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

TIMEZONES: list[tuple[str, str]] = [
    ("UTC",              "UTC"),
    ("Asia/Kolkata",     "GMT+5:30 India"),
    ("Asia/Dhaka",       "GMT+6:00 Bangladesh"),
    ("Asia/Rangoon",     "GMT+6:30 Myanmar"),
    ("Asia/Bangkok",     "GMT+7:00 Thailand"),
    ("Asia/Singapore",   "GMT+8:00 Singapore"),
    ("Asia/Tokyo",       "GMT+9:00 Japan"),
    ("Asia/Seoul",       "GMT+9:00 Korea"),
    ("Europe/London",    "GMT+0/+1 London"),
    ("Europe/Berlin",    "GMT+1/+2 Berlin"),
    ("America/New_York", "GMT-5/-4 New York"),
    ("America/Chicago",  "GMT-6/-5 Chicago"),
    ("America/Denver",   "GMT-7/-6 Denver"),
    ("America/Los_Angeles", "GMT-8/-7 Los Angeles"),
    ("Australia/Sydney", "GMT+10/+11 Sydney"),
]
TZ_PAGE_SIZE = 5


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _w(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return context.user_data.setdefault("qwiz", {})


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("qwiz", None)


def _pm(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    return context.user_data.setdefault("mpwiz", {})


def _clear_mp(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mpwiz", None)


async def _edit(query: Any, text: str, keyboard: InlineKeyboardMarkup) -> None:
    await query.answer()
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except Exception:
        # edit_message_text can fail if the message is too old or already deleted;
        # fall back to a fresh message so the user is never left without a prompt.
        try:
            await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        except Exception:
            logger.exception("_edit fallback reply_text also failed")


def _parse_hhmm(text: str) -> str | None:
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def _parse_slots(raw: str) -> list[str] | None:
    """
    Parse a comma/newline/space separated list of HH:MM time strings.
    Returns sorted unique valid slots, or None if any token is invalid.
    """
    tokens = re.split(r"[\s,;]+", raw.strip())
    tokens = [t.strip() for t in tokens if t.strip()]
    if not tokens:
        return None
    parsed: list[str] = []
    for t in tokens:
        p = _parse_hhmm(t)
        if p is None:
            return None
        parsed.append(p)
    return sorted(set(parsed))


def _next_slot_datetime(slots: list[str], tz_str: str = "UTC") -> tuple[str, datetime] | None:
    """
    Find the next slot time that is strictly in the future.
    Returns (slot_label, aware_datetime) or None if slots is empty.
    """
    if not slots:
        return None
    try:
        zone = pytz.timezone(tz_str) if tz_str != "UTC" else timezone.utc
    except pytz.exceptions.UnknownTimeZoneError:
        zone = timezone.utc

    now = datetime.now(tz=zone)
    candidates: list[tuple[datetime, str]] = []

    for slot in slots:
        h, mi = int(slot[:2]), int(slot[3:])
        candidate = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append((candidate, slot))

    candidates.sort(key=lambda x: x[0])
    dt, label = candidates[0]
    return label, dt


def _mp_interval_unit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ Minutes", callback_data=CB_MP_IU_MINUTES),
            InlineKeyboardButton("🕐 Hours",   callback_data=CB_MP_IU_HOURS),
            InlineKeyboardButton("📅 Days",    callback_data=CB_MP_IU_DAYS),
        ],
        [
            InlineKeyboardButton("⬅️ Back",   callback_data=CB_BACK_MP_IV),
            InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
        ],
    ])


def _mp_tz_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    start = page * TZ_PAGE_SIZE
    end   = start + TZ_PAGE_SIZE
    rows = [
        [InlineKeyboardButton(label, callback_data=f"mptz:{tz}")]
        for tz, label in TIMEZONES[start:end]
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{CB_MP_TZ_PAGE}{page - 1}"))
    if end < len(TIMEZONES):
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"{CB_MP_TZ_PAGE}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("⬅️ Back",   callback_data=CB_BACK_MP_FR),
        InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
    ])
    return InlineKeyboardMarkup(rows)


# ============================================================================
#  A) QUEUE MANAGER
# ============================================================================

def _qm_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ Set Daily Slots",         callback_data=CB_QM_SET_SLOTS)],
        [InlineKeyboardButton("➕ Add Content to Queue",    callback_data=CB_QM_ADD_CONTENT)],
        [InlineKeyboardButton("👁 View Queue",              callback_data=CB_QM_VIEW)],
        [InlineKeyboardButton("🗑 Clear Queue",             callback_data=CB_QM_CLEAR)],
        [
            InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU),
            InlineKeyboardButton("❌ Cancel",    callback_data=CB_CANCEL),
        ],
    ])


async def enter_queue_manager(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _clear(context)
    await _edit(query,
        "🗂 *Queue Manager*\n\n"
        "Set up daily posting slots and auto-fill them with content.\n\n"
        "📌 *How it works:*\n"
        "1. Set daily time slots (e.g. 09:00, 14:00, 20:00)\n"
        "2. Add content — it fills the next available slot automatically\n"
        "3. The bot fires each piece of content at its assigned time",
        _qm_menu_keyboard(),
    )
    return QM_MENU


# ── Set Slots ──

async def cb_qm_set_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "⏰ *Set Daily Slots*\n\n"
        "Type the times you want to post each day, separated by commas or spaces.\n\n"
        "Example: `09:00, 14:00, 20:00`\n\n"
        "Send your chat/channel ID first, then the slots:\n"
        "Format: `CHAT_ID: 09:00, 14:00, 20:00`\n"
        "Example: `-1001234567890: 09:00, 14:30, 21:00`",
        nav_keyboard(back_data=CB_BACK_QM_MENU),
    )
    return QM_SET_SLOTS


async def recv_qm_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    # Parse "CHAT_ID: HH:MM, HH:MM, ..."
    if ":" not in text:
        await update.message.reply_text(
            "⚠️ Format: `CHAT_ID: HH:MM, HH:MM, ...`\n"
            "Example: `-1001234567890: 09:00, 14:00, 20:00`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return QM_SET_SLOTS

    chat_part, _, slots_part = text.partition(":")
    # Handle negative chat IDs like -1001234567890
    # The partition on first ':' may split a negative ID — rejoin if needed
    # Detect if slots_part itself starts with digits (means chat_id was cut)
    chat_part = chat_part.strip()
    # Remove the extra colon from time strings that get split
    slots_raw = slots_part.strip()

    try:
        chat_id = int(chat_part)
    except ValueError:
        await update.message.reply_text(
            "⚠️ Invalid Chat ID. Make sure format is:\n`CHAT_ID: 09:00, 14:00`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return QM_SET_SLOTS

    slots = _parse_slots(slots_raw)
    if not slots:
        await update.message.reply_text(
            "⚠️ Could not parse any valid time slots.\n"
            "Use `HH:MM` format, e.g. `09:00, 14:30, 21:00`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return QM_SET_SLOTS

    user_id: int = update.message.from_user.id  # type: ignore[union-attr]
    try:
        await upsert_queue_slots(user_id, chat_id, slots)
    except Exception as exc:
        logger.exception("upsert_queue_slots failed: %s", exc)
        await update.message.reply_text(f"❌ Database error: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        return QM_MENU

    slot_display = "  •  ".join(slots)
    await update.message.reply_text(
        f"✅ *Daily slots saved for chat `{chat_id}`*\n\n"
        f"⏰ Slots: `{slot_display}`\n\n"
        "Now use *Add Content to Queue* to fill them!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_qm_menu_keyboard(),
    )
    return QM_MENU


# ── Add Content to Queue ──

async def cb_qm_add_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "➕ *Add Content to Queue*\n\n"
        "First, type the *Chat ID* of the target chat:",
        nav_keyboard(back_data=CB_BACK_QM_MENU),
    )
    return QM_ADD_CHAT_ID


async def recv_qm_add_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    try:
        chat_id = int(text)
    except ValueError:
        await update.message.reply_text("⚠️ Enter a valid numeric Chat ID.", parse_mode=ParseMode.MARKDOWN)
        return QM_ADD_CHAT_ID

    _w(context)["add_chat_id"] = chat_id
    await update.message.reply_text(
        f"✅ Chat: `{chat_id}`\n\n"
        "📝 Now send the content you want to add to the queue.\n"
        "(Text, photo, video, document, etc.)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=nav_keyboard(back_data=CB_BACK_QM_MENU),
    )
    return QM_ADD_CONTENT


async def recv_qm_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    w   = _w(context)

    # Extract content
    if msg.photo:
        w["media_file_id"] = msg.photo[-1].file_id;  w["media_type"] = "photo";    w["content_text"] = msg.caption
    elif msg.video:
        w["media_file_id"] = msg.video.file_id;      w["media_type"] = "video";    w["content_text"] = msg.caption
    elif msg.document:
        w["media_file_id"] = msg.document.file_id;   w["media_type"] = "document"; w["content_text"] = msg.caption
    elif msg.audio:
        w["media_file_id"] = msg.audio.file_id;      w["media_type"] = "audio";    w["content_text"] = msg.caption
    elif msg.animation:
        w["media_file_id"] = msg.animation.file_id;  w["media_type"] = "animation";w["content_text"] = msg.caption
    elif msg.voice:
        w["media_file_id"] = msg.voice.file_id;      w["media_type"] = "voice";    w["content_text"] = msg.caption
    elif msg.text:
        w["media_file_id"] = None; w["media_type"] = None; w["content_text"] = msg.text
    else:
        await msg.reply_text("⚠️ Unsupported content type. Please send text or media.")
        return QM_ADD_CONTENT

    user_id: int = msg.from_user.id  # type: ignore[union-attr]
    chat_id: int = w["add_chat_id"]

    # Look up existing slots
    queue_doc = await get_queue(user_id, chat_id)
    slots: list[str] = queue_doc["slots"] if queue_doc and queue_doc.get("slots") else []

    if not slots:
        await msg.reply_text(
            "⚠️ No slots configured for this chat yet.\n"
            "Use *Set Daily Slots* first to define posting times.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_qm_menu_keyboard(),
        )
        return QM_MENU

    result = _next_slot_datetime(slots)
    if result is None:
        await msg.reply_text("⚠️ No upcoming slot found.", parse_mode=ParseMode.MARKDOWN)
        return QM_MENU

    slot_label, slot_dt = result
    w["assigned_slot"]   = slot_label
    w["assigned_slot_dt"] = slot_dt.isoformat()

    content_preview = (w.get("content_text") or "")[:60]
    if w.get("media_type"):
        content_preview = f"[{w['media_type'].upper()}] {content_preview}"

    await msg.reply_text(
        f"📋 *Queue Assignment Preview*\n\n"
        f"🗂 Chat: `{chat_id}`\n"
        f"📝 Content: `{content_preview or '—'}`\n"
        f"⏰ Next slot: `{slot_label}` (next occurrence)\n\n"
        "Tap *✅ Confirm* to add to queue:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=confirm_keyboard(CB_QM_CONFIRM_ADD, CB_BACK_QM_MENU),
    )
    return QM_CONFIRM_SLOT


async def cb_qm_confirm_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Adding to queue…")
    w = _w(context)
    user_id: int = query.from_user.id  # type: ignore[union-attr]
    chat_id: int = w["add_chat_id"]

    content_item = {
        "text":          w.get("content_text"),
        "media_file_id": w.get("media_file_id"),
        "media_type":    w.get("media_type"),
        "slot":          w.get("assigned_slot"),
        "scheduled_for": w.get("assigned_slot_dt"),
        "posted":        False,
    }

    try:
        await add_to_queue(user_id, chat_id, content_item)
        # Also create a formal scheduled_post document so the scheduler can fire it
        from datetime import datetime as _dt
        slot_dt = _dt.fromisoformat(w["assigned_slot_dt"])
        doc = build_scheduled_post(
            user_id=user_id,
            chat_id=chat_id,
            content_text=w.get("content_text"),
            content_media_file_id=w.get("media_file_id"),
            content_media_type=w.get("media_type"),
            recurrence_type="once",
            next_run_at=slot_dt,
            max_runs=1,
        )
        post_id = await insert_post(doc)
    except Exception as exc:
        logger.exception("Queue add failed: %s", exc)
        await query.edit_message_text(f"❌ Error: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        _clear(context)
        return ConversationHandler.END

    _clear(context)
    await query.edit_message_text(
        f"✅ *Added to queue!*\n\n"
        f"⏰ Will post at slot `{w.get('assigned_slot')}`\n"
        f"🆔 Post ID: `{post_id}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_qm_menu_keyboard(),
    )
    return QM_MENU


# ── View Queue ──

async def cb_qm_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id: int = query.from_user.id  # type: ignore[union-attr]

    try:
        db = await get_db()
        cursor = db[COL_QUEUE_SLOTS].find({"user_id": user_id})
        docs = await cursor.to_list(length=50)
    except Exception as exc:
        await query.edit_message_text(f"❌ Error fetching queue: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        return QM_MENU

    if not docs:
        await query.edit_message_text(
            "📋 *Your Queue*\n\nNo queues set up yet.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_qm_menu_keyboard(),
        )
        return QM_MENU

    lines = ["📋 *Your Queues*\n"]
    for doc in docs:
        slots   = doc.get("slots", [])
        contents = doc.get("contents", [])
        pending  = sum(1 for c in contents if not c.get("posted", False))
        lines.append(
            f"🗂 Chat `{doc['chat_id']}`\n"
            f"  ⏰ Slots: `{'  •  '.join(slots) or 'none'}`\n"
            f"  📝 Items in queue: `{len(contents)}` ({pending} pending)\n"
        )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_qm_menu_keyboard(),
    )
    return QM_MENU


# ── Clear Queue ──

async def cb_qm_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "🗑 *Clear Queue*\n\n"
        "⚠️ This will delete *all* queued content for all your chats.\n"
        "Are you sure?",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, clear all", callback_data=CB_QM_CLEAR_YES),
                InlineKeyboardButton("⬅️ Back",           callback_data=CB_BACK_QM_MENU),
            ],
        ]),
    )
    return QM_MENU


async def cb_qm_clear_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Clearing…")
    user_id: int = query.from_user.id  # type: ignore[union-attr]

    try:
        db = await get_db()
        result = await db[COL_QUEUE_SLOTS].delete_many({"user_id": user_id})
        count = result.deleted_count
    except Exception as exc:
        await query.edit_message_text(f"❌ Error: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        return QM_MENU

    await query.edit_message_text(
        f"✅ Cleared `{count}` queue(s).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_qm_menu_keyboard(),
    )
    return QM_MENU


# ── Back to QM menu ──

async def cb_back_qm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "🗂 *Queue Manager*\n\nWhat would you like to do?",
        _qm_menu_keyboard(),
    )
    return QM_MENU


# ============================================================================
#  B) MEDIA POOL (Random Shuffler)
# ============================================================================

def _mp_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Item to Pool",    callback_data=CB_MP_ADD_ITEM)],
        [InlineKeyboardButton("👁 View Pool Stats",    callback_data=CB_MP_VIEW)],
        [InlineKeyboardButton("🔄 Reset Pool",         callback_data=CB_MP_RESET)],
        [InlineKeyboardButton("🗑 Clear Pool",         callback_data=CB_MP_CLEAR)],
        [
            InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU),
            InlineKeyboardButton("❌ Cancel",    callback_data=CB_CANCEL),
        ],
    ])


async def enter_media_pool(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    _clear_mp(context)
    await _edit(query,
        "🎲 *Media Pool — Random Shuffler*\n\n"
        "Upload multiple items to a pool. At each scheduled interval the bot picks "
        "one un-posted item at random.\n\n"
        "📌 *How it works:*\n"
        "1. Add items to the pool (text, photos, videos, etc.)\n"
        "2. Set a posting interval (every X hours, etc.)\n"
        "3. The bot posts a random un-posted item each time\n"
        "4. When all items are posted the pool resets automatically",
        _mp_menu_keyboard(),
    )
    return MP_MENU


# ── Add Item ──

async def cb_mp_add_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "➕ *Add Item to Pool*\n\n"
        "First, type the *Chat ID* of the channel/group for this pool:",
        nav_keyboard(back_data=CB_BACK_MP_MENU),
    )
    return MP_CHAT_ID


async def recv_mp_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    try:
        chat_id = int(text)
    except ValueError:
        await update.message.reply_text("⚠️ Enter a valid numeric Chat ID.", parse_mode=ParseMode.MARKDOWN)
        return MP_CHAT_ID

    _pm(context)["mp_chat_id"] = chat_id
    await update.message.reply_text(
        f"✅ Chat: `{chat_id}`\n\n"
        "🎲 Now send the content item to add to the pool.\n"
        "(Text, photo, video, document, audio — all supported.)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=nav_keyboard(back_data=CB_BACK_MP_MENU),
    )
    return MP_ADD_ITEM


async def recv_mp_item(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    pm  = _pm(context)

    item: dict[str, Any] = {"posted": False}

    if msg.photo:
        item.update(media_file_id=msg.photo[-1].file_id, media_type="photo",    text=msg.caption)
    elif msg.video:
        item.update(media_file_id=msg.video.file_id,     media_type="video",    text=msg.caption)
    elif msg.document:
        item.update(media_file_id=msg.document.file_id,  media_type="document", text=msg.caption)
    elif msg.audio:
        item.update(media_file_id=msg.audio.file_id,     media_type="audio",    text=msg.caption)
    elif msg.animation:
        item.update(media_file_id=msg.animation.file_id, media_type="animation",text=msg.caption)
    elif msg.voice:
        item.update(media_file_id=msg.voice.file_id,     media_type="voice",    text=msg.caption)
    elif msg.text:
        item.update(media_file_id=None, media_type=None, text=msg.text)
    else:
        await msg.reply_text("⚠️ Unsupported type. Send text or media.")
        return MP_ADD_ITEM

    user_id: int = msg.from_user.id  # type: ignore[union-attr]
    chat_id: int = pm["mp_chat_id"]

    try:
        await add_to_media_pool(user_id, chat_id, item)
    except Exception as exc:
        logger.exception("add_to_media_pool failed: %s", exc)
        await msg.reply_text(f"❌ Error: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        return MP_MENU

    preview = (item.get("text") or "")[:50]
    if item.get("media_type"):
        preview = f"[{item['media_type'].upper()}] {preview}"

    # Check if this is the first item — if so, prompt for interval setup
    try:
        db    = await get_db()
        pool  = await db[COL_MEDIA_POOLS].find_one({"user_id": user_id, "chat_id": chat_id})
        count = len(pool.get("items", [])) if pool else 0
    except Exception:
        count = 1

    pm["last_item_preview"] = preview

    await msg.reply_text(
        f"✅ *Item added to pool!*\n"
        f"📝 `{preview or '—'}`\n"
        f"🎲 Pool now has `{count}` item(s).\n\n"
        + ("Would you like to *set the posting interval* for this pool now?\n"
           "Use the menu below to add more items or configure the interval."
           if count == 1 else
           "Add more items or return to the pool menu."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_mp_menu_keyboard(),
    )
    _clear_mp(context)
    return MP_MENU


# ── View Pool Stats ──

async def cb_mp_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id: int = query.from_user.id  # type: ignore[union-attr]

    try:
        db     = await get_db()
        cursor = db[COL_MEDIA_POOLS].find({"user_id": user_id})
        docs   = await cursor.to_list(length=50)
    except Exception as exc:
        await query.edit_message_text(f"❌ Error: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        return MP_MENU

    if not docs:
        await query.edit_message_text(
            "🎲 *Media Pools*\n\nNo pools yet. Add items to get started.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_mp_menu_keyboard(),
        )
        return MP_MENU

    lines = ["🎲 *Your Media Pools*\n"]
    for doc in docs:
        items   = doc.get("items", [])
        total   = len(items)
        unposted = sum(1 for i in items if not i.get("posted", False))
        lines.append(
            f"🗂 Chat `{doc['chat_id']}`\n"
            f"  📦 Total items: `{total}`\n"
            f"  🔄 Un-posted:   `{unposted}`\n"
            f"  ✅ Posted:      `{total - unposted}`\n"
        )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_mp_menu_keyboard(),
    )
    return MP_MENU


# ── Reset Pool ──

async def cb_mp_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "🔄 *Reset Pool*\n\n"
        "This marks all items as *un-posted* so the cycle restarts.\n"
        "No items will be deleted.\n\nProceed?",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, reset",  callback_data=CB_MP_RESET_YES),
                InlineKeyboardButton("⬅️ Back",        callback_data=CB_BACK_MP_MENU),
            ],
        ]),
    )
    return MP_MENU


async def cb_mp_reset_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Resetting…")
    user_id: int = query.from_user.id  # type: ignore[union-attr]

    try:
        db     = await get_db()
        pools  = await db[COL_MEDIA_POOLS].find({"user_id": user_id}).to_list(length=50)
        count  = 0
        for pool in pools:
            await reset_media_pool(user_id, pool["chat_id"])
            count += 1
    except Exception as exc:
        await query.edit_message_text(f"❌ Error: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        return MP_MENU

    await query.edit_message_text(
        f"✅ Reset `{count}` pool(s). All items are un-posted again.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_mp_menu_keyboard(),
    )
    return MP_MENU


# ── Clear Pool ──

async def cb_mp_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "🗑 *Clear Pool*\n\n"
        "⚠️ This will *permanently delete* all items from all your pools.\n"
        "Are you sure?",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, delete all", callback_data=CB_MP_CLEAR_YES),
                InlineKeyboardButton("⬅️ Back",            callback_data=CB_BACK_MP_MENU),
            ],
        ]),
    )
    return MP_MENU


async def cb_mp_clear_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Clearing…")
    user_id: int = query.from_user.id  # type: ignore[union-attr]

    try:
        db     = await get_db()
        result = await db[COL_MEDIA_POOLS].delete_many({"user_id": user_id})
        count  = result.deleted_count
    except Exception as exc:
        await query.edit_message_text(f"❌ Error: `{exc}`", parse_mode=ParseMode.MARKDOWN)
        return MP_MENU

    await query.edit_message_text(
        f"✅ Deleted `{count}` pool(s).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_mp_menu_keyboard(),
    )
    return MP_MENU


# ── Back to MP menu ──

async def cb_back_mp_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "🎲 *Media Pool — Random Shuffler*\n\nWhat would you like to do?",
        _mp_menu_keyboard(),
    )
    return MP_MENU


# ── Cancel ──

async def cb_cancel_local(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Cancelled.")
    _clear(context)
    _clear_mp(context)
    await query.edit_message_text("❌ *Cancelled.* Use /start to begin again.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ============================================================================
#  ConversationHandler factories
# ============================================================================

def build_queue_manager() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(enter_queue_manager, pattern=f"^{CB_MANAGE_QUEUE}$")],
        states={
            QM_MENU: [
                CallbackQueryHandler(cb_qm_set_slots,   pattern=f"^{CB_QM_SET_SLOTS}$"),
                CallbackQueryHandler(cb_qm_add_content, pattern=f"^{CB_QM_ADD_CONTENT}$"),
                CallbackQueryHandler(cb_qm_view,        pattern=f"^{CB_QM_VIEW}$"),
                CallbackQueryHandler(cb_qm_clear,       pattern=f"^{CB_QM_CLEAR}$"),
                CallbackQueryHandler(cb_qm_clear_yes,   pattern=f"^{CB_QM_CLEAR_YES}$"),
                CallbackQueryHandler(cb_qm_confirm_add, pattern=f"^{CB_QM_CONFIRM_ADD}$"),
                CallbackQueryHandler(cb_back_qm_menu,   pattern=f"^{CB_BACK_QM_MENU}$"),
            ],
            QM_SET_SLOTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_qm_slots),
                CallbackQueryHandler(cb_back_qm_menu, pattern=f"^{CB_BACK_QM_MENU}$"),
            ],
            QM_ADD_CHAT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_qm_add_chat_id),
                CallbackQueryHandler(cb_back_qm_menu, pattern=f"^{CB_BACK_QM_MENU}$"),
            ],
            QM_ADD_CONTENT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO |
                     filters.Document.ALL | filters.AUDIO | filters.ANIMATION |
                     filters.VOICE) & ~filters.COMMAND,
                    recv_qm_content,
                ),
                CallbackQueryHandler(cb_back_qm_menu, pattern=f"^{CB_BACK_QM_MENU}$"),
            ],
            QM_CONFIRM_SLOT: [
                CallbackQueryHandler(cb_qm_confirm_add, pattern=f"^{CB_QM_CONFIRM_ADD}$"),
                CallbackQueryHandler(cb_back_qm_menu,   pattern=f"^{CB_BACK_QM_MENU}$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CallbackQueryHandler(cb_cancel_local, pattern=f"^{CB_CANCEL}$"),
        ],
        allow_reentry=True,
        name="queue_manager",
        persistent=False,
    )


def build_media_pool() -> ConversationHandler:
    mp_tz_pattern  = r"^mptz:(?!page:).+"
    mp_tzp_pattern = r"^mptz:page:\d+$"

    return ConversationHandler(
        entry_points=[CallbackQueryHandler(enter_media_pool, pattern=f"^{CB_MEDIA_POOL}$")],
        states={
            MP_MENU: [
                CallbackQueryHandler(cb_mp_add_item,  pattern=f"^{CB_MP_ADD_ITEM}$"),
                CallbackQueryHandler(cb_mp_view,      pattern=f"^{CB_MP_VIEW}$"),
                CallbackQueryHandler(cb_mp_reset,     pattern=f"^{CB_MP_RESET}$"),
                CallbackQueryHandler(cb_mp_reset_yes, pattern=f"^{CB_MP_RESET_YES}$"),
                CallbackQueryHandler(cb_mp_clear,     pattern=f"^{CB_MP_CLEAR}$"),
                CallbackQueryHandler(cb_mp_clear_yes, pattern=f"^{CB_MP_CLEAR_YES}$"),
                CallbackQueryHandler(cb_back_mp_menu, pattern=f"^{CB_BACK_MP_MENU}$"),
            ],
            MP_CHAT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_mp_chat_id),
                CallbackQueryHandler(cb_back_mp_menu, pattern=f"^{CB_BACK_MP_MENU}$"),
            ],
            MP_ADD_ITEM: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO |
                     filters.Document.ALL | filters.AUDIO | filters.ANIMATION |
                     filters.VOICE) & ~filters.COMMAND,
                    recv_mp_item,
                ),
                CallbackQueryHandler(cb_back_mp_menu, pattern=f"^{CB_BACK_MP_MENU}$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CallbackQueryHandler(cb_cancel_local, pattern=f"^{CB_CANCEL}$"),
        ],
        allow_reentry=True,
        name="media_pool",
        persistent=False,
    )
