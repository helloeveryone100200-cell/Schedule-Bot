"""
handlers/base.py — /start, /help, /cancel, and universal navigation guardrails.

Navigation pattern used throughout the bot:
  - Every wizard step includes ⬅️ Back and ❌ Cancel inline buttons.
  - Before committing to the database a full summary is shown with ✅ Confirm.
  - ❌ Cancel always resets ConversationHandler state and returns to the main menu.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Callback-data constants  (all ≤ 64 bytes — well within Telegram's 64-byte limit)
# ---------------------------------------------------------------------------
CB_HELP = "nav:help"
CB_CANCEL = "nav:cancel"
CB_MAIN_MENU = "nav:main_menu"
CB_SCHEDULE_NEW = "action:schedule_new"
CB_MY_POSTS = "action:my_posts"
CB_MANAGE_QUEUE = "action:manage_queue"
CB_MEDIA_POOL = "action:media_pool"

# ---------------------------------------------------------------------------
# Conversation states (used by the base ConversationHandler)
# ---------------------------------------------------------------------------
MAIN_MENU = 0


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


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Schedule New Post", callback_data=CB_SCHEDULE_NEW),
            InlineKeyboardButton("📋 My Posts", callback_data=CB_MY_POSTS),
        ],
        [
            InlineKeyboardButton("🗂 Manage Queue", callback_data=CB_MANAGE_QUEUE),
            InlineKeyboardButton("🎲 Media Pool", callback_data=CB_MEDIA_POOL),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data=CB_CANCEL),
        ],
    ])


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
            InlineKeyboardButton("⬅️ Back", callback_data=back_data),
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
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start — send welcome message with action buttons."""
    assert update.message is not None
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=start_keyboard(context.bot),
    )
    return MAIN_MENU


async def cb_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show the help/feature guide."""
    query = update.callback_query
    assert query is not None
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
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        "🏠 *Main Menu* — choose an action:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(),
    )
    return MAIN_MENU


async def cb_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Universal cancel handler.
    Clears all user_data wizard state and returns the user to the welcome screen.
    """
    query = update.callback_query
    assert query is not None
    await query.answer("Cancelled.")

    # Wipe any in-progress wizard state so the next session starts clean
    context.user_data.clear()

    await query.edit_message_text(
        "❌ *Cancelled.*\n\nUse /start to begin again.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Text-command version of cancel (/cancel)."""
    assert update.message is not None
    context.user_data.clear()
    await update.message.reply_text(
        "❌ *Cancelled.* Use /start to begin again.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END


# Stub handlers for menu actions — these will be replaced by full wizards in Steps 4 & 5
async def cb_schedule_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        "📅 *Schedule New Post* — wizard coming in Step 4!\n\n"
        "This will walk you through setting up a post with recurrence, "
        "lifecycle, and time-window options.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=nav_keyboard(back_data=CB_MAIN_MENU),
    )
    return MAIN_MENU


async def cb_my_posts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        "📋 *My Posts* — coming soon!\n\nYou'll be able to list, pause, resume, and delete your posts here.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=nav_keyboard(back_data=CB_MAIN_MENU),
    )
    return MAIN_MENU


async def cb_manage_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        "🗂 *Queue Manager* — coming in Step 5!\n\nSet daily posting slots and auto-fill them.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=nav_keyboard(back_data=CB_MAIN_MENU),
    )
    return MAIN_MENU


async def cb_media_pool(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    assert query is not None
    await query.answer()
    await query.edit_message_text(
        "🎲 *Media Pool* — coming in Step 5!\n\nDrop content here for the random shuffler.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=nav_keyboard(back_data=CB_MAIN_MENU),
    )
    return MAIN_MENU


# ---------------------------------------------------------------------------
# ConversationHandler factory
# ---------------------------------------------------------------------------

def build_base_conversation() -> ConversationHandler:
    """
    Build and return the root ConversationHandler.
    Steps 4 & 5 will nest their own ConversationHandlers into this structure.
    """
    return ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MAIN_MENU: [
                CallbackQueryHandler(cb_help, pattern=f"^{CB_HELP}$"),
                CallbackQueryHandler(cb_main_menu, pattern=f"^{CB_MAIN_MENU}$"),
                CallbackQueryHandler(cb_cancel, pattern=f"^{CB_CANCEL}$"),
                CallbackQueryHandler(cb_schedule_new, pattern=f"^{CB_SCHEDULE_NEW}$"),
                CallbackQueryHandler(cb_my_posts, pattern=f"^{CB_MY_POSTS}$"),
                CallbackQueryHandler(cb_manage_queue, pattern=f"^{CB_MANAGE_QUEUE}$"),
                CallbackQueryHandler(cb_media_pool, pattern=f"^{CB_MEDIA_POOL}$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            # Catch-all: any unhandled callback returns user to main menu
            CallbackQueryHandler(cb_main_menu),
        ],
        allow_reentry=True,
        name="base_conversation",
        persistent=False,
    )
