"""Background loops for session reminders, scheduled events, spoiler-delayed feeds, and VC cleanup."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
import httpx
from discord.ext import commands, tasks

from f1_bot.bot import F1WatchPartyBot, PartyFeedState
from f1_bot.clients.openf1 import OpenF1AuthRequiredError, parse_openf1_timestamp
from f1_bot.cogs.schedule import sync_guild_scheduled_events
from f1_bot.services.spoiler_queue import (
    RaceFeedEvent,
    ReplayCursor,
    format_pit_event,
    format_race_control_event,
    format_weather_event,
)

logger = logging.getLogger(__name__)


class SchedulerCog(commands.Cog):
    """Runs background automation tasks across all configured servers."""

    def __init__(self, bot: F1WatchPartyBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.reminder_loop.start()
        self.event_sync_loop.start()
        self.live_feed_loop.start()
        self.vc_cleanup_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()
        self.event_sync_loop.cancel()
        self.live_feed_loop.cancel()
        self.vc_cleanup_loop.cancel()

    @tasks.loop(seconds=60)
    async def reminder_loop(self) -> None:
        try:
            configured = await self.bot.db.list_configured_guilds()
            targets = [cfg for cfg in configured if cfg.announce_channel_id is not None]
            if not targets:
                return

            weekend = await self.bot.jolpica.get_next_race()
            if weekend is None:
                return

            now = datetime.now(timezone.utc)
            for cfg in targets:
                guild = self.bot.get_guild(cfg.guild_id)
                if guild is None or cfg.announce_channel_id is None:
                    continue
                channel = guild.get_channel(cfg.announce_channel_id)
                if not isinstance(channel, discord.TextChannel):
                    continue

                for session in weekend.sessions:
                    seconds_until = (session.start_time_utc - now).total_seconds()
                    if not (0 <= seconds_until <= cfg.reminder_minutes * 60):
                        continue

                    already_sent = await self.bot.db.is_reminder_sent(
                        guild.id,
                        weekend.season,
                        weekend.round_num,
                        session.name,
                    )
                    if already_sent:
                        continue

                    unix = int(session.start_time_utc.timestamp())
                    role_ping = f"<@&{cfg.ping_role_id}> " if cfg.ping_role_id else ""
                    embed = discord.Embed(
                        title=f"🔔 {weekend.race_name} — {session.name} starting soon!",
                        description=(
                            f"Starts <t:{unix}:R> (<t:{unix}:t>)\n"
                            f"**Circuit:** {weekend.circuit_name}\n\n"
                            "Ready to watch together? Run `/watchparty start`!"
                        ),
                        color=0xE10600,
                    )
                    try:
                        await channel.send(
                            content=role_ping or None,
                            embed=embed,
                        )
                        await self.bot.db.mark_reminder_sent(
                            guild.id,
                            weekend.season,
                            weekend.round_num,
                            session.name,
                            sent_at=now,
                        )
                    except (discord.Forbidden, discord.HTTPException) as exc:
                        logger.warning(
                            "Failed sending reminder in guild %s: %s", guild.id, exc
                        )
        except Exception:
            logger.exception("Error in reminder_loop")

    @reminder_loop.before_loop
    async def _before_reminder(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(hours=12)
    async def event_sync_loop(self) -> None:
        try:
            configured = await self.bot.db.list_configured_guilds()
            for cfg in configured:
                if not cfg.auto_create_events:
                    continue
                guild = self.bot.get_guild(cfg.guild_id)
                if guild is None:
                    continue
                try:
                    created, _ = await sync_guild_scheduled_events(self.bot, guild)
                    if created:
                        logger.info(
                            "Created %d scheduled event(s) for guild %s", created, guild.id
                        )
                except (discord.Forbidden, discord.HTTPException, httpx.HTTPError) as exc:
                    logger.warning(
                        "Failed syncing scheduled events for guild %s: %s", guild.id, exc
                    )
        except Exception:
            logger.exception("Error in event_sync_loop")

    @event_sync_loop.before_loop
    async def _before_event_sync(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=10)
    async def live_feed_loop(self) -> None:
        try:
            now = datetime.now(timezone.utc)
            for party_id, state in list(self.bot.party_feeds.items()):
                if state.is_replay:
                    await self._tick_replay_feed(state, now)
                elif not state.paused and state.session_key is not None:
                    await self._tick_live_feed(state, now)

                ready_events = state.queue.pop_ready(now)
                if ready_events:
                    await self._deliver_events(state, ready_events)
        except Exception:
            logger.exception("Error in live_feed_loop")

    @live_feed_loop.before_loop
    async def _before_live_feed(self) -> None:
        await self.bot.wait_until_ready()

    async def _load_drivers(self, state: PartyFeedState) -> None:
        if state.drivers or state.session_key is None:
            return
        raw_drivers = await self.bot.openf1.get_drivers(state.session_key)
        for drv in raw_drivers:
            num = drv.get("driver_number")
            acronym = drv.get("name_acronym") or drv.get("last_name")
            if num is not None and acronym:
                state.drivers[int(num)] = str(acronym)

    async def _tick_live_feed(self, state: PartyFeedState, now: datetime) -> None:
        assert state.session_key is not None
        try:
            await self._load_drivers(state)
            since = state.last_poll_iso
            rc_rows = await self.bot.openf1.get_race_control(state.session_key, since_iso=since)
            weather_rows = await self.bot.openf1.get_weather(state.session_key, since_iso=since)
            pit_rows = await self.bot.openf1.get_pit_stops(state.session_key, since_iso=since)
        except OpenF1AuthRequiredError:
            if not state.auth_notified:
                state.auth_notified = True
                state.paused = True
                await self._send_thread_notice(
                    state,
                    "🔒 Live OpenF1 polling requires sponsor credentials in `.env` "
                    "(`OPENF1_USERNAME` / `OPENF1_PASSWORD`). Live feed paused for this session; "
                    "voice chat and `/sync` continue normally.",
                )
            return
        except httpx.HTTPError as exc:
            logger.warning("OpenF1 poll failed for party %s: %s", state.party_id, exc)
            return

        state.last_poll_iso = now.isoformat()
        self._enqueue_raw_rows(state, rc_rows, weather_rows, pit_rows, ingested_at=now)

    async def _tick_replay_feed(self, state: PartyFeedState, now: datetime) -> None:
        if state.session_key is None or state.paused:
            return

        if not state.replay_loaded:
            try:
                session_meta = await self.bot.openf1.get_session(state.session_key)
                await self._load_drivers(state)
                rc_rows = await self.bot.openf1.get_race_control(state.session_key)
                weather_rows = await self.bot.openf1.get_weather(state.session_key)
                pit_rows = await self.bot.openf1.get_pit_stops(state.session_key)
            except (OpenF1AuthRequiredError, httpx.HTTPError) as exc:
                logger.warning("Failed loading replay session %s: %s", state.session_key, exc)
                state.paused = True
                await self._send_thread_notice(
                    state,
                    f"⚠️ Could not load replay feed for session `{state.session_key}`: `{exc}`",
                )
                return

            events: list[RaceFeedEvent] = []
            for row in rc_rows:
                ev = format_race_control_event(row)
                if ev is not None:
                    events.append(ev)
            prev_rain: int | None = None
            for row in weather_rows:
                ev, prev_rain = format_weather_event(row, prev_rainfall=prev_rain)
                if ev is not None:
                    events.append(ev)
            for row in pit_rows:
                ev = format_pit_event(row, drivers=state.drivers)
                if ev is not None:
                    events.append(ev)

            events.sort(key=lambda e: e.event_time)
            state.replay_events = events

            party = await self.bot.db.get_watch_party(state.party_id)
            party_start = (
                party.sync_started_at
                if party and party.sync_started_at
                else (party.created_at if party else now)
            )
            offset_sec = party.sync_offset_seconds if party else 0

            session_start: datetime | None = None
            if session_meta and session_meta.get("date_start"):
                session_start = parse_openf1_timestamp(str(session_meta["date_start"]))
            elif events:
                session_start = events[0].event_time
            else:
                session_start = now

            state.replay_cursor = ReplayCursor(
                session_start=session_start,
                party_start=party_start,
                offset_seconds=offset_sec,
            )
            state.last_poll_iso = state.replay_cursor.virtual_now(now).isoformat()
            state.replay_loaded = True
            logger.info(
                "Loaded %d replay event(s) for party %s (session %s)",
                len(events),
                state.party_id,
                state.session_key,
            )

        if state.replay_cursor is None:
            return

        virtual_now = state.replay_cursor.virtual_now(now)
        last_virtual = (
            parse_openf1_timestamp(state.last_poll_iso)
            if state.last_poll_iso
            else state.replay_cursor.session_start
        )
        for ev in state.replay_events:
            if last_virtual <= ev.event_time <= virtual_now:
                state.queue.enqueue(ev, ingested_at=now)
        state.last_poll_iso = virtual_now.isoformat()

    def _enqueue_raw_rows(
        self,
        state: PartyFeedState,
        rc_rows: list[dict],
        weather_rows: list[dict],
        pit_rows: list[dict],
        *,
        ingested_at: datetime,
    ) -> None:
        for row in rc_rows:
            ev = format_race_control_event(row)
            if ev is not None:
                state.queue.enqueue(ev, ingested_at=ingested_at)

        for row in weather_rows:
            ev, state.last_rainfall = format_weather_event(
                row, prev_rainfall=state.last_rainfall
            )
            if ev is not None:
                state.queue.enqueue(ev, ingested_at=ingested_at)

        for row in pit_rows:
            ev = format_pit_event(row, drivers=state.drivers)
            if ev is not None:
                state.queue.enqueue(ev, ingested_at=ingested_at)

    async def _deliver_events(
        self, state: PartyFeedState, events: list[RaceFeedEvent]
    ) -> None:
        guild = self.bot.get_guild(state.guild_id)
        if guild is None:
            return
        thread = guild.get_thread(state.thread_id)
        if thread is None:
            try:
                fetched = await guild.fetch_channel(state.thread_id)
                if isinstance(fetched, discord.Thread):
                    thread = fetched
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        if thread is None:
            return

        for ev in events:
            embed = discord.Embed(
                title=ev.title,
                description=ev.description,
                color=ev.color,
                timestamp=ev.event_time,
            )
            embed.set_footer(
                text=f"Delayed {state.queue.delay_seconds}s to prevent spoilers"
            )
            try:
                await thread.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning(
                    "Failed posting feed event to thread %s: %s", state.thread_id, exc
                )
                break

    async def _send_thread_notice(self, state: PartyFeedState, message: str) -> None:
        guild = self.bot.get_guild(state.guild_id)
        if guild is None:
            return
        thread = guild.get_thread(state.thread_id)
        if thread is None:
            return
        try:
            await thread.send(message)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @tasks.loop(seconds=60)
    async def vc_cleanup_loop(self) -> None:
        try:
            parties = await self.bot.db.list_active_watch_parties()
            now = datetime.now(timezone.utc)
            for party in parties:
                if party.voice_channel_id is None:
                    continue

                guild = self.bot.get_guild(party.guild_id)
                if guild is None:
                    continue

                vc = guild.get_channel(party.voice_channel_id)
                if not isinstance(vc, discord.VoiceChannel):
                    # VC was deleted externally; close out the watch party cleanly.
                    self.bot.party_feeds.pop(party.id, None)
                    await self.bot.db.close_watch_party(party.id)
                    continue

                human_members = [member for member in vc.members if not member.bot]
                if human_members:
                    if party.empty_since is not None:
                        await self.bot.db.update_watch_party_empty_since(party.id, None)
                    continue

                if party.empty_since is None:
                    await self.bot.db.update_watch_party_empty_since(party.id, now)
                    continue

                cfg = await self.bot.db.get_guild_settings(guild.id)
                cleanup_minutes = cfg.vc_cleanup_minutes if cfg else 15
                if (now - party.empty_since) >= timedelta(minutes=cleanup_minutes):
                    self.bot.party_feeds.pop(party.id, None)
                    await self.bot.db.close_watch_party(party.id)
                    try:
                        await vc.delete(
                            reason=f"Watch-party VC empty for {cleanup_minutes} minutes"
                        )
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                        logger.warning("Failed deleting empty VC %s: %s", vc.id, exc)

                    thread = guild.get_thread(party.thread_id)
                    if thread is not None:
                        try:
                            await thread.send(
                                f"🧹 Voice channel was empty for {cleanup_minutes} minutes — "
                                "watch party automatically ended and cleaned up."
                            )
                            await thread.edit(archived=True)
                        except (discord.Forbidden, discord.HTTPException):
                            pass
        except Exception:
            logger.exception("Error in vc_cleanup_loop")

    @vc_cleanup_loop.before_loop
    async def _before_vc_cleanup(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: F1WatchPartyBot) -> None:
    await bot.add_cog(SchedulerCog(bot))
