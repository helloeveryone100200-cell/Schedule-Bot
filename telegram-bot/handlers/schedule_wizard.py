"""
handlers/schedule_wizard.py — Multi-step scheduling wizard.

Wizard flow:
  1.  WIZARD_CHAT_ID        — enter target chat/channel ID
  2.  WIZARD_CONTENT        — send post content (text / media)
  3.  WIZARD_RECURRENCE     — choose recurrence type
        ├─ interval  → WIZARD_INTERVAL_VALUE → WIZARD_INTERVAL_UNIT
        ├─ days_of_week → WIZARD_DOW_SELECT  (multi-toggle)
        └─ once      → (skip interval steps)
  4.  WIZARD_FIRST_RUN      — enter first-run datetime (HH:MM or DD/MM/YYYY HH:MM)
  5.  WIZARD_TIMEZONE       — pick timezone (inline keyboard)
  6.  WIZARD_MAX_RUNS       — max runs or "unlimited"
  7.  WIZARD_TIME_WINDOW    — silent-hours ON/OFF toggle
        └─ ON → WIZARD_TW_START → WIZARD_TW_END
  8.  WIZARD_LIFECYCLE      — toggle auto-delete / self-destruct / auto-pin
        ├─ auto-delete ON  → WIZARD_AD_HOURS
        ├─ self-destruct ON → WIZARD_SD_SECS
        └─ auto-pin ON     → WIZARD_AP_HOURS
  9.  WIZARD_SUMMARY        — full review, ✅ Confirm saves to DB
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

from database import build_scheduled_post, insert_post
from handlers.base import CB_CANCEL, CB_MAIN_MENU, cmd_cancel, nav_keyboard, confirm_keyboard

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wizard state constants
# ---------------------------------------------------------------------------
(
    WIZARD_CHAT_ID,
    WIZARD_CONTENT,
    WIZARD_RECURRENCE,
    WIZARD_INTERVAL_VALUE,
    WIZARD_INTERVAL_UNIT,
    WIZARD_DOW_SELECT,
    WIZARD_FIRST_RUN,
    WIZARD_TIMEZONE,
    WIZARD_MAX_RUNS,
    WIZARD_TIME_WINDOW,
    WIZARD_TW_START,
    WIZARD_TW_END,
    WIZARD_LIFECYCLE,
    WIZARD_AD_HOURS,
    WIZARD_SD_SECS,
    WIZARD_AP_HOURS,
    WIZARD_SUMMARY,
) = range(10, 27)

# ---------------------------------------------------------------------------
# Callback-data constants (all ≤ 64 bytes)
# ---------------------------------------------------------------------------
CB_SCHEDULE_NEW  = "action:schedule_new"
CB_REC_ONCE      = "rec:once"
CB_REC_INTERVAL  = "rec:interval"
CB_REC_DOW       = "rec:dow"
CB_IU_MINUTES    = "iu:minutes"
CB_IU_HOURS      = "iu:hours"
CB_IU_DAYS       = "iu:days"
CB_DOW_NEXT      = "dow:next"
CB_TW_ON         = "tw:on"
CB_TW_OFF        = "tw:off"
CB_LC_NEXT       = "lc:next"
CB_CONFIRM_POST  = "confirm:post"

# Day-of-week toggle: "dow:0" … "dow:6"
def _dow_cb(day: int) -> str:
    return f"dow:{day}"

# Lifecycle toggle callbacks
CB_LC_AD   = "lc:ad"    # auto-delete toggle
CB_LC_SD   = "lc:sd"    # self-destruct toggle
CB_LC_AP   = "lc:ap"    # auto-pin toggle

# Timezone pagination
CB_TZ_PAGE = "tz:page:"     # "tz:page:0", "tz:page:1", …

# Back targets
CB_BACK_RECURRENCE  = "back:recurrence"
CB_BACK_INTERVAL_V  = "back:interval_v"
CB_BACK_INTERVAL_U  = "back:interval_u"
CB_BACK_DOW         = "back:dow"
CB_BACK_FIRST_RUN   = "back:first_run"
CB_BACK_TIMEZONE    = "back:timezone"
CB_BACK_MAX_RUNS    = "back:max_runs"
CB_BACK_TW          = "back:tw"
CB_BACK_TW_START    = "back:tw_start"
CB_BACK_LIFECYCLE   = "back:lifecycle"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DOW_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

TIMEZONES: list[tuple[str, str]] = [
    ("UTC",             "UTC"),
    ("Asia/Kolkata",    "GMT+5:30 India"),
    ("Asia/Dhaka",      "GMT+6:00 Bangladesh"),
    ("Asia/Rangoon",    "GMT+6:30 Myanmar"),
    ("Asia/Bangkok",    "GMT+7:00 Thailand"),
    ("Asia/Singapore",  "GMT+8:00 Singapore"),
    ("Asia/Tokyo",      "GMT+9:00 Japan"),
    ("Asia/Seoul",      "GMT+9:00 Korea"),
    ("Europe/London",   "GMT+0/+1 London"),
    ("Europe/Berlin",   "GMT+1/+2 Berlin"),
    ("America/New_York","GMT-5/-4 New York"),
    ("America/Chicago", "GMT-6/-5 Chicago"),
    ("America/Denver",  "GMT-7/-6 Denver"),
    ("America/Los_Angeles","GMT-8/-7 Los Angeles"),
    ("Australia/Sydney","GMT+10/+11 Sydney"),
]
TZ_PAGE_SIZE = 5   # timezones shown per page

# ---------------------------------------------------------------------------
# Wizard data helpers
# ---------------------------------------------------------------------------

def _w(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Get (or create) the wizard scratch-pad in user_data."""
    return context.user_data.setdefault("wizard", {})


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("wizard", None)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _recurrence_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔂 Interval (every X min/hr/day)", callback_data=CB_REC_INTERVAL)],
        [InlineKeyboardButton("📆 Specific Days of Week",          callback_data=CB_REC_DOW)],
        [InlineKeyboardButton("1️⃣  One-Time Post",                 callback_data=CB_REC_ONCE)],
        [
            InlineKeyboardButton("⬅️ Back",   callback_data=CB_MAIN_MENU),
            InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
        ],
    ])


def _interval_unit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏱ Minutes", callback_data=CB_IU_MINUTES),
            InlineKeyboardButton("🕐 Hours",   callback_data=CB_IU_HOURS),
            InlineKeyboardButton("📅 Days",    callback_data=CB_IU_DAYS),
        ],
        [
            InlineKeyboardButton("⬅️ Back",   callback_data=CB_BACK_INTERVAL_V),
            InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
        ],
    ])


def _dow_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    """Build a 7-button day-of-week toggle keyboard."""
    rows: list[list[InlineKeyboardButton]] = []
    # Two rows: Mon-Thu, Fri-Sun
    days_row1 = [
        InlineKeyboardButton(
            f"{'✅' if d in selected else '☐'} {DOW_LABELS[d]}",
            callback_data=_dow_cb(d),
        )
        for d in range(4)
    ]
    days_row2 = [
        InlineKeyboardButton(
            f"{'✅' if d in selected else '☐'} {DOW_LABELS[d]}",
            callback_data=_dow_cb(d),
        )
        for d in range(4, 7)
    ]
    rows.append(days_row1)
    rows.append(days_row2)
    rows.append([
        InlineKeyboardButton("⬅️ Back",    callback_data=CB_BACK_RECURRENCE),
        InlineKeyboardButton("➡️ Next",    callback_data=CB_DOW_NEXT),
        InlineKeyboardButton("❌ Cancel",  callback_data=CB_CANCEL),
    ])
    return InlineKeyboardMarkup(rows)


def _timezone_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    start = page * TZ_PAGE_SIZE
    end   = start + TZ_PAGE_SIZE
    page_tzs = TIMEZONES[start:end]
    rows = [
        [InlineKeyboardButton(label, callback_data=f"tz:{tz_name}")]
        for tz_name, label in page_tzs
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{CB_TZ_PAGE}{page - 1}"))
    if end < len(TIMEZONES):
        nav.append(InlineKeyboardButton("▶️ Next", callback_data=f"{CB_TZ_PAGE}{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton("⬅️ Back",   callback_data=CB_BACK_FIRST_RUN),
        InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
    ])
    return InlineKeyboardMarkup(rows)


def _time_window_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    on_icon  = "🟢 ON ✅" if enabled else "🔘 ON"
    off_icon = "🔴 OFF ✅" if not enabled else "🔘 OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(on_icon,  callback_data=CB_TW_ON),
            InlineKeyboardButton(off_icon, callback_data=CB_TW_OFF),
        ],
        [
            InlineKeyboardButton("⬅️ Back",    callback_data=CB_BACK_MAX_RUNS),
            InlineKeyboardButton("➡️ Next",    callback_data=CB_LC_NEXT if not enabled else CB_TW_ON),
            InlineKeyboardButton("❌ Cancel",  callback_data=CB_CANCEL),
        ],
    ])


def _lifecycle_keyboard(ad: bool, sd: bool, ap: bool) -> InlineKeyboardMarkup:
    def _flag(v: bool) -> str:
        return "✅" if v else "☐"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{_flag(ad)} Auto-Delete after X hours",      callback_data=CB_LC_AD)],
        [InlineKeyboardButton(f"{_flag(sd)} Self-Destruct after X seconds",  callback_data=CB_LC_SD)],
        [InlineKeyboardButton(f"{_flag(ap)} Auto-Pin (+ unpin after X hrs)", callback_data=CB_LC_AP)],
        [
            InlineKeyboardButton("⬅️ Back",   callback_data=CB_BACK_TW),
            InlineKeyboardButton("➡️ Next",   callback_data=CB_LC_NEXT),
            InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
        ],
    ])


# ---------------------------------------------------------------------------
# Input parsers / validators
# ---------------------------------------------------------------------------

_TIME_RE  = re.compile(r"^(\d{1,2}):(\d{2})$")
_DT_RE    = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})$")


def _parse_run_time(text: str, tz: str) -> datetime | None:
    """
    Parse user-supplied date/time string into an aware datetime.
    Accepts: "HH:MM" (today in the given tz) or "DD/MM/YYYY HH:MM".
    Returns None on parse failure.
    """
    text = text.strip()
    zone = pytz.timezone(tz) if tz != "UTC" else timezone.utc

    m = _DT_RE.match(text)
    if m:
        d, mo, y, h, mi = (int(x) for x in m.groups())
        try:
            dt = datetime(y, mo, d, h, mi)
            if isinstance(zone, pytz.BaseTzInfo):
                return zone.localize(dt)
            return dt.replace(tzinfo=zone)
        except ValueError:
            return None

    m = _TIME_RE.match(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        now = datetime.now(tz=zone)
        candidate = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    return None


def _parse_hhmm(text: str) -> str | None:
    """Validate and normalise 'HH:MM' string. Returns None if invalid."""
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


# ---------------------------------------------------------------------------
# Shared "answer + edit" helper
# ---------------------------------------------------------------------------

async def _edit(query: Any, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """
    Answer the callback and edit the message in-place.
    Falls back to reply_text if the edit fails (e.g. message too old, parse error).
    """
    await query.answer()
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
    except Exception as exc:
        logger.warning("edit_message_text failed (%s) — sending new message instead.", exc)
        try:
            await query.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )
        except Exception as exc2:
            logger.error("reply_text fallback also failed: %s", exc2)


# ---------------------------------------------------------------------------
# Wizard entry
# ---------------------------------------------------------------------------

async def enter_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — triggered by 'Schedule New Post' button."""
    query = update.callback_query
    assert query is not None
    _clear(context)
    _w(context)   # initialise empty wizard dict
    await _edit(
        query,
        "📅 *Schedule New Post — Step 1/9*\n\n"
        "Send the *Chat ID* of the channel or group you want to post to.\n\n"
        "💡 To get a chat ID: forward any message from the chat to @userinfobot "
        "or add @username\\_to\\_id\\_bot to the group temporarily.\n\n"
        "Type the ID below (example: `-1001234567890`):",
        nav_keyboard(back_data=CB_MAIN_MENU),
    )
    return WIZARD_CHAT_ID


# ---------------------------------------------------------------------------
# Step 1 — Chat ID
# ---------------------------------------------------------------------------

async def recv_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    text = (update.message.text or "").strip()
    try:
        chat_id = int(text)
    except ValueError:
        await update.message.reply_text(
            "⚠️ That doesn't look like a valid Chat ID. Please send a number, "
            "e.g. `-1001234567890`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WIZARD_CHAT_ID

    _w(context)["chat_id"] = chat_id
    await update.message.reply_text(
        f"✅ Chat ID saved: `{chat_id}`\n\n"
        "📝 *Step 2/9 — Post Content*\n\n"
        "Now send the content you want to post. This can be:\n"
        "• Plain text\n"
        "• A photo, video, document, or audio\n"
        "• Text + media together (send media with a caption)\n\n"
        "Send your content now:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=nav_keyboard(back_data=None),
    )
    return WIZARD_CONTENT


# ---------------------------------------------------------------------------
# Step 2 — Content
# ---------------------------------------------------------------------------

async def recv_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    msg: Message = update.message
    w = _w(context)

    if msg.photo:
        w["media_file_id"]   = msg.photo[-1].file_id
        w["media_type"]      = "photo"
        w["content_text"]    = msg.caption
    elif msg.video:
        w["media_file_id"]   = msg.video.file_id
        w["media_type"]      = "video"
        w["content_text"]    = msg.caption
    elif msg.document:
        w["media_file_id"]   = msg.document.file_id
        w["media_type"]      = "document"
        w["content_text"]    = msg.caption
    elif msg.audio:
        w["media_file_id"]   = msg.audio.file_id
        w["media_type"]      = "audio"
        w["content_text"]    = msg.caption
    elif msg.animation:
        w["media_file_id"]   = msg.animation.file_id
        w["media_type"]      = "animation"
        w["content_text"]    = msg.caption
    elif msg.voice:
        w["media_file_id"]   = msg.voice.file_id
        w["media_type"]      = "voice"
        w["content_text"]    = msg.caption
    elif msg.text:
        w["media_file_id"]   = None
        w["media_type"]      = None
        w["content_text"]    = msg.text
    else:
        await msg.reply_text("⚠️ Unsupported content type. Please send text, a photo, video, document, or audio.")
        return WIZARD_CONTENT

    await msg.reply_text(
        "✅ Content saved.\n\n"
        "🔁 *Step 3/9 — Recurrence Type*\n\n"
        "How often should this post be sent?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_recurrence_keyboard(),
    )
    return WIZARD_RECURRENCE


# ---------------------------------------------------------------------------
# Step 3 — Recurrence type
# ---------------------------------------------------------------------------

async def cb_rec_once(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _w(context)["recurrence_type"] = "once"
    query = update.callback_query
    await _edit(query,
        "🕐 *Step 4/9 — First Run Time*\n\n"
        "When should this post be sent?\n\n"
        "Type the date/time in one of these formats:\n"
        "• `HH:MM` — today at this time (e.g. `14:30`)\n"
        "• `DD/MM/YYYY HH:MM` — specific date (e.g. `25/12/2025 09:00`)",
        nav_keyboard(back_data=CB_BACK_RECURRENCE),
    )
    return WIZARD_FIRST_RUN


async def cb_rec_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _w(context)["recurrence_type"] = "interval"
    query = update.callback_query
    await _edit(query,
        "⏱ *Step 3a — Interval Value*\n\n"
        "How many minutes/hours/days between each post?\n\n"
        "Type a positive whole number (e.g. `6`):",
        nav_keyboard(back_data=CB_BACK_RECURRENCE),
    )
    return WIZARD_INTERVAL_VALUE


async def cb_rec_dow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    w = _w(context)
    w["recurrence_type"] = "days_of_week"
    w.setdefault("days_of_week", set())
    query = update.callback_query
    await _edit(query,
        "📆 *Step 3a — Days of Week*\n\n"
        "Tap days to toggle them. Tap *➡️ Next* when done.",
        _dow_keyboard(w["days_of_week"]),
    )
    return WIZARD_DOW_SELECT


# ---------------------------------------------------------------------------
# Step 3a (interval) — value
# ---------------------------------------------------------------------------

async def recv_interval_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    text = (update.message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text(
            "⚠️ Please enter a *positive whole number* greater than 0 (e.g. `6`).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WIZARD_INTERVAL_VALUE

    _w(context)["interval_value"] = int(text)
    await update.message.reply_text(
        f"✅ Value: `{text}`\n\n"
        "⏱ *Step 3b — Interval Unit*\n\nChoose the time unit:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_interval_unit_keyboard(),
    )
    return WIZARD_INTERVAL_UNIT


async def cb_back_interval_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "⏱ *Step 3a — Interval Value*\n\n"
        "Type a positive whole number (e.g. `6`):",
        nav_keyboard(back_data=CB_BACK_RECURRENCE),
    )
    return WIZARD_INTERVAL_VALUE


# ---------------------------------------------------------------------------
# Step 3b (interval) — unit
# ---------------------------------------------------------------------------

async def cb_interval_unit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    unit_map = {CB_IU_MINUTES: "minutes", CB_IU_HOURS: "hours", CB_IU_DAYS: "days"}
    unit = unit_map[query.data]
    _w(context)["interval_unit"] = unit
    await _edit(query,
        f"✅ Interval: every `{_w(context)['interval_value']} {unit}`\n\n"
        "🕐 *Step 4/9 — First Run Time*\n\n"
        "When should the *first* post fire?\n"
        "• `HH:MM` — today at this time\n"
        "• `DD/MM/YYYY HH:MM` — specific date",
        nav_keyboard(back_data=CB_BACK_INTERVAL_U),
    )
    return WIZARD_FIRST_RUN


# ---------------------------------------------------------------------------
# Step 3a (DOW) — day toggles
# ---------------------------------------------------------------------------

async def cb_dow_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    day = int(query.data.split(":")[1])
    w = _w(context)
    selected: set[int] = w.setdefault("days_of_week", set())
    if day in selected:
        selected.discard(day)
    else:
        selected.add(day)
    await query.answer(f"{'✅' if day in selected else '☐'} {DOW_LABELS[day]}")
    await query.edit_message_reply_markup(_dow_keyboard(selected))
    return WIZARD_DOW_SELECT


async def cb_dow_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    w = _w(context)
    selected: set[int] = w.get("days_of_week", set())
    if not selected:
        await query.answer("⚠️ Please select at least one day.", show_alert=True)
        return WIZARD_DOW_SELECT
    await _edit(query,
        f"✅ Days: `{', '.join(DOW_LABELS[d] for d in sorted(selected))}`\n\n"
        "🕐 *Step 4/9 — First Run Time*\n\n"
        "When should the *first* post fire?\n"
        "• `HH:MM` — today at this time\n"
        "• `DD/MM/YYYY HH:MM` — specific date",
        nav_keyboard(back_data=CB_BACK_DOW),
    )
    return WIZARD_FIRST_RUN


# ---------------------------------------------------------------------------
# Step 4 — First run time
# ---------------------------------------------------------------------------

async def recv_first_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    w = _w(context)
    tz_str = w.get("timezone", "UTC")
    text = (update.message.text or "").strip()

    dt = _parse_run_time(text, tz_str)
    if dt is None:
        await update.message.reply_text(
            "⚠️ Couldn't parse that. Use `HH:MM` or `DD/MM/YYYY HH:MM`.\n"
            "Example: `14:30` or `25/12/2025 09:00`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WIZARD_FIRST_RUN

    w["first_run"] = dt
    w["first_run_raw"] = text
    await update.message.reply_text(
        f"✅ First run: `{text}`\n\n"
        "🌍 *Step 5/9 — Timezone*\n\n"
        "Select your timezone so times are interpreted correctly:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_timezone_keyboard(0),
    )
    return WIZARD_TIMEZONE


# ---------------------------------------------------------------------------
# Back to first run
# ---------------------------------------------------------------------------

async def cb_back_first_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    w = _w(context)
    back = CB_BACK_DOW if w.get("recurrence_type") == "days_of_week" else CB_BACK_INTERVAL_U
    await _edit(query,
        "🕐 *Step 4/9 — First Run Time*\n\n"
        "• `HH:MM` — today at this time\n"
        "• `DD/MM/YYYY HH:MM` — specific date",
        nav_keyboard(back_data=back),
    )
    return WIZARD_FIRST_RUN


# ---------------------------------------------------------------------------
# Step 5 — Timezone
# ---------------------------------------------------------------------------

async def cb_timezone_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    page = int(query.data.replace(CB_TZ_PAGE, ""))
    await query.answer()
    await query.edit_message_reply_markup(_timezone_keyboard(page))
    return WIZARD_TIMEZONE


async def cb_timezone_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    tz_name = query.data.replace("tz:", "")
    try:
        pytz.timezone(tz_name)
    except pytz.exceptions.UnknownTimeZoneError:
        await query.answer("⚠️ Unknown timezone.", show_alert=True)
        return WIZARD_TIMEZONE

    w = _w(context)
    w["timezone"] = tz_name

    # Re-parse first_run with the now-known timezone
    raw = w.get("first_run_raw", "")
    dt = _parse_run_time(raw, tz_name)
    if dt:
        w["first_run"] = dt

    await _edit(query,
        f"✅ Timezone: `{tz_name}`\n\n"
        "🔁 *Step 6/9 — Max Runs*\n\n"
        "How many times should this post be sent?\n"
        "• Type a number (e.g. `10`) to limit runs\n"
        "• Type `0` for *unlimited* (runs forever)",
        nav_keyboard(back_data=CB_BACK_TIMEZONE),
    )
    return WIZARD_MAX_RUNS


# ---------------------------------------------------------------------------
# Back to timezone
# ---------------------------------------------------------------------------

async def cb_back_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "🌍 *Step 5/9 — Timezone*\n\nSelect your timezone:",
        _timezone_keyboard(0),
    )
    return WIZARD_TIMEZONE


# ---------------------------------------------------------------------------
# Step 6 — Max runs
# ---------------------------------------------------------------------------

async def recv_max_runs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text(
            "⚠️ Please enter a whole number ≥ 0.  Type `0` for unlimited.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WIZARD_MAX_RUNS

    val = int(text)
    w = _w(context)
    w["max_runs"] = None if val == 0 else val
    # Use setdefault so back-navigation doesn't clobber a time-window choice
    # the user already made.  Only initialise if the key is absent.
    w.setdefault("time_window_enabled", False)

    label = "unlimited" if val == 0 else str(val)
    await update.message.reply_text(
        f"✅ Max runs: `{label}`\n\n"
        "🕐 *Step 7/9 — Silent Hours (Time Window)*\n\n"
        "Enable a daily time window during which posts are allowed to fire?\n"
        "Posts outside this window will be *skipped*, not deleted.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_time_window_keyboard(False),
    )
    return WIZARD_TIME_WINDOW


# ---------------------------------------------------------------------------
# Step 7 — Time window toggle
# ---------------------------------------------------------------------------

async def cb_tw_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    enabled = query.data == CB_TW_ON
    _w(context)["time_window_enabled"] = enabled
    await query.answer()
    await query.edit_message_reply_markup(_time_window_keyboard(enabled))
    return WIZARD_TIME_WINDOW


async def cb_tw_proceed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Called when user taps ➡️ Next on the time-window screen.
    Routes to start-time input (if ON) or lifecycle (if OFF).
    """
    query = update.callback_query
    assert query is not None
    w = _w(context)
    if w.get("time_window_enabled"):
        await _edit(query,
            "🕐 *Step 7a — Window Start Time*\n\n"
            "Type the earliest time posts should fire (e.g. `08:00`):",
            nav_keyboard(back_data=CB_BACK_TW),
        )
        return WIZARD_TW_START
    else:
        w["tw_start"] = None
        w["tw_end"]   = None
        await _edit(query,
            "⚙️ *Step 8/9 — Lifecycle Options*\n\n"
            "Tap options to toggle them on/off, then tap *➡️ Next*:",
            _lifecycle_keyboard(
                w.get("auto_delete", False),
                w.get("self_destruct", False),
                w.get("auto_pin", False),
            ),
        )
        return WIZARD_LIFECYCLE


# ---------------------------------------------------------------------------
# Steps 7a / 7b — Time-window start / end
# ---------------------------------------------------------------------------

async def recv_tw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    text = (update.message.text or "").strip()
    parsed = _parse_hhmm(text)
    if parsed is None:
        await update.message.reply_text("⚠️ Use `HH:MM` format, e.g. `08:00`.", parse_mode=ParseMode.MARKDOWN)
        return WIZARD_TW_START
    _w(context)["tw_start"] = parsed
    await update.message.reply_text(
        f"✅ Window starts: `{parsed}`\n\n"
        "🕐 *Step 7b — Window End Time*\n\n"
        "Type the latest time posts may fire (e.g. `22:00`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=nav_keyboard(back_data=CB_BACK_TW_START),
    )
    return WIZARD_TW_END


async def recv_tw_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    text = (update.message.text or "").strip()
    parsed = _parse_hhmm(text)
    w = _w(context)
    if parsed is None:
        await update.message.reply_text("⚠️ Use `HH:MM` format, e.g. `22:00`.", parse_mode=ParseMode.MARKDOWN)
        return WIZARD_TW_END

    start = w.get("tw_start", "00:00")
    if parsed <= start:
        await update.message.reply_text(
            f"⚠️ End time (`{parsed}`) must be *after* start time (`{start}`).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WIZARD_TW_END

    w["tw_end"] = parsed
    # Initialise lifecycle flags
    w.setdefault("auto_delete",   False)
    w.setdefault("self_destruct", False)
    w.setdefault("auto_pin",      False)
    await update.message.reply_text(
        f"✅ Window: `{start}` → `{parsed}`\n\n"
        "⚙️ *Step 8/9 — Lifecycle Options*\n\n"
        "Tap options to toggle them on/off, then tap *➡️ Next*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_lifecycle_keyboard(
            w["auto_delete"], w["self_destruct"], w["auto_pin"]
        ),
    )
    return WIZARD_LIFECYCLE


# ---------------------------------------------------------------------------
# Step 8 — Lifecycle toggles
# ---------------------------------------------------------------------------

async def cb_lc_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    w = _w(context)
    key_map = {CB_LC_AD: "auto_delete", CB_LC_SD: "self_destruct", CB_LC_AP: "auto_pin"}
    key = key_map[query.data]
    w[key] = not w.get(key, False)
    await query.answer(f"{'✅ Enabled' if w[key] else '☐ Disabled'}: {key.replace('_', ' ').title()}")
    await query.edit_message_reply_markup(
        _lifecycle_keyboard(w.get("auto_delete", False), w.get("self_destruct", False), w.get("auto_pin", False))
    )
    return WIZARD_LIFECYCLE


async def cb_lc_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Route to the first enabled lifecycle detail step, or directly to summary."""
    query = update.callback_query
    assert query is not None
    w = _w(context)
    if w.get("auto_delete"):
        await _edit(query,
            "🗑 *Auto-Delete — After how many hours?*\n\n"
            "Type a positive number (decimals OK, e.g. `24` or `0.5`):",
            nav_keyboard(back_data=CB_BACK_LIFECYCLE),
        )
        return WIZARD_AD_HOURS
    if w.get("self_destruct"):
        await _edit(query,
            "💣 *Self-Destruct — After how many seconds?*\n\n"
            "Type a positive integer (e.g. `60`):",
            nav_keyboard(back_data=CB_BACK_LIFECYCLE),
        )
        return WIZARD_SD_SECS
    if w.get("auto_pin"):
        await _edit(query,
            "📌 *Auto-Pin — Unpin after how many hours?*\n\n"
            "Type a positive number (e.g. `12`):",
            nav_keyboard(back_data=CB_BACK_LIFECYCLE),
        )
        return WIZARD_AP_HOURS
    return await _show_summary(update, context)


# ---------------------------------------------------------------------------
# Step 8a/b/c — Lifecycle detail inputs
# ---------------------------------------------------------------------------

async def recv_ad_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    try:
        val = float(update.message.text.strip())
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a positive number, e.g. `24`.", parse_mode=ParseMode.MARKDOWN)
        return WIZARD_AD_HOURS
    _w(context)["ad_hours"] = val
    w = _w(context)
    if w.get("self_destruct"):
        await update.message.reply_text(
            f"✅ Auto-delete: `{val}h`\n\n"
            "💣 *Self-Destruct — After how many seconds?*\n\n"
            "Type a positive integer:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=nav_keyboard(back_data=CB_BACK_LIFECYCLE),
        )
        return WIZARD_SD_SECS
    if w.get("auto_pin"):
        await update.message.reply_text(
            f"✅ Auto-delete: `{val}h`\n\n"
            "📌 *Auto-Pin — Unpin after how many hours?*\n\n"
            "Type a positive number:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=nav_keyboard(back_data=CB_BACK_LIFECYCLE),
        )
        return WIZARD_AP_HOURS
    return await _show_summary_from_message(update, context)


async def recv_sd_secs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    text = (update.message.text or "").strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("⚠️ Please enter a positive integer, e.g. `60`.", parse_mode=ParseMode.MARKDOWN)
        return WIZARD_SD_SECS
    _w(context)["sd_secs"] = int(text)
    w = _w(context)
    if w.get("auto_pin"):
        await update.message.reply_text(
            f"✅ Self-destruct: `{text}s`\n\n"
            "📌 *Auto-Pin — Unpin after how many hours?*\n\n"
            "Type a positive number:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=nav_keyboard(back_data=CB_BACK_LIFECYCLE),
        )
        return WIZARD_AP_HOURS
    return await _show_summary_from_message(update, context)


async def recv_ap_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    try:
        val = float(update.message.text.strip())
        if val <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please enter a positive number, e.g. `12`.", parse_mode=ParseMode.MARKDOWN)
        return WIZARD_AP_HOURS
    _w(context)["ap_hours"] = val
    return await _show_summary_from_message(update, context)


# ---------------------------------------------------------------------------
# Step 9 — Summary + Confirm
# ---------------------------------------------------------------------------

def _build_summary(w: dict[str, Any]) -> str:
    rec = w.get("recurrence_type", "once")
    if rec == "interval":
        rec_line = f"Every `{w.get('interval_value')} {w.get('interval_unit')}`"
    elif rec == "days_of_week":
        days = sorted(w.get("days_of_week", set()))
        rec_line = f"Weekly on `{', '.join(DOW_LABELS[d] for d in days)}`"
    else:
        rec_line = "One-time"

    tw = w.get("time_window_enabled", False)
    tw_line = f"`{w.get('tw_start')}` → `{w.get('tw_end')}`" if tw else "Disabled"

    ad  = f"`{w.get('ad_hours')}h`"  if w.get("auto_delete")   else "OFF"
    sd  = f"`{w.get('sd_secs')}s`"   if w.get("self_destruct") else "OFF"
    ap  = f"`{w.get('ap_hours')}h`"  if w.get("auto_pin")      else "OFF"

    max_r = w.get("max_runs")
    max_line = f"`{max_r}` runs" if max_r else "Unlimited"

    content_preview = (w.get("content_text") or "")[:60]
    if w.get("media_type"):
        content_preview = f"[{w['media_type'].upper()}] {content_preview}"

    first_run = w.get("first_run_raw", "—")

    return (
        "📋 *Post Summary — Please Review*\n\n"
        f"🗂 *Chat ID:* `{w.get('chat_id')}`\n"
        f"📝 *Content:* `{content_preview or '—'}`\n"
        f"🔁 *Recurrence:* {rec_line}\n"
        f"🕐 *First Run:* `{first_run}`\n"
        f"🌍 *Timezone:* `{w.get('timezone', 'UTC')}`\n"
        f"🔢 *Max Runs:* {max_line}\n"
        f"🕑 *Time Window:* {tw_line}\n"
        f"🗑 *Auto-Delete:* {ad}\n"
        f"💣 *Self-Destruct:* {sd}\n"
        f"📌 *Auto-Pin:* {ap}\n\n"
        "Tap *✅ Confirm* to schedule, *⬅️ Back* to edit, or *❌ Cancel* to abort."
    )


async def _show_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        _build_summary(_w(context)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=confirm_keyboard(CB_CONFIRM_POST, CB_BACK_LIFECYCLE),
    )
    return WIZARD_SUMMARY


async def _show_summary_from_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    assert update.message is not None
    await update.message.reply_text(
        _build_summary(_w(context)),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=confirm_keyboard(CB_CONFIRM_POST, CB_BACK_LIFECYCLE),
    )
    return WIZARD_SUMMARY


async def cb_confirm_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save the post to MongoDB and end the wizard."""
    query = update.callback_query
    assert query is not None
    await query.answer("Saving…")

    w = _w(context)
    user_id: int = query.from_user.id  # type: ignore[union-attr]

    try:
        doc = build_scheduled_post(
            user_id=user_id,
            chat_id=w["chat_id"],
            content_text=w.get("content_text"),
            content_media_file_id=w.get("media_file_id"),
            content_media_type=w.get("media_type"),
            timezone_str=w.get("timezone", "UTC"),
            recurrence_type=w.get("recurrence_type", "once"),
            interval_value=w.get("interval_value"),
            interval_unit=w.get("interval_unit"),
            days_of_week=sorted(w["days_of_week"]) if w.get("days_of_week") else None,
            next_run_at=w.get("first_run"),
            max_runs=w.get("max_runs"),
            time_window_enabled=w.get("time_window_enabled", False),
            time_window_start=w.get("tw_start"),
            time_window_end=w.get("tw_end"),
            auto_delete_enabled=w.get("auto_delete", False),
            auto_delete_after_hours=w.get("ad_hours"),
            self_destruct_enabled=w.get("self_destruct", False),
            self_destruct_after_seconds=w.get("sd_secs"),
            auto_pin_enabled=w.get("auto_pin", False),
            auto_pin_unpin_after_hours=w.get("ap_hours"),
        )
        post_id = await insert_post(doc)
    except Exception as exc:
        logger.exception("Failed to save post: %s", exc)
        await query.edit_message_text(
            f"❌ Failed to save post: `{exc}`\n\nTry again or contact support.",
            parse_mode=ParseMode.MARKDOWN,
        )
        _clear(context)
        return ConversationHandler.END

    _clear(context)
    await query.edit_message_text(
        f"✅ *Post scheduled successfully!*\n\n"
        f"🆔 Post ID: `{post_id}`\n\n"
        "Use *My Posts* to manage it.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Back-navigation helpers
# ---------------------------------------------------------------------------

async def cb_back_recurrence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "🔁 *Step 3/9 — Recurrence Type*\n\nHow often should this post be sent?",
        _recurrence_keyboard(),
    )
    return WIZARD_RECURRENCE


async def cb_back_dow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    w = _w(context)
    await _edit(query,
        "📆 *Step 3a — Days of Week*\n\nTap days to toggle, then ➡️ Next.",
        _dow_keyboard(w.get("days_of_week", set())),
    )
    return WIZARD_DOW_SELECT


async def cb_back_interval_unit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    w = _w(context)
    await _edit(query,
        f"✅ Value: `{w.get('interval_value')}`\n\n"
        "⏱ *Step 3b — Interval Unit*\n\nChoose the time unit:",
        _interval_unit_keyboard(),
    )
    return WIZARD_INTERVAL_UNIT


async def cb_back_max_runs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "🔢 *Step 6/9 — Max Runs*\n\n"
        "Type a number to limit runs, or `0` for unlimited:",
        nav_keyboard(back_data=CB_BACK_TIMEZONE),
    )
    return WIZARD_MAX_RUNS


async def cb_back_tw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    w = _w(context)
    await _edit(query,
        "🕐 *Step 7/9 — Silent Hours (Time Window)*\n\n"
        "Enable a daily window during which posts are allowed to fire?",
        _time_window_keyboard(w.get("time_window_enabled", False)),
    )
    return WIZARD_TIME_WINDOW


async def cb_back_tw_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _edit(query,
        "🕐 *Step 7a — Window Start Time*\n\n"
        "Type the earliest time posts should fire (e.g. `08:00`):",
        nav_keyboard(back_data=CB_BACK_TW),
    )
    return WIZARD_TW_START


async def cb_back_lifecycle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    w = _w(context)
    await _edit(query,
        "⚙️ *Step 8/9 — Lifecycle Options*\n\n"
        "Tap options to toggle them on/off, then tap *➡️ Next*:",
        _lifecycle_keyboard(w.get("auto_delete", False), w.get("self_destruct", False), w.get("auto_pin", False)),
    )
    return WIZARD_LIFECYCLE


# ---------------------------------------------------------------------------
# Cancel (duplicated locally for convenience)
# ---------------------------------------------------------------------------

async def cb_cancel_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer("Cancelled.")
    _clear(context)
    await query.edit_message_text("❌ *Cancelled.* Use /start to begin again.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler factory
# ---------------------------------------------------------------------------

def build_schedule_wizard() -> ConversationHandler:
    dow_pattern  = r"^dow:[0-6]$"
    tz_pattern   = r"^tz:(?!page:).+"
    tzp_pattern  = r"^tz:page:\d+$"

    return ConversationHandler(
        entry_points=[CallbackQueryHandler(enter_wizard, pattern=f"^{CB_SCHEDULE_NEW}$")],
        states={
            WIZARD_CHAT_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_chat_id),
            ],
            WIZARD_CONTENT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO |
                     filters.Document.ALL | filters.AUDIO | filters.ANIMATION |
                     filters.VOICE) & ~filters.COMMAND,
                    recv_content,
                ),
            ],
            WIZARD_RECURRENCE: [
                CallbackQueryHandler(cb_rec_once,     pattern=f"^{CB_REC_ONCE}$"),
                CallbackQueryHandler(cb_rec_interval, pattern=f"^{CB_REC_INTERVAL}$"),
                CallbackQueryHandler(cb_rec_dow,      pattern=f"^{CB_REC_DOW}$"),
                CallbackQueryHandler(cb_back_recurrence, pattern=f"^{CB_BACK_RECURRENCE}$"),
            ],
            WIZARD_INTERVAL_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_interval_value),
                CallbackQueryHandler(cb_back_recurrence, pattern=f"^{CB_BACK_RECURRENCE}$"),
            ],
            WIZARD_INTERVAL_UNIT: [
                CallbackQueryHandler(cb_interval_unit,    pattern=f"^({CB_IU_MINUTES}|{CB_IU_HOURS}|{CB_IU_DAYS})$"),
                CallbackQueryHandler(cb_back_interval_value, pattern=f"^{CB_BACK_INTERVAL_V}$"),
            ],
            WIZARD_DOW_SELECT: [
                CallbackQueryHandler(cb_dow_toggle,      pattern=dow_pattern),
                CallbackQueryHandler(cb_dow_next,        pattern=f"^{CB_DOW_NEXT}$"),
                CallbackQueryHandler(cb_back_recurrence, pattern=f"^{CB_BACK_RECURRENCE}$"),
            ],
            WIZARD_FIRST_RUN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_first_run),
                CallbackQueryHandler(cb_back_first_run,  pattern=f"^{CB_BACK_FIRST_RUN}$"),
                CallbackQueryHandler(cb_back_dow,        pattern=f"^{CB_BACK_DOW}$"),
                CallbackQueryHandler(cb_back_interval_unit, pattern=f"^{CB_BACK_INTERVAL_U}$"),
            ],
            WIZARD_TIMEZONE: [
                CallbackQueryHandler(cb_timezone_pick,  pattern=tz_pattern),
                CallbackQueryHandler(cb_timezone_page,  pattern=tzp_pattern),
                CallbackQueryHandler(cb_back_first_run, pattern=f"^{CB_BACK_FIRST_RUN}$"),
            ],
            WIZARD_MAX_RUNS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_max_runs),
                CallbackQueryHandler(cb_back_timezone, pattern=f"^{CB_BACK_TIMEZONE}$"),
            ],
            WIZARD_TIME_WINDOW: [
                CallbackQueryHandler(cb_tw_toggle,  pattern=f"^({CB_TW_ON}|{CB_TW_OFF})$"),
                CallbackQueryHandler(cb_tw_proceed, pattern=f"^{CB_LC_NEXT}$"),
                CallbackQueryHandler(cb_back_max_runs, pattern=f"^{CB_BACK_MAX_RUNS}$"),
            ],
            WIZARD_TW_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_tw_start),
                CallbackQueryHandler(cb_back_tw, pattern=f"^{CB_BACK_TW}$"),
            ],
            WIZARD_TW_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_tw_end),
                CallbackQueryHandler(cb_back_tw_start, pattern=f"^{CB_BACK_TW_START}$"),
            ],
            WIZARD_LIFECYCLE: [
                CallbackQueryHandler(cb_lc_toggle, pattern=f"^({CB_LC_AD}|{CB_LC_SD}|{CB_LC_AP})$"),
                CallbackQueryHandler(cb_lc_next,   pattern=f"^{CB_LC_NEXT}$"),
                CallbackQueryHandler(cb_back_tw,   pattern=f"^{CB_BACK_TW}$"),
            ],
            WIZARD_AD_HOURS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_ad_hours),
                CallbackQueryHandler(cb_back_lifecycle, pattern=f"^{CB_BACK_LIFECYCLE}$"),
            ],
            WIZARD_SD_SECS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_sd_secs),
                CallbackQueryHandler(cb_back_lifecycle, pattern=f"^{CB_BACK_LIFECYCLE}$"),
            ],
            WIZARD_AP_HOURS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_ap_hours),
                CallbackQueryHandler(cb_back_lifecycle, pattern=f"^{CB_BACK_LIFECYCLE}$"),
            ],
            WIZARD_SUMMARY: [
                CallbackQueryHandler(cb_confirm_post,   pattern=f"^{CB_CONFIRM_POST}$"),
                CallbackQueryHandler(cb_back_lifecycle, pattern=f"^{CB_BACK_LIFECYCLE}$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CallbackQueryHandler(cb_cancel_wizard, pattern=f"^{CB_CANCEL}$"),
        ],
        allow_reentry=True,
        name="schedule_wizard",
        persistent=False,
    )
