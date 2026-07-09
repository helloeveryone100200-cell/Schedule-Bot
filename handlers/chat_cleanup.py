"""
handlers/chat_cleanup.py — Track every message sent/received inside a chat
during a multi-step wizard, then wipe them all away once the wizard finishes
(or is cancelled), leaving only the final "success"/"cancelled" message.

How it works
------------
- `main.py` patches `Bot.send_message` so every OUTGOING message the bot
  sends is recorded here automatically (no need to touch every call site in
  schedule_wizard.py / queue_manager.py / base.py).
- `main.py` also registers a low-priority (group=-1) update handler that
  records every INCOMING message/callback so the user's own typed replies
  (dates, numbers, etc.) get swept up too.
- Each wizard entry point calls `reset(context, chat_id)` to start a fresh
  tracking window, and each wizard exit point (confirm or cancel) calls
  `cleanup(context, chat_id, keep_message_id=...)` to delete everything
  except the final message.

Telegram limits: bots can only delete their own messages, or other users'
messages sent within the last 48 hours in a private chat. Failures are
swallowed — a message that can't be deleted just stays put.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_BOT_DATA_KEY = "chat_cleanup"


def _store(bot_data: dict) -> dict:
    return bot_data.setdefault(_BOT_DATA_KEY, {})


def track(bot_data: dict, chat_id: int, message_id: int) -> None:
    """Record a message id belonging to `chat_id` for later cleanup."""
    ids = _store(bot_data).setdefault(chat_id, [])
    if message_id not in ids:
        ids.append(message_id)


def reset(bot_data: dict, chat_id: int) -> None:
    """Start a fresh tracking window for `chat_id` (call at wizard start)."""
    _store(bot_data)[chat_id] = []


async def cleanup(context, chat_id: int, keep_message_id: int | None = None) -> None:
    """Delete every tracked message in `chat_id` except `keep_message_id`."""
    store = _store(context.application.bot_data)
    ids = store.get(chat_id, [])
    for mid in ids:
        if mid == keep_message_id:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception as exc:
            # Message too old (>48h), already deleted, or not deletable by
            # the bot — safe to ignore, it just stays visible.
            logger.debug("chat_cleanup: could not delete %s in %s: %s", mid, chat_id, exc)
    store[chat_id] = []
