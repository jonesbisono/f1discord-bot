"""CLI entrypoint for running the F1 Watch-Party Companion Bot."""

from __future__ import annotations

import logging
import sys

from pydantic import ValidationError

from f1_bot.bot import F1WatchPartyBot
from f1_bot.config import Settings


def main() -> None:
    try:
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(
            "Configuration error: ensure DISCORD_TOKEN is set in environment or .env.\n"
            f"{exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    bot = F1WatchPartyBot(settings)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
