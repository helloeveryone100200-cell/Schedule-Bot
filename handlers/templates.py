"""
handlers/templates.py — Post Template management.

Allows users to view and delete saved templates.
Templates are saved from My Posts → 💾 Template button.
Templates are loaded in Schedule Wizard → Step 2 → 📝 Use Template.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
)

from database import list_templates, delete_template
from handlers.base import (
    CB_CANCEL, CB_MAIN_MENU, CB_TEMPLATES,
    cmd_cancel, main_menu_keyboard,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
TMPL_MENU        = 200
TMPL_CONFIRM_DEL = 201

# ---------------------------------------------------------------------------
# Callback-data constants
# ---------------------------------------------------------------------------
CB_TMPL_PAGE    = "tmpl:page:"      # + page number
CB_TMPL_DEL     = "tmpl:del:"       # + template ObjectId
CB_TMPL_DEL_YES = "tmpl:delyes:"    # + template ObjectId

_PAGE_SIZE = 5


# ---------------------------------------------------------------------------
# Keyboard builder
# ---------------------------------------------------------------------------

def _tmpl_list_keyboard(templates: list[dict], page: int) -> InlineKeyboardMarkup:
    total = len(templates)
    start = page * _PAGE_SIZE
    end   = min(start + _PAGE_SIZE, total)
    rows: list[list[InlineKeyboardButton]] = []

    for tmpl in templates[start:end]:
        tid  = str(tmpl["_id"])
        name = tmpl.get("name", "Unnamed")[:40]
        rows.append([
            InlineKeyboardButton(f"📄 {name}", callback_data=f"tmpl:view:{tid}"),
            InlineKeyboardButton("🗑 Delete",  callback_data=f"{CB_TMPL_DEL}{tid}"),
        ])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{CB_TMPL_PAGE}{page - 1}"))
    if end < total:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"{CB_TMPL_PAGE}{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Entry & pagination
# ---------------------------------------------------------------------------

async def enter_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.from_user:
        return TMPL_MENU
    await query.answer()
    user_id = query.from_user.id

    try:
        templates = await list_templates(user_id)
    except Exception as exc:
        logger.exception("list_templates failed: %s", exc)
        await query.edit_message_text(
            f"❌ Error loading templates: `{str(exc).replace(chr(96), chr(39))}`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)]]),
        )
        return TMPL_MENU

    if not templates:
        await query.edit_message_text(
            "📝 *My Templates*\n\n"
            "No templates saved yet.\n\n"
            "Go to *📋 My Posts* and tap *💾 Template #N* on any post to save it.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data=CB_MAIN_MENU)]]),
        )
        return TMPL_MENU

    context.user_data["_tmpl_page"] = 0
    await query.edit_message_text(
        f"📝 *My Templates* — {len(templates)} saved\n\n"
        "💡 To use a template: *Schedule New Post → Step 2 → 📝 Use Template*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_tmpl_list_keyboard(templates, 0),
    )
    return TMPL_MENU


async def cb_tmpl_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.from_user:
        return TMPL_MENU
    page    = int((query.data or "0").replace(CB_TMPL_PAGE, "") or "0")
    user_id = query.from_user.id
    try:
        templates = await list_templates(user_id)
    except Exception as exc:
        await query.answer(f"Error: {exc}", show_alert=True)
        return TMPL_MENU
    await query.answer()
    context.user_data["_tmpl_page"] = page
    await query.edit_message_text(
        f"📝 *My Templates* — {len(templates)} saved\n\n"
        "💡 To use a template: *Schedule New Post → Step 2 → 📝 Use Template*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_tmpl_list_keyboard(templates, page),
    )
    return TMPL_MENU


# ---------------------------------------------------------------------------
# Delete flow
# ---------------------------------------------------------------------------

async def cb_tmpl_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return TMPL_MENU
    tid = (query.data or "").replace(CB_TMPL_DEL, "")
    await query.answer()
    await query.edit_message_text(
        "🗑 *Delete Template*\n\nAre you sure? This cannot be undone.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, delete", callback_data=f"{CB_TMPL_DEL_YES}{tid}"),
                InlineKeyboardButton("⬅️ Back",        callback_data=CB_TEMPLATES),
            ],
        ]),
    )
    return TMPL_CONFIRM_DEL


async def cb_tmpl_del_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.from_user:
        return TMPL_MENU
    tid = (query.data or "").replace(CB_TMPL_DEL_YES, "")
    try:
        deleted = await delete_template(tid)
        await query.answer("Deleted." if deleted else "Already deleted.")
    except Exception as exc:
        logger.exception("delete_template failed: %s", exc)
        await query.answer(f"Error: {exc}", show_alert=True)
    # Refresh the list
    return await enter_templates(update, context)


# ---------------------------------------------------------------------------
# Main-menu redirect & cancel
# ---------------------------------------------------------------------------

async def cb_tmpl_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    user_id = query.from_user.id if query.from_user else None
    await query.edit_message_text(
        "🏠 *Main Menu* — choose an action:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(bot=context.bot, user_id=user_id),
    )
    return ConversationHandler.END


async def cb_tmpl_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer("Cancelled.")
    user_id = query.from_user.id if query.from_user else None
    await query.edit_message_text(
        "❌ *Cancelled.* Back to Main Menu.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu_keyboard(bot=context.bot, user_id=user_id),
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler factory
# ---------------------------------------------------------------------------

def build_templates_conversation() -> ConversationHandler:
    oid = r"[0-9a-f]{24}"
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(enter_templates, pattern=f"^{CB_TEMPLATES}$")],
        states={
            TMPL_MENU: [
                CallbackQueryHandler(enter_templates, pattern=f"^{CB_TEMPLATES}$"),
                CallbackQueryHandler(cb_tmpl_page,   pattern=rf"^{CB_TMPL_PAGE}\d+$"),
                CallbackQueryHandler(cb_tmpl_del,    pattern=rf"^{CB_TMPL_DEL}{oid}$"),
                CallbackQueryHandler(cb_tmpl_main_menu, pattern=f"^{CB_MAIN_MENU}$"),
            ],
            TMPL_CONFIRM_DEL: [
                CallbackQueryHandler(cb_tmpl_del_yes,   pattern=rf"^{CB_TMPL_DEL_YES}{oid}$"),
                CallbackQueryHandler(enter_templates,    pattern=f"^{CB_TEMPLATES}$"),
                CallbackQueryHandler(cb_tmpl_main_menu, pattern=f"^{CB_MAIN_MENU}$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CallbackQueryHandler(cb_tmpl_cancel, pattern=f"^{CB_CANCEL}$"),
        ],
        allow_reentry=True,
        name="templates_conversation",
        persistent=False,
    )
