"""Championship standings and race results commands (/standings and /results)."""

from __future__ import annotations

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from f1_bot.bot import F1WatchPartyBot


def _format_points(points: float) -> str:
    return str(int(points)) if points.is_integer() else f"{points:.1f}"


def _format_grid_delta(grid: int, position: int) -> str:
    if grid <= 0:
        return "PL"
    delta = grid - position
    if delta > 0:
        return f"▲+{delta}"
    if delta < 0:
        return f"▼{delta}"
    return "—"


class StatsCog(commands.Cog):
    """Post-race classification and championship standings commands."""

    standings_group = app_commands.Group(
        name="standings",
        description="View Formula 1 Drivers and Constructors standings",
    )

    def __init__(self, bot: F1WatchPartyBot) -> None:
        self.bot = bot

    @standings_group.command(
        name="drivers",
        description="Show Formula 1 Drivers Championship standings",
    )
    @app_commands.describe(season="Season year (defaults to current season)")
    async def standings_drivers(
        self,
        interaction: discord.Interaction,
        season: app_commands.Range[int, 1950, 2100] | None = None,
    ) -> None:
        await interaction.response.defer()
        season_arg: int | str = season if season is not None else "current"
        try:
            resolved_season, entries = await self.bot.jolpica.get_driver_standings(
                season_arg
            )
        except httpx.HTTPError as exc:
            await interaction.followup.send(
                f"Failed to fetch driver standings: `{exc}`",
                ephemeral=True,
            )
            return

        if not entries:
            await interaction.followup.send(
                f"No driver standings found for season `{season_arg}`."
            )
            return

        lines: list[str] = []
        for item in entries[:20]:
            wins_text = f" · {item.wins}W" if item.wins else ""
            lines.append(
                f"`{item.position_text:>2}.` **{item.driver_code}** · {item.driver_name} "
                f"*({item.constructor_name})* — **{_format_points(item.points)} pts**{wins_text}"
            )

        embed = discord.Embed(
            title=f"🏆 {resolved_season or season_arg} Drivers Championship",
            description="\n".join(lines)[:4000],
            color=0xE10600,
        )
        if len(entries) > 20:
            embed.set_footer(text=f"Showing top 20 of {len(entries)} drivers")
        await interaction.followup.send(embed=embed)

    @standings_group.command(
        name="constructors",
        description="Show Formula 1 Constructors Championship standings",
    )
    @app_commands.describe(season="Season year (defaults to current season)")
    async def standings_constructors(
        self,
        interaction: discord.Interaction,
        season: app_commands.Range[int, 1950, 2100] | None = None,
    ) -> None:
        await interaction.response.defer()
        season_arg: int | str = season if season is not None else "current"
        try:
            resolved_season, entries = await self.bot.jolpica.get_constructor_standings(
                season_arg
            )
        except httpx.HTTPError as exc:
            await interaction.followup.send(
                f"Failed to fetch constructor standings: `{exc}`",
                ephemeral=True,
            )
            return

        if not entries:
            await interaction.followup.send(
                f"No constructor standings found for season `{season_arg}`."
            )
            return

        lines: list[str] = []
        for item in entries:
            wins_text = f" · {item.wins}W" if item.wins else ""
            lines.append(
                f"`{item.position_text:>2}.` **{item.constructor_name}** — "
                f"**{_format_points(item.points)} pts**{wins_text}"
            )

        embed = discord.Embed(
            title=f"🏭 {resolved_season or season_arg} Constructors Championship",
            description="\n".join(lines)[:4000],
            color=0xE10600,
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="results",
        description="Show Grand Prix race results (defaults to latest race)",
    )
    @app_commands.describe(
        season="Season year (defaults to current season)",
        round="Round number (defaults to latest completed race)",
    )
    async def results_cmd(
        self,
        interaction: discord.Interaction,
        season: app_commands.Range[int, 1950, 2100] | None = None,
        round: app_commands.Range[int, 1, 30] | None = None,
    ) -> None:
        await interaction.response.defer()
        season_arg: int | str = season if season is not None else "current"
        round_arg: int | str = round if round is not None else "last"
        try:
            race = await self.bot.jolpica.get_race_results(season_arg, round_arg)
        except httpx.HTTPError as exc:
            await interaction.followup.send(
                f"Failed to fetch race results: `{exc}`",
                ephemeral=True,
            )
            return

        if race is None or not race.entries:
            await interaction.followup.send(
                f"No race results found for season `{season_arg}`, round `{round_arg}`."
            )
            return

        podium_medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines: list[str] = []
        for entry in race.entries[:20]:
            medal = podium_medals.get(entry.position, f"`{entry.position_text:>2}.`")
            delta = _format_grid_delta(entry.grid, entry.position)
            outcome = entry.time_text or entry.status
            fl_badge = " 🟣FL" if entry.fastest_lap_rank == 1 else ""
            pts_text = f" · **{_format_points(entry.points)}**" if entry.points > 0 else ""
            lines.append(
                f"{medal} **{entry.driver_code}** ({entry.constructor_name}) · "
                f"`{delta}` · {outcome}{pts_text}{fl_badge}"
            )

        embed = discord.Embed(
            title=f"🏁 {race.season} R{race.round_num:02d} — {race.race_name}",
            description="\n".join(lines)[:4000],
            color=0xE10600,
        )
        embed.set_footer(text=f"{race.circuit_name} · {race.date}")
        await interaction.followup.send(embed=embed)


async def setup(bot: F1WatchPartyBot) -> None:
    await bot.add_cog(StatsCog(bot))
