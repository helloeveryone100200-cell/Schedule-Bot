import threading
import logging
from flask import Flask

logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_server(port: int = 8080) -> None:
    logger.info("Starting keep-alive server on port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def start_keep_alive(port: int = 8080) -> threading.Thread:
    thread = threading.Thread(target=run_server, args=(port,), daemon=True)
    thread.start()
    logger.info("Keep-alive server thread started.")
    return thread
