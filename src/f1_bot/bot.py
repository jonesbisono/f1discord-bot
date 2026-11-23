"""Core Discord bot class and shared runtime state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import discord
from discord.ext import commands

from f1_bot.clients.jolpica import JolpicaClient
from f1_bot.clients.openf1 import OpenF1Client
from f1_bot.config import Settings
from f1_bot.db.database import Database
from f1_bot.services.spoiler_queue import RaceFeedEvent, ReplayCursor, SpoilerDelayQueue

logger = logging.getLogger(__name__)

COGS = (
    "f1_bot.cogs.setup",
    "f1_bot.cogs.schedule",
    "f1_bot.cogs.watchparty",
    "f1_bot.cogs.stats",
    "f1_bot.tasks.scheduler",
)


@dataclass
class PartyFeedState:
    """In-memory live/replay feed state for an active watch party."""

    party_id: int
    guild_id: int
    thread_id: int
    session_key: int | None
    queue: SpoilerDelayQueue
    is_replay: bool
    replay_cursor: ReplayCursor | None = None
    replay_events: list[RaceFeedEvent] = field(default_factory=list)
    replay_loaded: bool = False
    last_poll_iso: str | None = None
    last_rainfall: int | None = None
    drivers: dict[int, str] = field(default_factory=dict)
    auth_notified: bool = False
    paused: bool = False


class F1WatchPartyBot(commands.Bot):
    """Multi-server Formula 1 Watch-Party Companion Bot."""

    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
        )
        self.settings = settings
        self.db = Database(settings.database_path)
        self.jolpica = JolpicaClient()
        self.openf1 = OpenF1Client(
            username=settings.openf1_username,
            password=settings.openf1_password,
        )
        self.party_feeds: dict[int, PartyFeedState] = {}

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self._restore_active_parties()

        for extension in COGS:
            await self.load_extension(extension)
            logger.info("Loaded extension: %s", extension)

        synced = await self.tree.sync()
        logger.info("Synced %d application command(s)", len(synced))

    async def _restore_active_parties(self) -> None:
        active_parties = await self.db.list_active_watch_parties()
        now_iso = datetime.now(timezone.utc).isoformat()
        for party in active_parties:
            self.party_feeds[party.id] = PartyFeedState(
                party_id=party.id,
                guild_id=party.guild_id,
                thread_id=party.thread_id,
                session_key=party.session_key,
                queue=SpoilerDelayQueue(delay_seconds=party.spoiler_delay_seconds),
                is_replay=party.is_replay,
                last_poll_iso=None if party.is_replay else now_iso,
            )
        if active_parties:
            logger.info("Restored %d active watch party feed(s) from database", len(active_parties))

    async def on_ready(self) -> None:
        logger.info(
            "Logged in as %s (ID: %s) across %d guild(s)",
            self.user,
            self.user.id if self.user else "?",
            len(self.guilds),
        )

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            await self.jolpica.close()
            await self.openf1.close()
            await self.db.close()
