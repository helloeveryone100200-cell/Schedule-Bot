"""
handlers/owner_panel.py — Bot-owner-only admin panel.

Only chat IDs listed in config.ADMIN_IDS ever see the "🛠 Owner Panel" button
on /start (added in handlers/base.py's main_menu_keyboard). Every handler in
this module double-checks the caller against ADMIN_IDS again before doing
anything, so even a crafted callback_data can't be replayed by a non-owner.

Features:
  - 📊 Status        — live counts: users, groups, posts by status, queues, pools
  - 👤 User List      — paginated list of every user_id who has ever messaged the bot
  - 👥 Group List     — paginated list of every group/channel the bot is active in
  - 📣 Broadcast All  — send a message to every tracked user
  - 🎯 Broadcast One  — send a message to one specific user_id ("broadcast signal")
  - 🧹 Clear Data     — wipe all scheduled posts / queues / media pools (double-confirm)
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import ADMIN_IDS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Callback-data constants
# ---------------------------------------------------------------------------
CB_OWNER_PANEL      = "owner:panel"
CB_OWNER_STATUS     = "owner:status"
CB_OWNER_USERS      = "owner:users"
CB_OWNER_GROUPS     = "owner:groups"
CB_OWNER_BCAST_ALL  = "owner:bcast_all"
CB_OWNER_BCAST_ONE  = "owner:bcast_one"
CB_OWNER_CLEARDATA  = "owner:cleardata"
CB_OWNER_CLEAR_YES  = "owner:cleardata_yes"
CB_OWNER_BACK       = "owner:back"

CB_OWNER_USERS_PAGE  = "owner:userspage:"   # + page number
CB_OWNER_GROUPS_PAGE = "owner:groupspage:"  # + page number

PAGE_SIZE = 20

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
OWNER_MENU           = 100
OWNER_LIST           = 101
OWNER_BCAST_ALL_WAIT = 102
OWNER_BCAST_ONE_WAIT_ID  = 103
OWNER_BCAST_ONE_WAIT_MSG = 104
OWNER_CLEARDATA_CONFIRM  = 105


def _is_owner(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def _owner_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status",    callback_data=CB_OWNER_STATUS),
        ],
        [
            InlineKeyboardButton("👤 User List",  callback_data=CB_OWNER_USERS),
            InlineKeyboardButton("👥 Group List", callback_data=CB_OWNER_GROUPS),
        ],
        [
            InlineKeyboardButton("📣 Broadcast All", callback_data=CB_OWNER_BCAST_ALL),
            InlineKeyboardButton("🎯 Broadcast One", callback_data=CB_OWNER_BCAST_ONE),
        ],
        [
            InlineKeyboardButton("🧹 Clear Data", callback_data=CB_OWNER_CLEARDATA),
        ],
        [
            InlineKeyboardButton("🏠 Main Menu", callback_data="nav:main_menu"),
        ],
    ])


def _back_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Owner Panel", callback_data=CB_OWNER_BACK)],
    ])


def _cancel_only_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=CB_OWNER_BACK)],
    ])


async def cb_owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — show the owner panel main menu."""
    query = update.callback_query
    if not query or not query.from_user:
        return ConversationHandler.END
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "🛠 *Owner Panel*\n\nChoose a tool:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_owner_menu_keyboard(),
    )
    return OWNER_MENU


async def cb_owner_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Return to the owner panel main menu from any sub-screen."""
    query = update.callback_query
    if not query or not query.from_user:
        return ConversationHandler.END
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    context.user_data.pop("_owner_bcast_target", None)
    await query.edit_message_text(
        "🛠 *Owner Panel*\n\nChoose a tool:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_owner_menu_keyboard(),
    )
    return OWNER_MENU


async def cb_owner_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show live counts of users, groups, posts, queues, pools."""
    from database import get_bot_stats

    query = update.callback_query
    if not query or not query.from_user:
        return OWNER_MENU
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    try:
        stats = await get_bot_stats()
    except Exception as exc:
        logger.exception("get_bot_stats failed: %s", exc)
        await query.edit_message_text(
            f"❌ Error loading status: `{exc}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_back_only_keyboard(),
        )
        return OWNER_MENU

    text = (
        "📊 *Bot Status*\n\n"
        f"👤 Users tracked: `{stats['users']}`\n"
        f"👥 Groups/channels tracked: `{stats['groups']}`\n\n"
        f"📝 Scheduled posts total: `{stats['posts_total']}`\n"
        f"  ⏳ Pending: `{stats['posts_pending']}`\n"
        f"  ⏸ Paused: `{stats['posts_paused']}`\n"
        f"  ✅ Posted: `{stats['posts_posted']}`\n"
        f"  ❌ Failed: `{stats['posts_failed']}`\n\n"
        f"🗂 Queue slots: `{stats['queues']}`\n"
        f"🎲 Media pools: `{stats['pools']}`"
    )
    await query.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=_back_only_keyboard()
    )
    return OWNER_MENU


async def _render_id_list(query, ids: list[int], page: int, label: str, page_cb_prefix: str) -> None:
    total = len(ids)
    n_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, n_pages - 1))
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    chunk = ids[start:end]

    lines = [f"{label} (page {page + 1}/{n_pages}, total {total})\n"]
    for idx, item_id in enumerate(chunk, start=start + 1):
        lines.append(f"{idx}. `{item_id}`")
    if not chunk:
        lines.append("_None yet._")

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{page_cb_prefix}{page - 1}"))
    if end < total:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"{page_cb_prefix}{page + 1}"))
    buttons = [nav_row] if nav_row else []
    buttons.append([InlineKeyboardButton("⬅️ Back to Owner Panel", callback_data=CB_OWNER_BACK)])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_owner_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from database import list_user_ids

    query = update.callback_query
    if not query or not query.from_user:
        return OWNER_MENU
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    try:
        ids = await list_user_ids()
    except Exception as exc:
        logger.exception("list_user_ids failed: %s", exc)
        await query.edit_message_text(
            f"❌ Error: `{exc}`", parse_mode=ParseMode.MARKDOWN, reply_markup=_back_only_keyboard()
        )
        return OWNER_MENU
    await _render_id_list(query, ids, 0, "👤 *User List*", CB_OWNER_USERS_PAGE)
    return OWNER_LIST


async def cb_owner_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from database import list_user_ids

    query = update.callback_query
    if not query or not query.from_user:
        return OWNER_MENU
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    page_str = (query.data or "").replace(CB_OWNER_USERS_PAGE, "")
    page = int(page_str) if page_str.isdigit() else 0
    await query.answer()
    ids = await list_user_ids()
    await _render_id_list(query, ids, page, "👤 *User List*", CB_OWNER_USERS_PAGE)
    return OWNER_LIST


async def cb_owner_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from database import list_group_ids

    query = update.callback_query
    if not query or not query.from_user:
        return OWNER_MENU
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    try:
        ids = await list_group_ids()
    except Exception as exc:
        logger.exception("list_group_ids failed: %s", exc)
        await query.edit_message_text(
            f"❌ Error: `{exc}`", parse_mode=ParseMode.MARKDOWN, reply_markup=_back_only_keyboard()
        )
        return OWNER_MENU
    await _render_id_list(query, ids, 0, "👥 *Group List*", CB_OWNER_GROUPS_PAGE)
    return OWNER_LIST


async def cb_owner_groups_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from database import list_group_ids

    query = update.callback_query
    if not query or not query.from_user:
        return OWNER_MENU
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    page_str = (query.data or "").replace(CB_OWNER_GROUPS_PAGE, "")
    page = int(page_str) if page_str.isdigit() else 0
    await query.answer()
    ids = await list_group_ids()
    await _render_id_list(query, ids, page, "👥 *Group List*", CB_OWNER_GROUPS_PAGE)
    return OWNER_LIST


# ---------------------------------------------------------------------------
# Broadcast — all users
# ---------------------------------------------------------------------------

async def cb_owner_bcast_all_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.from_user:
        return OWNER_MENU
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "📣 *Broadcast to All Users*\n\n"
        "Send the message you want delivered to every tracked user "
        "(text only). Send /cancel_owner to abort.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_only_keyboard(),
    )
    return OWNER_BCAST_ALL_WAIT


async def msg_owner_bcast_all_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from database import list_user_ids

    if not update.message or not update.effective_user:
        return OWNER_BCAST_ALL_WAIT
    if not _is_owner(update.effective_user.id):
        return ConversationHandler.END
    text = update.message.text
    if not text:
        await update.message.reply_text("Please send text only.")
        return OWNER_BCAST_ALL_WAIT

    ids = await list_user_ids()
    sent, failed = 0, 0
    status_msg = await update.message.reply_text(f"Sending to {len(ids)} users…")
    for uid in ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Forbidden:
            failed += 1
        except TelegramError:
            failed += 1
    await status_msg.edit_text(
        f"📣 *Broadcast complete.*\n\n✅ Delivered: `{sent}`\n❌ Failed/blocked: `{failed}`",
        parse_mode=ParseMode.MARKDOWN,
    )
    await update.message.reply_text(
        "🛠 *Owner Panel*\n\nChoose a tool:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_owner_menu_keyboard(),
    )
    return OWNER_MENU


# ---------------------------------------------------------------------------
# Broadcast — single user ("broadcast signal")
# ---------------------------------------------------------------------------

async def cb_owner_bcast_one_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.from_user:
        return OWNER_MENU
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "🎯 *Broadcast to One User*\n\n"
        "Send the numeric *user ID* to message. Send /cancel_owner to abort.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_cancel_only_keyboard(),
    )
    return OWNER_BCAST_ONE_WAIT_ID


async def msg_owner_bcast_one_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return OWNER_BCAST_ONE_WAIT_ID
    if not _is_owner(update.effective_user.id):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text("That doesn't look like a numeric user ID. Try again, or /cancel_owner.")
        return OWNER_BCAST_ONE_WAIT_ID
    context.user_data["_owner_bcast_target"] = int(text)
    await update.message.reply_text(
        f"Target set to `{text}`.\n\nNow send the message text to deliver.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return OWNER_BCAST_ONE_WAIT_MSG


async def msg_owner_bcast_one_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return OWNER_BCAST_ONE_WAIT_MSG
    if not _is_owner(update.effective_user.id):
        return ConversationHandler.END
    target = context.user_data.get("_owner_bcast_target")
    text = update.message.text
    if target is None:
        await update.message.reply_text("No target set — starting over. Use the Owner Panel again.")
        return ConversationHandler.END
    if not text:
        await update.message.reply_text("Please send text only.")
        return OWNER_BCAST_ONE_WAIT_MSG

    try:
        await context.bot.send_message(chat_id=target, text=text)
        result = f"✅ Delivered to `{target}`."
    except Forbidden:
        result = f"❌ `{target}` has blocked the bot or never started it."
    except TelegramError as exc:
        result = f"❌ Failed to deliver to `{target}`: `{exc}`"

    context.user_data.pop("_owner_bcast_target", None)
    await update.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)
    await update.message.reply_text(
        "🛠 *Owner Panel*\n\nChoose a tool:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_owner_menu_keyboard(),
    )
    return OWNER_MENU


async def cmd_cancel_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/cancel_owner — abort any in-progress owner-panel broadcast flow."""
    if not update.effective_user or not _is_owner(update.effective_user.id):
        return ConversationHandler.END
    context.user_data.pop("_owner_bcast_target", None)
    if update.message:
        await update.message.reply_text(
            "❌ Cancelled.",
            reply_markup=_owner_menu_keyboard(),
        )
    return OWNER_MENU


# ---------------------------------------------------------------------------
# Clear Data (double-confirm — destructive)
# ---------------------------------------------------------------------------

async def cb_owner_cleardata_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.from_user:
        return OWNER_MENU
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "⚠️ *Clear All Scheduled Data*\n\n"
        "This permanently deletes:\n"
        "• All scheduled posts (pending, paused, posted, failed)\n"
        "• All queue slots\n"
        "• All media pools\n\n"
        "Tracked user/group lists are kept so broadcasting keeps working.\n\n"
        "*This cannot be undone.* Proceed?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, wipe it all", callback_data=CB_OWNER_CLEAR_YES),
                InlineKeyboardButton("⬅️ Back", callback_data=CB_OWNER_BACK),
            ],
        ]),
    )
    return OWNER_CLEARDATA_CONFIRM


async def cb_owner_cleardata_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    from database import clear_all_bot_data

    query = update.callback_query
    if not query or not query.from_user:
        return OWNER_MENU
    if not _is_owner(query.from_user.id):
        await query.answer("⛔ Owner only.", show_alert=True)
        return ConversationHandler.END
    await query.answer("Wiping…")
    try:
        result = await clear_all_bot_data()
    except Exception as exc:
        logger.exception("clear_all_bot_data failed: %s", exc)
        await query.edit_message_text(
            f"❌ Clear failed: `{exc}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_back_only_keyboard(),
        )
        return OWNER_MENU

    await query.edit_message_text(
        "🧹 *Data cleared.*\n\n"
        f"Posts removed: `{result['posts']}`\n"
        f"Queue slots removed: `{result['queues']}`\n"
        f"Media pools removed: `{result['pools']}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_back_only_keyboard(),
    )
    return OWNER_MENU


# ---------------------------------------------------------------------------
# ConversationHandler factory
# ---------------------------------------------------------------------------

def build_owner_panel() -> ConversationHandler:
    """
    Owner-only admin panel. Entry point is the "🛠 Owner Panel" inline button
    shown on /start (only rendered for user IDs in config.ADMIN_IDS — see
    handlers/base.py main_menu_keyboard). Every handler additionally
    re-checks ADMIN_IDS so a copied callback_data can't be replayed by
    someone else.
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_owner_panel, pattern=f"^{CB_OWNER_PANEL}$"),
        ],
        states={
            OWNER_MENU: [
                CallbackQueryHandler(cb_owner_status,          pattern=f"^{CB_OWNER_STATUS}$"),
                CallbackQueryHandler(cb_owner_users,           pattern=f"^{CB_OWNER_USERS}$"),
                CallbackQueryHandler(cb_owner_groups,          pattern=f"^{CB_OWNER_GROUPS}$"),
                CallbackQueryHandler(cb_owner_bcast_all_start, pattern=f"^{CB_OWNER_BCAST_ALL}$"),
                CallbackQueryHandler(cb_owner_bcast_one_start, pattern=f"^{CB_OWNER_BCAST_ONE}$"),
                CallbackQueryHandler(cb_owner_cleardata_confirm, pattern=f"^{CB_OWNER_CLEARDATA}$"),
                CallbackQueryHandler(cb_owner_back,            pattern=f"^{CB_OWNER_BACK}$"),
            ],
            OWNER_LIST: [
                CallbackQueryHandler(cb_owner_users_page,  pattern=rf"^{CB_OWNER_USERS_PAGE}\d+$"),
                CallbackQueryHandler(cb_owner_groups_page, pattern=rf"^{CB_OWNER_GROUPS_PAGE}\d+$"),
                CallbackQueryHandler(cb_owner_back,        pattern=f"^{CB_OWNER_BACK}$"),
            ],
            OWNER_BCAST_ALL_WAIT: [
                CallbackQueryHandler(cb_owner_back, pattern=f"^{CB_OWNER_BACK}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_owner_bcast_all_send),
            ],
            OWNER_BCAST_ONE_WAIT_ID: [
                CallbackQueryHandler(cb_owner_back, pattern=f"^{CB_OWNER_BACK}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_owner_bcast_one_id),
            ],
            OWNER_BCAST_ONE_WAIT_MSG: [
                CallbackQueryHandler(cb_owner_back, pattern=f"^{CB_OWNER_BACK}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_owner_bcast_one_send),
            ],
            OWNER_CLEARDATA_CONFIRM: [
                CallbackQueryHandler(cb_owner_cleardata_yes, pattern=f"^{CB_OWNER_CLEAR_YES}$"),
                CallbackQueryHandler(cb_owner_back,          pattern=f"^{CB_OWNER_BACK}$"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cb_owner_back, pattern=f"^{CB_OWNER_BACK}$"),
            CommandHandler("cancel_owner", cmd_cancel_owner),
        ],
        allow_reentry=True,
        name="owner_panel_conversation",
        persistent=False,
    )
