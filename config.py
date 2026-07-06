import os
import sys
import logging

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
MONGO_URI: str = os.getenv("MONGO_URI", "")
PORT: int = int(os.getenv("PORT", "8080"))

_raw_admin_ids = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: list[int] = [
    int(i.strip())
    for i in _raw_admin_ids.split(",")
    if i.strip().isdigit()
]

if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN is not set. Exiting.")
    sys.exit(1)

if not MONGO_URI:
    logger.critical("MONGO_URI is not set. Exiting.")
    sys.exit(1)

logger.info("Configuration loaded successfully.")
logger.info("Admin IDs: %s", ADMIN_IDS)
