"""Entry point — run ``python main.py`` to start the Discord bot."""

from __future__ import annotations

import logging
import sys

from bot.bot import build_bot
from bot.config import load_settings
from bot.logger import configure_logging

log = logging.getLogger(__name__)


def main() -> int:
    try:
        settings = load_settings()
    except RuntimeError as exc:
        # Config errors happen before logging is configured, so use stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings.log_level)

    bot = build_bot(settings)
    try:
        # ``Bot.run`` manages the event loop, reconnection, and graceful
        # shutdown on SIGINT/SIGTERM for us.
        bot.run(settings.discord_token, log_handler=None)
    except KeyboardInterrupt:
        log.info("Received keyboard interrupt — shutting down.")
    except Exception:
        log.exception("Fatal error while running the bot")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
