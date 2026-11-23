"""Schedule, session countdown, and Discord Scheduled Events commands."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from f1_bot.bot import F1WatchPartyBot
from f1_bot.clients.jolpica import RaceWeekend

logger = logging.getLogger(__name__)


async def sync_guild_scheduled_events(
    bot: F1WatchPartyBot,
    guild: discord.Guild,
    *,
    days_ahead: int = 14,
) -> tuple[int, int]:
    """Create Discord Scheduled Events for upcoming GP sessions not yet synced."""
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days_ahead)
    schedule = await bot.jolpica.get_schedule("current")

    created = 0
    skipped = 0
    for weekend in schedule:
        for session in weekend.sessions:
            if session.start_time_utc <= now + timedelta(minutes=1):
                continue
            if session.start_time_utc > horizon:
                continue

            already_synced = await bot.db.is_event_synced(
                guild.id,
                weekend.season,
                weekend.round_num,
                session.name,
            )
            if already_synced:
                skipped += 1
                continue

            duration_hours = 2 if session.name == "Race" else 1
            end_time = session.start_time_utc + timedelta(hours=duration_hours)
            event_name = f"F1 {weekend.race_name} — {session.name}"[:100]
            location = f"{weekend.circuit_name}, {weekend.country}"[:100]
            description = (
                f"Round {weekend.round_num} · {weekend.race_name}\n"
                f"Circuit: {weekend.circuit_name} ({weekend.locality}, {weekend.country})"
            )[:1000]

            discord_event = await guild.create_scheduled_event(
                name=event_name,
                description=description,
                start_time=session.start_time_utc,
                end_time=end_time,
                privacy_level=discord.PrivacyLevel.guild_only,
                entity_type=discord.EntityType.external,
                location=location,
                reason="Synced upcoming F1 Grand Prix session",
            )
            await bot.db.mark_event_synced(
                guild.id,
                weekend.season,
                weekend.round_num,
                session.name,
                discord_event.id,
            )
            created += 1

    return (created, skipped)


def _format_next_race_embed(weekend: RaceWeekend) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏎️ Next GP: {weekend.race_name} (Round {weekend.round_num})",
        description=(
            f"**Circuit:** {weekend.circuit_name}\n"
            f"**Location:** {weekend.locality}, {weekend.country}"
        ),
        color=0xE10600,
    )
    lines: list[str] = []
    for session in weekend.sessions:
        unix = int(session.start_time_utc.timestamp())
        lines.append(f"**{session.name}:** <t:{unix}:F> (<t:{unix}:R>)")

    embed.add_field(
        name="Weekend Schedule (Your Local Time)",
        value="\n".join(lines) if lines else "Session times TBD",
        inline=False,
    )
    return embed


class ScheduleCog(commands.Cog):
    """Commands for viewing F1 calendars and syncing Discord Scheduled Events."""

    def __init__(self, bot: F1WatchPartyBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="next",
        description="Show the next Grand Prix weekend and all session times",
    )
    async def next_cmd(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            weekend = await self.bot.jolpica.get_next_race()
        except httpx.HTTPError as exc:
            await interaction.followup.send(
                f"Failed to fetch F1 schedule from Jolpica-F1: `{exc}`",
                ephemeral=True,
            )
            return

        if weekend is None:
            await interaction.followup.send(
                "No upcoming Grand Prix sessions were found for the current season."
            )
            return

        await interaction.followup.send(embed=_format_next_race_embed(weekend))

    @app_commands.command(
        name="schedule",
        description="Show the Formula 1 race calendar for a season",
    )
    @app_commands.describe(season="Season year (defaults to current season)")
    async def schedule_cmd(
        self,
        interaction: discord.Interaction,
        season: app_commands.Range[int, 1950, 2100] | None = None,
    ) -> None:
        await interaction.response.defer()
        season_arg: int | str = season if season is not None else "current"
        try:
            weekends = await self.bot.jolpica.get_schedule(season_arg)
        except httpx.HTTPError as exc:
            await interaction.followup.send(
                f"Failed to fetch F1 schedule: `{exc}`",
                ephemeral=True,
            )
            return

        if not weekends:
            await interaction.followup.send(f"No races found for season `{season_arg}`.")
            return

        now = datetime.now(timezone.utc)
        lines: list[str] = []
        for wk in weekends:
            race = wk.race_session
            if race:
                unix = int(race.start_time_utc.timestamp())
                marker = "✅" if race.start_time_utc + timedelta(hours=3) < now else "🔜"
                lines.append(
                    f"{marker} **R{wk.round_num:02d}** · {wk.race_name} — <t:{unix}:d> <t:{unix}:t>"
                )
            else:
                lines.append(f"🔜 **R{wk.round_num:02d}** · {wk.race_name}")

        display_season = weekends[0].season or season_arg
        embed = discord.Embed(
            title=f"📅 {display_season} Formula 1 Calendar",
            description="\n".join(lines)[:4000],
            color=0xE10600,
        )
        embed.set_footer(text=f"Total rounds: {len(weekends)}")
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="events-sync",
        description="Create Discord Scheduled Events for upcoming F1 sessions",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_events=True)
    @app_commands.describe(
        days_ahead="How many days ahead to sync sessions (default 14, max 60)"
    )
    async def events_sync_cmd(
        self,
        interaction: discord.Interaction,
        days_ahead: app_commands.Range[int, 1, 60] = 14,
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        try:
            created, skipped = await sync_guild_scheduled_events(
                self.bot,
                interaction.guild,
                days_ahead=days_ahead,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I need the **Manage Events** permission to create Discord Scheduled Events.",
                ephemeral=True,
            )
            return
        except (discord.HTTPException, httpx.HTTPError) as exc:
            logger.exception("Failed syncing scheduled events")
            await interaction.followup.send(
                f"Failed to sync scheduled events: `{exc}`",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Synced upcoming F1 sessions (next {days_ahead} days): "
            f"**{created}** created, **{skipped}** already synced.",
            ephemeral=True,
        )


async def setup(bot: F1WatchPartyBot) -> None:
    await bot.add_cog(ScheduleCog(bot))
