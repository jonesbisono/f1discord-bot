"""Watch-party voice/thread lifecycle and F1 TV playback sync commands (/watchparty and /sync)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from f1_bot.bot import F1WatchPartyBot, PartyFeedState
from f1_bot.clients.openf1 import OpenF1AuthRequiredError
from f1_bot.services.spoiler_queue import (
    SpoilerDelayQueue,
    compute_current_playback_seconds,
    format_clock,
    parse_clock,
)

logger = logging.getLogger(__name__)


class WatchPartyCog(commands.Cog):
    """Commands to launch, manage, and synchronize F1 watch parties."""

    watchparty_group = app_commands.Group(
        name="watchparty",
        description="Start or stop an F1 watch party in this server",
        guild_only=True,
    )
    sync_group = app_commands.Group(
        name="sync",
        description="Synchronize your F1 TV / broadcast playback clock with the group",
        guild_only=True,
    )

    def __init__(self, bot: F1WatchPartyBot) -> None:
        self.bot = bot

    @watchparty_group.command(
        name="start",
        description="Create a watch-party voice channel, race thread, and spoiler-delayed feed",
    )
    @app_commands.describe(
        name="Custom label for this watch party",
        spoiler_delay="Override spoiler delay in seconds (0–180)",
        replay_session_key="Optional historical OpenF1 session_key for Replay Mode testing",
    )
    async def watchparty_start(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
        spoiler_delay: app_commands.Range[int, 0, 180] | None = None,
        replay_session_key: int | None = None,
    ) -> None:
        assert interaction.guild is not None
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Please run `/watchparty start` inside a standard server text channel.",
                ephemeral=True,
            )
            return

        existing = await self.bot.db.get_active_watch_party(interaction.guild.id)
        if existing is not None:
            await interaction.response.send_message(
                f"A watch party is already active in <#{existing.thread_id}>. "
                "Run `/watchparty stop` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        cfg = await self.bot.db.get_guild_settings(interaction.guild.id)
        delay = (
            spoiler_delay
            if spoiler_delay is not None
            else (cfg.spoiler_delay_seconds if cfg else 45)
        )

        is_replay = replay_session_key is not None
        session_key: int | None = replay_session_key
        session_label = name.strip() if name and name.strip() else ""
        auth_note: str | None = None

        if is_replay and session_key is not None:
            try:
                session_meta = await self.bot.openf1.get_session(session_key)
            except (OpenF1AuthRequiredError, httpx.HTTPError) as exc:
                await interaction.followup.send(
                    f"Could not load OpenF1 replay session `{session_key}`: `{exc}`",
                    ephemeral=True,
                )
                return

            if session_meta is None:
                await interaction.followup.send(
                    f"OpenF1 session `{session_key}` was not found.",
                    ephemeral=True,
                )
                return

            if not session_label:
                country = session_meta.get("country_name") or session_meta.get(
                    "location", "F1"
                )
                sess_name = session_meta.get("session_name", "Session")
                year = session_meta.get("year", "")
                session_label = f"{year} {country} {sess_name} (Replay)".strip()
        else:
            try:
                latest = await self.bot.openf1.get_latest_session()
                if latest and latest.get("session_key"):
                    session_key = int(latest["session_key"])
                    if not session_label:
                        country = latest.get("country_name") or latest.get("location", "F1")
                        sess_name = latest.get("session_name", "Session")
                        session_label = f"{country} {sess_name}".strip()
            except OpenF1AuthRequiredError:
                auth_note = (
                    "OpenF1 live session data requires sponsor credentials in `.env` "
                    "(`OPENF1_USERNAME` / `OPENF1_PASSWORD`). Voice channel and `/sync` "
                    "still work normally!"
                )
            except httpx.HTTPError as exc:
                logger.warning("Failed checking latest OpenF1 session: %s", exc)
                auth_note = "Could not reach OpenF1 right now; live feed polling is paused."

            if not session_label:
                session_label = "F1 Watch Party"

        # Create temporary voice channel if permitted
        voice_channel: discord.VoiceChannel | None = None
        category: discord.CategoryChannel | None = None
        if cfg and cfg.vc_category_id:
            found = interaction.guild.get_channel(cfg.vc_category_id)
            if isinstance(found, discord.CategoryChannel):
                category = found

        vc_name = f"🏎️ {session_label}"[:100]
        try:
            voice_channel = await interaction.guild.create_voice_channel(
                name=vc_name,
                category=category,
                reason=f"F1 Watch Party started by {interaction.user}",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Could not create watch-party voice channel: %s", exc)

        # Create starter message and thread
        vc_mention = voice_channel.mention if voice_channel else "*Not created (missing permissions)*"
        starter_msg = await interaction.channel.send(
            f"🏁 **Watch Party Started: {session_label}**\n"
            f"Voice Channel: {vc_mention} · Spoiler Delay: **{delay}s**"
        )
        thread = await starter_msg.create_thread(
            name=f"🏁 {session_label}"[:100],
            auto_archive_duration=1440,
            reason="F1 Watch Party discussion thread",
        )

        now = datetime.now(timezone.utc)
        party = await self.bot.db.create_watch_party(
            guild_id=interaction.guild.id,
            text_channel_id=interaction.channel.id,
            thread_id=thread.id,
            voice_channel_id=voice_channel.id if voice_channel else None,
            session_key=session_key,
            session_label=session_label,
            spoiler_delay_seconds=delay,
            is_replay=is_replay,
            created_at=now,
        )

        self.bot.party_feeds[party.id] = PartyFeedState(
            party_id=party.id,
            guild_id=interaction.guild.id,
            thread_id=thread.id,
            session_key=session_key,
            queue=SpoilerDelayQueue(delay_seconds=delay),
            is_replay=is_replay,
            last_poll_iso=None if is_replay else now.isoformat(),
            paused=session_key is None,
        )

        welcome = discord.Embed(
            title=f"🏎️ {session_label}",
            description=(
                "Everyone watch on your own **F1 TV** / broadcast stream and sync together:\n"
                "• `/sync start` — Run a live countdown so everyone presses play at once\n"
                "• `/sync set <MM:SS or HH:MM:SS>` — Jump the shared clock to a race timestamp\n"
                "• `/sync now` — Privately view the exact playback timestamp if you join late\n"
                "• `/watchparty stop` — End the watch party and clean up the voice channel"
            ),
            color=0xE10600,
        )
        welcome.add_field(name="Voice Channel", value=vc_mention, inline=True)
        welcome.add_field(name="Spoiler Delay", value=f"{delay} seconds", inline=True)
        welcome.add_field(
            name="Feed Mode",
            value=f"Replay (`{session_key}`)" if is_replay else ("Live OpenF1" if session_key else "Sync Only"),
            inline=True,
        )
        if auth_note:
            welcome.add_field(name="OpenF1 Notice", value=auth_note, inline=False)

        await thread.send(embed=welcome)
        await interaction.followup.send(
            f"Watch party ready in {thread.mention}! Join {vc_mention}.",
            ephemeral=True,
        )

    @watchparty_group.command(
        name="stop",
        description="End the active watch party, archive its thread, and delete the temporary VC",
    )
    async def watchparty_stop(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        party = await self.bot.db.get_active_watch_party(interaction.guild.id)
        if party is None:
            await interaction.response.send_message(
                "There is no active watch party in this server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        self.bot.party_feeds.pop(party.id, None)
        await self.bot.db.close_watch_party(party.id)

        if party.voice_channel_id:
            vc = interaction.guild.get_channel(party.voice_channel_id)
            if isinstance(vc, discord.VoiceChannel):
                try:
                    await vc.delete(reason=f"Watch party stopped by {interaction.user}")
                except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                    logger.warning("Could not delete VC %s: %s", party.voice_channel_id, exc)

        thread = interaction.guild.get_thread(party.thread_id)
        if thread is not None:
            try:
                await thread.send(
                    f"🏁 **Watch party ended** by {interaction.user.mention}. Thanks for watching!"
                )
                await thread.edit(archived=True)
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("Could not archive thread %s: %s", party.thread_id, exc)

        await interaction.followup.send(
            f"Stopped watch party **{party.session_label}**.",
            ephemeral=True,
        )

    @sync_group.command(
        name="start",
        description="Run a live countdown and start the shared playback sync clock at 00:00:00",
    )
    @app_commands.describe(countdown="Countdown length in seconds (3–15, default 5)")
    async def sync_start(
        self,
        interaction: discord.Interaction,
        countdown: app_commands.Range[int, 3, 15] = 5,
    ) -> None:
        assert interaction.guild is not None
        party = await self.bot.db.get_active_watch_party(interaction.guild.id)
        if party is None:
            await interaction.response.send_message(
                "No active watch party found. Start one first with `/watchparty start`.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"⏳ **Playback sync starting in {countdown}...** Get ready on pause!"
        )
        for remaining in range(countdown - 1, 0, -1):
            await asyncio.sleep(1)
            await interaction.edit_original_response(
                content=f"⏳ **Playback sync starting in {remaining}...**"
            )

        await asyncio.sleep(1)
        now = datetime.now(timezone.utc)
        await self.bot.db.update_watch_party_sync(party.id, now, 0)

        state = self.bot.party_feeds.get(party.id)
        if state and state.is_replay and state.replay_cursor is not None:
            state.replay_cursor.party_start = now
            state.replay_cursor.offset_seconds = 0
            state.last_poll_iso = state.replay_cursor.virtual_now(now).isoformat()

        await interaction.edit_original_response(
            content=(
                "▶️ **GO! Press PLAY now!** (`00:00:00`)\n"
                "Late joiners can run `/sync now` anytime to see the exact race timestamp."
            )
        )

    @sync_group.command(
        name="set",
        description="Set the shared F1 TV playback timestamp (e.g. 00:42:15 or 42:15)",
    )
    @app_commands.describe(timestamp="Current stream timestamp in MM:SS or HH:MM:SS")
    async def sync_set(
        self,
        interaction: discord.Interaction,
        timestamp: str,
    ) -> None:
        assert interaction.guild is not None
        party = await self.bot.db.get_active_watch_party(interaction.guild.id)
        if party is None:
            await interaction.response.send_message(
                "No active watch party found. Start one first with `/watchparty start`.",
                ephemeral=True,
            )
            return

        try:
            offset_seconds = parse_clock(timestamp)
        except ValueError as exc:
            await interaction.response.send_message(
                f"Invalid timestamp `{timestamp}`: {exc}",
                ephemeral=True,
            )
            return

        now = datetime.now(timezone.utc)
        await self.bot.db.update_watch_party_sync(party.id, now, offset_seconds)

        state = self.bot.party_feeds.get(party.id)
        if state and state.is_replay and state.replay_cursor is not None:
            state.replay_cursor.party_start = now
            state.replay_cursor.offset_seconds = offset_seconds
            state.last_poll_iso = state.replay_cursor.virtual_now(now).isoformat()

        formatted = format_clock(offset_seconds)
        await interaction.response.send_message(
            f"⏱️ Shared playback clock updated to **`{formatted}`** by {interaction.user.mention}."
        )

    @sync_group.command(
        name="now",
        description="Privately check the current synced playback timestamp without spoilers",
    )
    async def sync_now(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        party = await self.bot.db.get_active_watch_party(interaction.guild.id)
        if party is None:
            await interaction.response.send_message(
                "There is no active watch party in this server.",
                ephemeral=True,
            )
            return

        if party.sync_started_at is None:
            await interaction.response.send_message(
                "The sync clock hasn't been started yet. Ask someone to run `/sync start` or `/sync set`.",
                ephemeral=True,
            )
            return

        current_seconds = compute_current_playback_seconds(
            party.sync_started_at,
            party.sync_offset_seconds,
        )
        await interaction.response.send_message(
            f"⏱️ Scrub your F1 TV player to **`{format_clock(current_seconds)}`** "
            f"(Party: **{party.session_label}**).",
            ephemeral=True,
        )


async def setup(bot: F1WatchPartyBot) -> None:
    await bot.add_cog(WatchPartyCog(bot))
