"""Signull — Polymarket 5M Up/Down trading bot."""

import logging
import sys

import uvicorn

from src.bot import TradingBot
from src.config import BotConfig
from src.server import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = BotConfig.from_env()
    except ValueError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    if len(sys.argv) > 1 and sys.argv[1] == "server":
        app = create_app(config)
        print(f"Dashboard: http://{config.server_host}:{config.server_port}")
        uvicorn.run(app, host=config.server_host, port=config.server_port, log_level="info")
        return

    bot = TradingBot(config)
    bot.run()


if __name__ == "__main__":
    main()