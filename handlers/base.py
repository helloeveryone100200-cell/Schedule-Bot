"""
handlers/base.py — /start, /help, /cancel, My Posts, and universal navigation.

Navigation pattern used throughout the bot:
  - Every wizard step includes ⬅️ Back and ❌ Cancel inline buttons.
  - Before committing to the database a full summary is shown with ✅ Confirm.
  - ❌ Cancel always resets ConversationHandler state and returns to the main menu.
"""

from __future__ import annotations

import logging
from datetime import timezone as _dt_timezone
from typing import Any

import pytz

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from handlers import chat_cleanup
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

from config import ADMIN_IDS
from handlers.owner_panel import CB_OWNER_PANEL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Callback-data constants  (all ≤ 64 bytes — well within Telegram's 64-byte limit)
# ---------------------------------------------------------------------------
CB_HELP         = "nav:help"
CB_CANCEL       = "nav:cancel"
CB_MAIN_MENU    = "nav:main_menu"
CB_SCHEDULE_NEW = "action:schedule_new"
CB_MY_POSTS     = "action:my_posts"
CB_MANAGE_QUEUE = "action:manage_queue"
CB_MEDIA_POOL   = "action:media_pool"

# My Posts action prefixes (prefix + 24-char ObjectId ≤ 64 bytes total)
CB_MPP_PAGE    = "mpp:page:"     # + page number
CB_MPP_PAUSE   = "mpp:pause:"   # + post_id
CB_MPP_RESUME  = "mpp:resume:"  # + post_id
CB_MPP_DEL       = "mpp:del:"       # + post_id  (ask confirm)
CB_MPP_DEL_YES   = "mpp:delyes:"   # + post_id  (execute)
CB_MPP_CLONE     = "mpp:clone:"    # + post_id  (ask confirm)
CB_MPP_CLONE_YES = "mpp:cloneyes:" # + post_id  (execute)
CB_MPP_SAVE_TMPL = "mpp:savetmpl:" # + post_id  (save as template)

CB_MY_STATS  = "action:my_stats"   # post statistics
CB_TEMPLATES = "action:templates"  # templates menu

# ---------------------------------------------------------------------------
# Conversation states (used by the base ConversationHandler)
# ---------------------------------------------------------------------------
MAIN_MENU               = 0
MY_POSTS                = 1
MY_POSTS_CONFIRM_DELETE = 2
MY_POSTS_CONFIRM_CLONE  = 3
MY_POSTS_SAVE_TEMPLATE  = 4

POSTS_PER_PAGE = 5


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def _bot_username(bot: Bot) -> str:
    """Return the bot's username, falling back gracefully if not yet available."""
    return bot.username or "YourSchedulerBot"


def start_keyboard(bot: Bot) -> InlineKeyboardMarkup:
    username = _bot_username(bot)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❓ Help", callback_data=CB_HELP),
        ],
        [
            InlineKeyboardButton(
                "➕ Add me to Group/Channel",
                url=f"https://t.me/{username}?startgroup=true",
            ),
        ],
        [
            InlineKeyboardButton(
                "📢 Share Bot",
                url=f"https://t.me/share/url?url=https://t.me/{username}&text=Check out this Advanced Scheduler Bot!",
            ),
        ],
    ])


def main_menu_keyboard(bot: Bot | None = None, user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📅 Schedule New Post", callback_data=CB_SCHEDULE_NEW),
            InlineKeyboardButton("📋 My Posts",          callback_data=CB_MY_POSTS),
        ],
        [
            InlineKeyboardButton("🗂 Manage Queue", callback_data=CB_MANAGE_QUEUE),
            InlineKeyboardButton("🎲 Media Pool",   callback_data=CB_MEDIA_POOL),
        ],
        [
            InlineKeyboardButton("📝 Templates",  callback_data=CB_TEMPLATES),
            InlineKeyboardButton("📊 My Stats",   callback_data=CB_MY_STATS),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
        ],
    ]
    if bot is not None:
        username = _bot_username(bot)
        rows.append([
            InlineKeyboardButton(
                "➕ Add to my Group",
                url=f"https://t.me/{username}?startgroup=true",
            ),
            InlineKeyboardButton(
                "📢 Share Bot",
                url=f"https://t.me/share/url?url=https://t.me/{username}&text=Check out this Advanced Scheduler Bot!",
            ),
        ])
    # Owner-only entry point — never shown to non-admin users. Every
    # owner_panel.py handler re-checks ADMIN_IDS anyway as defense in depth.
    if user_id is not None and user_id in ADMIN_IDS:
        rows.append([
            InlineKeyboardButton("🛠 Owner Panel", callback_data=CB_OWNER_PANEL),
        ])
    return InlineKeyboardMarkup(rows)


def nav_keyboard(back_data: str | None = None) -> InlineKeyboardMarkup:
    """
    Universal navigation row.  Pass `back_data` to enable the Back button;
    omit it (or pass None) to hide Back (e.g. on the first wizard step).
    """
    row: list[InlineKeyboardButton] = []
    if back_data:
        row.append(InlineKeyboardButton("⬅️ Back", callback_data=back_data))
    row.append(InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL))
    return InlineKeyboardMarkup([row])


def confirm_keyboard(confirm_data: str, back_data: str) -> InlineKeyboardMarkup:
    """
    Final summary confirmation keyboard shown before any database write.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirm", callback_data=confirm_data),
            InlineKeyboardButton("⬅️ Back",   callback_data=back_data),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
        ],
    ])


# ---------------------------------------------------------------------------
# Welcome / help text
# ---------------------------------------------------------------------------

WELCOME_TEXT = (
    "👋 *Welcome to the Advanced Scheduler Bot!*\n\n"
    "I can help you schedule posts to any Telegram group or channel "
    "with powerful features:\n\n"
    "🔁 *Recurring posts* — every X minutes / hours / days, or specific weekdays\n"
    "🕐 *Time windows* — only post between set hours (silent-hours support)\n"
    "🗂 *Queue system* — fill predefined daily slots automatically\n"
    "🎲 *Random shuffler* — post randomly from a media pool\n"
    "📌 *Auto-pin / unpin* — pin on send, unpin after a duration\n"
    "🗑 *Auto-delete / self-destruct* — remove posts after X hours or seconds\n\n"
    "Tap *❓ Help* below to explore all features, or *📅 Schedule New Post* to get started."
)

HELP_TEXT = (
    "📖 *Feature Guide*\n\n"
    "*📅 Schedule New Post*\n"
    "  Set up a one-off or recurring post with full lifecycle control.\n\n"
    "*📋 My Posts*\n"
    "  View, pause, resume, or delete your scheduled posts.\n\n"
    "*🗂 Manage Queue*\n"
    "  Define daily posting slots. Content auto-fills the next free slot.\n\n"
    "*🎲 Media Pool*\n"
    "  Upload multiple pieces of content. The bot picks one randomly at each interval.\n\n"
    "*🕐 Time Windows*\n"
    "  Set a daily active window (e.g. 08:00–22:00). Posts outside it are skipped.\n\n"
    "*📌 Lifecycle Options*\n"
    "  Auto-delete after X hours, self-destruct after X seconds, auto-pin+unpin.\n\n"
    "*🔁 Recurrence*\n"
    "  Every X minutes/hours/days · Specific weekdays · Max runs counter."
)


# ---------------------------------------------------------------------------
# My Posts — helpers
# ---------------------------------------------------------------------------

_STATUS_ICON: dict[str, str] = {
    "pending": "⏳",
    "paused":  "⏸",
    "posted":  "✅",
    "failed":  "❌",
}


def _format_next_run(next_run, tz_str: str) -> str:
    """Format a UTC `next_run_at` datetime in the post's own saved timezone."""
    if next_run is None:
        return "—"
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=_dt_timezone.utc)
    try:
        zone = pytz.timezone(tz_str) if tz_str != "UTC" else _dt_timezone.utc
    except pytz.exceptions.UnknownTimeZoneError:
        zone = _dt_timezone.utc
        tz_str = "UTC"
    local_dt = next_run.astimezone(zone)
    label = "UTC" if tz_str == "UTC" else tz_str.split("/")[-1].replace("_", " ")
    return local_dt.strftime(f"%d/%m %H:%M {label}")


def _posts_list_text(posts: list[dict], page: int) -> str:
    total     = len(posts)
    n_pages   = max(1, (total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    start     = page * POSTS_PER_PAGE
    end       = min(start + POSTS_PER_PAGE, total)
    lines     = [f"📋 *My Posts* (page {page + 1}/{n_pages})\n"]
    for idx, post in enumerate(posts[start:end], start=start + 1):
        status  = post.get("status", "?")
        chat_id = post.get("chat_id", "?")
        rec     = post.get("recurrence", {})
        rec_type = rec.get("type", "once")
        next_run = rec.get("next_run_at")
        tz_str   = post.get("timezone", "UTC")
        next_str = _format_next_run(next_run, tz_str)
        icon    = _STATUS_ICON.get(status, "❓")
        lines.append(f"{idx}. {icon} Chat `{chat_id}` · `{rec_type}` · {next_str}")
    return "\n".join(lines)


def _posts_keyboard(posts: list[dict], page: int) -> InlineKeyboardMarkup:
    total  = len(posts)
    start  = page * POSTS_PER_PAGE
    end    = min(start + POSTS_PER_PAGE, total)
    buttons: list[list[InlineKeyboardButton]] = []

    for idx, post in enumerate(posts[start:end], start=start + 1):
        pid    = str(post["_id"])
        status = post.get("status", "?")
        # Row 1: status controls + delete
        ctrl: list[InlineKeyboardButton] = []
        if status == "pending":
            ctrl.append(InlineKeyboardButton(f"⏸ Pause #{idx}",  callback_data=f"{CB_MPP_PAUSE}{pid}"))
        elif status == "paused":
            ctrl.append(InlineKeyboardButton(f"▶️ Resume #{idx}", callback_data=f"{CB_MPP_RESUME}{pid}"))
        ctrl.append(InlineKeyboardButton(f"🗑 Delete #{idx}", callback_data=f"{CB_MPP_DEL}{pid}"))
        buttons.append(ctrl)
        # Row 2: utilities
        buttons.append([
            InlineKeyboardButton(f"🔁 Clone #{idx}",    callback_data=f"{CB_MPP_CLONE}{pid}"),
            InlineKeyboardButton(f"💾 Template #{idx}", callback_data=f"{CB_MPP_SAVE_TMPL}{pid}"),
        ])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{CB_MPP_PAGE}{page - 1}"))
    if end < total:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"{CB_MPP_PAGE}{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([
        InlineKeyboardButton("📊 Statistics",  callback_data=CB_MY_STATS),
        InlineKeyboardButton("🏠 Main Menu",   callback_data=CB_MAIN_MENU),
    ])
    return InlineKeyboardMarkup(buttons)


async def _render_my_posts(
    query: Any,
    user_id: int,
    page: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """Fetch posts and render the My Posts list. Returns the next state."""
    from database import get_user_posts  # lazy import to avoid circular deps
    try:
        posts = await get_user_posts(user_id)
    except Exception as exc:
        logger.exception("get_user_posts failed: %s", exc)
        await query.edit_message_text(
            f"❌ Error loading posts: `{str(exc).replace(chr(96), chr(39))}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)]]
            ),
        )
        return MY_POSTS

    if not posts:
        await query.edit_message_text(
            "📋 *My Posts*\n\nNo posts yet. Tap *📅 Schedule New Post* to create one.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)]]
            ),
        )
        return MY_POSTS

    # Clamp page to valid range
    max_page = max(0, (len(posts) + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE - 1)
    page     = min(page, max_page)
    context.user_data["_mp_page"] = page

    await query.edit_message_text(
        _posts_list_text(posts, page),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_posts_keyboard(posts, page),
    )
    return MY_POSTS


# ---------------------------------------------------------------------------
# Handlers — navigation & main menu
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start — send welcome message with main menu buttons."""
    if not update.message:
        return MAIN_MENU
    user_id = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(
        "🏠 *Main Menu* — choose an action:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(context.bot, user_id),
    )
    return MAIN_MENU


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the help/feature guide."""
    query = update.callback_query
    if not query:
        return MAIN_MENU
    await query.answer()
    await query.edit_message_text(
        HELP_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)],
        ]),
    )
    return MAIN_MENU


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to the main feature menu."""
    query = update.callback_query
    if not query:
        return MAIN_MENU
    await query.answer()
    user_id = query.from_user.id if query.from_user else None
    await query.edit_message_text(
        "🏠 *Main Menu* — choose an action:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(context.bot, user_id),
    )
    return MAIN_MENU


async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Universal cancel handler.
    Clears all user_data wizard state and returns the user to the welcome screen.
    """
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer("Cancelled.")

    # Wipe any in-progress wizard state so the next session starts clean
    context.user_data.clear()

    chat_id = query.message.chat_id if query.message else None
    keep_id = query.message.message_id if query.message else None
    if chat_id is not None:
        await chat_cleanup.cleanup(context, chat_id, keep_message_id=keep_id)

    await query.edit_message_text(
        "❌ *Cancelled.*\n\nUse /start to begin again.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Text-command version of cancel (/cancel)."""
    context.user_data.clear()
    if update.message:
        chat_id = update.message.chat_id
        # Send the confirmation FIRST and keep *that* message — sweeping
        # everything else including the user's own "/cancel" text — so only
        # one message (the confirmation) remains, matching cb_cancel's
        # behaviour.
        final = await update.message.reply_text(
            "❌ *Cancelled.* Use /start to begin again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await chat_cleanup.cleanup(context, chat_id, keep_message_id=final.message_id)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Handlers — My Posts
# ---------------------------------------------------------------------------

async def cb_my_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: show the first page of the user's scheduled posts."""
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS
    await query.answer()
    user_id: int = query.from_user.id
    page = context.user_data.get("_mp_page", 0)
    return await _render_my_posts(query, user_id, page, context)


async def cb_mpp_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Paginate through the posts list."""
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS
    page_str = (query.data or "").replace(CB_MPP_PAGE, "")
    page = int(page_str) if page_str.isdigit() else 0
    await query.answer()
    user_id: int = query.from_user.id
    return await _render_my_posts(query, user_id, page, context)


async def cb_mpp_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pause a pending post."""
    from database import update_post_status
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS
    post_id = (query.data or "").replace(CB_MPP_PAUSE, "")
    try:
        await update_post_status(post_id, "paused")
        await query.answer("⏸ Post paused.")
    except Exception as exc:
        logger.exception("Pause post %s failed: %s", post_id, exc)
        await query.answer(f"Error: {exc}", show_alert=True)
    user_id: int = query.from_user.id
    page = context.user_data.get("_mp_page", 0)
    return await _render_my_posts(query, user_id, page, context)


async def cb_mpp_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Resume a paused post (sets status back to pending)."""
    from database import update_post_status
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS
    post_id = (query.data or "").replace(CB_MPP_RESUME, "")
    try:
        await update_post_status(post_id, "pending")
        await query.answer("▶️ Post resumed.")
    except Exception as exc:
        logger.exception("Resume post %s failed: %s", post_id, exc)
        await query.answer(f"Error: {exc}", show_alert=True)
    user_id: int = query.from_user.id
    page = context.user_data.get("_mp_page", 0)
    return await _render_my_posts(query, user_id, page, context)


async def cb_mpp_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask for delete confirmation."""
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS_CONFIRM_DELETE
    post_id = (query.data or "").replace(CB_MPP_DEL, "")
    await query.answer()
    await query.edit_message_text(
        "⚠️ *Confirm Delete*\n\nThis post will be permanently deleted.\n\nProceed?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, delete", callback_data=f"{CB_MPP_DEL_YES}{post_id}"),
                InlineKeyboardButton("⬅️ Back",        callback_data=CB_MY_POSTS),
            ],
        ]),
    )
    return MY_POSTS_CONFIRM_DELETE


async def cb_mpp_del_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Execute the delete and refresh the list."""
    from database import delete_post
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS
    post_id = (query.data or "").replace(CB_MPP_DEL_YES, "")
    try:
        deleted = await delete_post(post_id)
        await query.answer("Deleted." if deleted else "Already deleted.")
    except Exception as exc:
        logger.exception("Delete post %s failed: %s", post_id, exc)
        await query.answer(f"Error: {exc}", show_alert=True)
    user_id: int = query.from_user.id
    page = context.user_data.get("_mp_page", 0)
    return await _render_my_posts(query, user_id, page, context)


# ---------------------------------------------------------------------------
# Handlers — My Stats
# ---------------------------------------------------------------------------

async def cb_my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show per-channel post statistics for the current user."""
    from database import get_post_stats
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS
    await query.answer()
    user_id = query.from_user.id

    try:
        rows = await get_post_stats(user_id)
    except Exception as exc:
        logger.exception("get_post_stats failed: %s", exc)
        await query.edit_message_text(
            f"❌ Error loading statistics: `{str(exc).replace(chr(96), chr(39))}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)]]),
        )
        return MY_POSTS

    if not rows:
        await query.edit_message_text(
            "📊 *My Statistics*\n\nNo posts found. Schedule something first!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 My Posts", callback_data=CB_MY_POSTS)],
                [InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)],
            ]),
        )
        return MY_POSTS

    lines = ["📊 *Post Statistics*\n"]
    for row in rows:
        chat_id  = row["_id"]
        total    = row["total"]
        sm       = {s["status"]: s["count"] for s in row.get("stats", [])}
        lines.append(
            f"🗂 `{chat_id}` — *{total}* total\n"
            f"  ⏳`{sm.get('pending', 0)}`  ✅`{sm.get('posted', 0)}`"
            f"  ⏸`{sm.get('paused', 0)}`  ❌`{sm.get('failed', 0)}`"
        )

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 My Posts",  callback_data=CB_MY_POSTS)],
            [InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)],
        ]),
    )
    return MY_POSTS


# ---------------------------------------------------------------------------
# Handlers — Clone Post
# ---------------------------------------------------------------------------

async def cb_mpp_clone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Ask for clone confirmation."""
    from database import get_post
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS
    post_id = (query.data or "").replace(CB_MPP_CLONE, "")
    try:
        post = await get_post(post_id)
    except Exception as exc:
        await query.answer(f"Error: {exc}", show_alert=True)
        return MY_POSTS
    if not post:
        await query.answer("Post not found.", show_alert=True)
        return MY_POSTS
    await query.answer()
    chat_id  = post.get("chat_id", "?")
    rec_type = (post.get("recurrence") or {}).get("type", "once")
    await query.edit_message_text(
        f"🔁 *Clone Post*\n\n"
        f"🗂 Chat: `{chat_id}`\n"
        f"🔄 Recurrence: `{rec_type}`\n\n"
        "A copy will be created with the same content and recurrence settings.\n"
        "It will be scheduled to run in *5 minutes* from now.\n\n"
        "Confirm?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Clone",  callback_data=f"{CB_MPP_CLONE_YES}{post_id}"),
                InlineKeyboardButton("⬅️ Back",  callback_data=CB_MY_POSTS),
            ],
        ]),
    )
    return MY_POSTS_CONFIRM_CLONE


async def cb_mpp_clone_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Execute the clone and refresh the My Posts list."""
    from database import clone_post
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS
    post_id = (query.data or "").replace(CB_MPP_CLONE_YES, "")
    try:
        new_id = await clone_post(post_id)
    except Exception as exc:
        logger.exception("Clone post %s failed: %s", post_id, exc)
        await query.answer(f"Error: {exc}", show_alert=True)
        return MY_POSTS
    await query.answer(f"✅ Cloned! ID ends …{new_id[-6:]}")
    user_id: int = query.from_user.id
    page = context.user_data.get("_mp_page", 0)
    return await _render_my_posts(query, user_id, page, context)


# ---------------------------------------------------------------------------
# Handlers — Save as Template
# ---------------------------------------------------------------------------

async def cb_mpp_save_tmpl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt user for a template name after tapping 💾 Template."""
    from database import get_post
    query = update.callback_query
    if not query or not query.from_user:
        return MY_POSTS
    post_id = (query.data or "").replace(CB_MPP_SAVE_TMPL, "")
    try:
        post = await get_post(post_id)
    except Exception as exc:
        await query.answer(f"Error: {exc}", show_alert=True)
        return MY_POSTS
    if not post:
        await query.answer("Post not found.", show_alert=True)
        return MY_POSTS
    await query.answer()
    context.user_data["_save_tmpl_post_id"] = post_id
    content = post.get("content") or {}
    preview = content.get("text") or f"[{content.get('media_type', 'media')}]"
    await query.edit_message_text(
        f"💾 *Save as Template*\n\n"
        f"Content preview: _{str(preview)[:80]}_\n\n"
        "📝 Type a short name for this template (e.g. *Weekly Promo*):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back", callback_data=CB_MY_POSTS)],
        ]),
    )
    return MY_POSTS_SAVE_TEMPLATE


async def recv_template_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the template name from the user and save the template."""
    from database import get_post, save_template
    if not update.message or not update.effective_user:
        return MY_POSTS_SAVE_TEMPLATE
    name    = (update.message.text or "").strip()[:50]
    if not name:
        await update.message.reply_text("⚠️ Please enter a valid name.")
        return MY_POSTS_SAVE_TEMPLATE
    post_id = context.user_data.get("_save_tmpl_post_id")
    user_id = update.effective_user.id
    try:
        post = await get_post(post_id)
        if not post:
            raise ValueError("Post not found")
        content = post.get("content") or {}
        tmpl_id = await save_template(user_id, name, content)
    except Exception as exc:
        logger.exception("save_template failed: %s", exc)
        await update.message.reply_text(
            f"❌ Error saving template: `{str(exc).replace(chr(96), chr(39))}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return MY_POSTS_SAVE_TEMPLATE
    context.user_data.pop("_save_tmpl_post_id", None)
    await update.message.reply_text(
        f"✅ *Template saved!*\n\n"
        f"📝 Name: *{name}*\n"
        f"🆔 ID: `{tmpl_id}`\n\n"
        "Access from *📝 Templates* in the Main Menu.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)]]),
    )
    return MY_POSTS


# ---------------------------------------------------------------------------
# ConversationHandler factory
# ---------------------------------------------------------------------------

def build_base_conversation() -> ConversationHandler:
    """
    Root ConversationHandler — handles /start, navigation, help, cancel,
    and the full My Posts management flow (list / pause / resume / delete).

    Action entry-points for Schedule New Post, Manage Queue, and Media Pool are
    intentionally NOT registered here — they live as entry_points in their own
    higher-priority ConversationHandlers so those wizards always intercept first.
    """
    nav_pattern = f"^({CB_HELP}|{CB_CANCEL}|{CB_MAIN_MENU})$"

    # Patterns that contain a 24-char MongoDB ObjectId suffix
    oid = r"[0-9a-f]{24}"

    return ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(cb_help,      pattern=f"^{CB_HELP}$"),
                CallbackQueryHandler(cb_main_menu, pattern=f"^{CB_MAIN_MENU}$"),
                CallbackQueryHandler(cb_cancel,    pattern=f"^{CB_CANCEL}$"),
                CallbackQueryHandler(cb_my_posts,  pattern=f"^{CB_MY_POSTS}$"),
                CallbackQueryHandler(cb_my_stats,  pattern=f"^{CB_MY_STATS}$"),
            ],
            MY_POSTS: [
                CallbackQueryHandler(cb_my_posts,       pattern=f"^{CB_MY_POSTS}$"),
                CallbackQueryHandler(cb_mpp_page,       pattern=rf"^{CB_MPP_PAGE}\d+$"),
                CallbackQueryHandler(cb_mpp_pause,      pattern=rf"^{CB_MPP_PAUSE}{oid}$"),
                CallbackQueryHandler(cb_mpp_resume,     pattern=rf"^{CB_MPP_RESUME}{oid}$"),
                CallbackQueryHandler(cb_mpp_del,        pattern=rf"^{CB_MPP_DEL}{oid}$"),
                CallbackQueryHandler(cb_mpp_clone,      pattern=rf"^{CB_MPP_CLONE}{oid}$"),
                CallbackQueryHandler(cb_mpp_save_tmpl,  pattern=rf"^{CB_MPP_SAVE_TMPL}{oid}$"),
                CallbackQueryHandler(cb_my_stats,       pattern=f"^{CB_MY_STATS}$"),
                CallbackQueryHandler(cb_main_menu,      pattern=f"^{CB_MAIN_MENU}$"),
                CallbackQueryHandler(cb_cancel,         pattern=f"^{CB_CANCEL}$"),
            ],
            MY_POSTS_CONFIRM_DELETE: [
                CallbackQueryHandler(cb_mpp_del_yes, pattern=rf"^{CB_MPP_DEL_YES}{oid}$"),
                CallbackQueryHandler(cb_my_posts,    pattern=f"^{CB_MY_POSTS}$"),
                CallbackQueryHandler(cb_main_menu,   pattern=f"^{CB_MAIN_MENU}$"),
                CallbackQueryHandler(cb_cancel,      pattern=f"^{CB_CANCEL}$"),
            ],
            MY_POSTS_CONFIRM_CLONE: [
                CallbackQueryHandler(cb_mpp_clone_yes, pattern=rf"^{CB_MPP_CLONE_YES}{oid}$"),
                CallbackQueryHandler(cb_my_posts,      pattern=f"^{CB_MY_POSTS}$"),
                CallbackQueryHandler(cb_main_menu,     pattern=f"^{CB_MAIN_MENU}$"),
                CallbackQueryHandler(cb_cancel,        pattern=f"^{CB_CANCEL}$"),
            ],
            MY_POSTS_SAVE_TEMPLATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_template_name),
                CallbackQueryHandler(cb_my_posts,  pattern=f"^{CB_MY_POSTS}$"),
                CallbackQueryHandler(cb_main_menu, pattern=f"^{CB_MAIN_MENU}$"),
                CallbackQueryHandler(cb_cancel,    pattern=f"^{CB_CANCEL}$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CallbackQueryHandler(cb_main_menu, pattern=nav_pattern),
        ],
        allow_reentry=True,
        name="base_conversation",
        persistent=False,
    )
