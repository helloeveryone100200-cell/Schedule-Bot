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

from telegram.ext import Application, ApplicationBuilder

from config import MONGO_URI, PORT, TELEGRAM_BOT_TOKEN
from database import close_db, connect_db
from handlers.base import build_base_conversation
from handlers.queue_manager import build_media_pool, build_queue_manager
from handlers.schedule_wizard import build_schedule_wizard
from keep_alive import start_keep_alive
from scheduler import setup_scheduler

logger = logging.getLogger(__name__)

# Module-level scheduler reference so post_shutdown can stop it
_scheduler = None


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
    #    asyncio.get_event_loop() inside a running coroutine returns the
    #    current running loop, ensuring APScheduler and PTB share it.
    # asyncio.get_running_loop() is the correct modern idiom (Python 3.10+).
    # get_event_loop() inside an async coroutine raises a DeprecationWarning
    # in 3.10+ and will be removed in a future Python version.
    loop = asyncio.get_running_loop()
    _scheduler = setup_scheduler(application.bot, event_loop=loop)
    _scheduler.start()
    logger.info("APScheduler started (loop id=%d).", id(loop))


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
