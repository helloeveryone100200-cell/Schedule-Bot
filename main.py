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

from telegram import Update
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
from database import close_db, connect_db
from handlers import chat_cleanup
from handlers.base import build_base_conversation
from handlers.queue_manager import build_media_pool, build_queue_manager
from handlers.schedule_wizard import build_schedule_wizard
from keep_alive import start_keep_alive
from scheduler import setup_scheduler

logger = logging.getLogger(__name__)

# Increment this string whenever a significant update is deployed.
# Users can type /ping to confirm which version is running on Render.
BOT_VERSION = "2026-07-08-v3 | My Posts ✅ | Time-Window Next ✅ | Lifecycle retry ✅"

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
    """
    chat = update.effective_chat
    msg = update.effective_message
    if chat is not None and msg is not None:
        chat_cleanup.track(context.application.bot_data, chat.id, msg.message_id)


def _patch_outgoing_tracking(app: Application) -> None:
    """
    Monkey-patch Bot.send_message so every message the BOT sends is recorded
    automatically, without touching every reply_text()/send_message() call
    site across handlers/base.py, schedule_wizard.py, queue_manager.py, etc.
    """
    original_send_message = app.bot.send_message

    async def tracked_send_message(chat_id=None, *args, **kwargs):
        message = await original_send_message(chat_id, *args, **kwargs)
        if chat_id is not None and message is not None:
            chat_cleanup.track(app.bot_data, chat_id, message.message_id)
        return message

    app.bot.send_message = tracked_send_message


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

    # Build PTB Application
    app: Application = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # /ping — deployment verification (registered before ConversationHandlers so
    # it is always reachable regardless of the user's conversation state)
    app.add_handler(CommandHandler("ping", cmd_ping))

    # Chat cleanup tracking: patch outgoing sends, and record incoming
    # messages/callbacks at low priority (group=-1) so they don't interfere
    # with normal handler dispatch order.
    _patch_outgoing_tracking(app)
    app.add_handler(MessageHandler(filters.ALL, _track_incoming), group=-1)
    app.add_handler(CallbackQueryHandler(_track_incoming), group=-1)

    # Register ConversationHandlers — specific wizards first (higher priority)
    app.add_handler(build_schedule_wizard())
    app.add_handler(build_queue_manager())
    app.add_handler(build_media_pool())
    app.add_handler(build_base_conversation())

    logger.info("Bot starting — polling for updates…")
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,   # ignore stale updates accumulated during downtime
    )


if __name__ == "__main__":
    main()
