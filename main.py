"""
main.py — Application entry point.

Boot order:
  1. Start keep-alive Flask server (background thread — Render/UptimeRobot)
  2. Connect to MongoDB
  3. Start APScheduler (shares the asyncio event loop with PTB)
  4. Start Telegram bot (polling)
  5. On shutdown: stop scheduler → close MongoDB
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from config import MONGO_URI, PORT, TELEGRAM_BOT_TOKEN
from database import close_db, connect_db, record_chat_activity, upsert_group, upsert_user
from handlers import chat_cleanup
from handlers.base import build_base_conversation
from handlers.owner_panel import build_owner_panel
from handlers.queue_manager import build_media_pool, build_queue_manager
from handlers.schedule_wizard import build_schedule_wizard
from keep_alive import start_keep_alive
from scheduler import setup_scheduler

logger = logging.getLogger(__name__)

# Increment this string whenever a significant update is deployed.
# Users can type /ping to confirm which version is running on Render.
BOT_VERSION = "2026-07-10-v4 | /id ✅ | Owner Panel ✅ | Main Menu everywhere ✅"

# Module-level scheduler reference so post_shutdown can stop it
_scheduler = None


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ping — reply with version info so users can confirm the deployment."""
    if not update.message:
        return
    try:
        await update.message.reply_text("pong — " + BOT_VERSION)
    except Exception as exc:
        logger.exception("cmd_ping failed: %s", exc)
        try:
            await update.message.reply_text("pong (error: " + str(exc) + ")")
        except Exception:
            pass


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/id — reply with this chat's title/ID and the sender's user ID."""
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    from telegram.helpers import escape_markdown

    chat = update.effective_chat
    user = update.effective_user
    title = escape_markdown(chat.title or chat.first_name or "Private Chat", version=1)
    try:
        await update.message.reply_text(
            f"ℹ️ *Group Help*\n"
            f"{title}\n\n"
            f"`/id`\n\n"
            f"*CHAT ID:* `{chat.id}`\n"
            f"*YOUR ID:* `{user.id}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as exc:
        logger.exception("cmd_id failed: %s", exc)


async def post_init(application: Application) -> None:
    """
    Called by PTB after the Application is initialised and the event loop is running.
    This is the correct place to start APScheduler — both PTB and APScheduler share
    the same asyncio loop, so starting here prevents any event-loop conflicts.
    """
    global _scheduler

    # 1. Connect to MongoDB
    await connect_db(MONGO_URI)
    logger.info("MongoDB connected.")

    # 2. Set up and start APScheduler on the SAME event loop as PTB.
    #    asyncio.get_running_loop() is the correct modern idiom (Python 3.10+).
    #    get_event_loop() inside an async coroutine raises DeprecationWarning in 3.10+.
    loop = asyncio.get_running_loop()
    _scheduler = setup_scheduler(application.bot, event_loop=loop)
    _scheduler.start()
    logger.info("APScheduler started (loop id=%d).", id(loop))


async def _track_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Low-priority (group=-1) tracker: records every incoming message/callback
    message id so chat_cleanup can later wipe an entire wizard's back-and-forth
    once it finishes. Never blocks or mutates the update.

    Also opportunistically records the sending user / chat into MongoDB so the
    owner panel's Status / User List / Group List / Broadcast tools have data
    to work with. Best-effort — failures are logged and swallowed so a DB
    hiccup never breaks normal bot flow.
    """
    chat = update.effective_chat
    msg = update.effective_message
    if chat is not None and msg is not None:
        chat_cleanup.track(context.application.bot_data, chat.id, msg.message_id)

    user = update.effective_user
    if user is not None and not user.is_bot:
        try:
            await upsert_user(user.id, user.username, user.first_name)
        except Exception:
            logger.exception("upsert_user tracking failed for user_id=%s", user.id)

    if chat is not None and chat.type in ("group", "supergroup", "channel"):
        try:
            await upsert_group(chat.id, chat.title, chat.type)
        except Exception:
            logger.exception("upsert_group tracking failed for chat_id=%s", chat.id)
        if msg is not None and msg.date is not None:
            try:
                await record_chat_activity(chat.id, msg.date.hour)
            except Exception:
                logger.exception("record_chat_activity failed for chat_id=%s", chat.id)


# Holds a reference to the running Application's bot_data dict. Populated once
# in main() after the Application is built. A module-level variable is used
# (rather than an attribute on the Bot instance) because PTB's TelegramObject
# defines a strict `__setattr__` that raises AttributeError for any attribute
# name not declared in its (and every parent class's) `__slots__` — so even a
# fresh instance attribute on a subclass cannot be set after construction.
_bot_data_ref: dict | None = None


class _TrackedBot(Bot):
    """
    Bot subclass that records every outgoing message id via chat_cleanup, so
    wizard flows can later wipe them without touching every reply_text()/
    send_message() call site across handlers/base.py, schedule_wizard.py,
    queue_manager.py, etc.

    A plain instance-attribute monkey-patch (`app.bot.send_message = ...`)
    does NOT work here: PTB's Bot uses __slots__ and a strict __setattr__,
    so assigning to the instance raises AttributeError. Subclassing to
    override the method works, but storing state also requires going
    through the module-level `_bot_data_ref` above rather than `self.x = ...`.
    """

    async def send_message(self, chat_id=None, *args, **kwargs):
        message = await super().send_message(chat_id, *args, **kwargs)
        if chat_id is not None and message is not None and _bot_data_ref is not None:
            chat_cleanup.track(_bot_data_ref, chat_id, message.message_id)
        return message


async def post_shutdown(application: Application) -> None:
    """Graceful teardown: stop scheduler, then close DB."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
    await close_db()
    logger.info("MongoDB disconnected. Shutdown complete.")


def main() -> None:
    logger.info("Starting Telegram Advanced Scheduler Bot…")
    logger.info("Version: %s", BOT_VERSION)

    # Keep-alive server (daemon thread) — must start before PTB blocks the loop
    start_keep_alive(PORT)

    # Build PTB Application, using a _TrackedBot instance (instead of
    # .token(...)) so outgoing sends are recorded for chat_cleanup.
    tracked_bot = _TrackedBot(token=TELEGRAM_BOT_TOKEN)
    app: Application = (
        ApplicationBuilder()
        .bot(tracked_bot)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    global _bot_data_ref
    _bot_data_ref = app.bot_data

    # /ping — deployment verification (registered before ConversationHandlers so
    # it is always reachable regardless of the user's conversation state)
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("id", cmd_id))

    # Chat cleanup tracking: record incoming messages/callbacks at low
    # priority (group=-1) so they don't interfere with normal handler
    # dispatch order. Outgoing sends are tracked by _TrackedBot above.
    app.add_handler(MessageHandler(filters.ALL, _track_incoming), group=-1)
    app.add_handler(CallbackQueryHandler(_track_incoming), group=-1)

    # Register ConversationHandlers — specific wizards first (higher priority)
    app.add_handler(build_schedule_wizard())
    app.add_handler(build_queue_manager())
    app.add_handler(build_media_pool())
    app.add_handler(build_owner_panel())
    app.add_handler(build_base_conversation())

    logger.info("Bot starting — polling for updates…")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,   # ignore stale updates accumulated during downtime
    )


if __name__ == "__main__":
    main()
