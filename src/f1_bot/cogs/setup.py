"""Server configuration commands (/setup and /config)."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from f1_bot.bot import F1WatchPartyBot
from f1_bot.db.database import GuildSettings


def _build_config_embed(guild: discord.Guild, cfg: GuildSettings) -> discord.Embed:
    announce = (
        f"<#{cfg.announce_channel_id}>" if cfg.announce_channel_id else "Not configured"
    )
    ping_role = f"<@&{cfg.ping_role_id}>" if cfg.ping_role_id else "None"
    vc_category = f"<#{cfg.vc_category_id}>" if cfg.vc_category_id else "Server root (no category)"

    embed = discord.Embed(
        title=f"⚙️ F1 Watch-Party Config — {guild.name}",
        color=0xE10600,
    )
    embed.add_field(name="Announcement Channel", value=announce, inline=True)
    embed.add_field(name="Ping Role", value=ping_role, inline=True)
    embed.add_field(name="VC Category", value=vc_category, inline=True)
    embed.add_field(
        name="Spoiler Delay", value=f"{cfg.spoiler_delay_seconds} seconds", inline=True
    )
    embed.add_field(
        name="Reminder Lead Time", value=f"{cfg.reminder_minutes} minutes", inline=True
    )
    embed.add_field(
        name="Empty VC Cleanup", value=f"{cfg.vc_cleanup_minutes} minutes", inline=True
    )
    embed.add_field(
        name="Auto-Create Scheduled Events",
        value="Enabled" if cfg.auto_create_events else "Disabled",
        inline=False,
    )
    return embed


class SetupCog(commands.Cog):
    """Per-server watch-party configuration commands."""

    def __init__(self, bot: F1WatchPartyBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="setup",
        description="Configure F1 watch-party settings for this server",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        announce_channel="Channel where session reminders and notices are posted",
        ping_role="Role mentioned before F1 sessions start",
        vc_category="Category where temporary watch-party voice channels are created",
        spoiler_delay="Delay in seconds for live race control/pit alerts (0–180)",
        reminder_minutes="Minutes before a session starts to post a reminder (5–120)",
        vc_cleanup_minutes="Minutes after a watch-party VC becomes empty before deletion (1–60)",
        auto_create_events="Automatically create Discord Scheduled Events for upcoming sessions",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        announce_channel: discord.TextChannel | None = None,
        ping_role: discord.Role | None = None,
        vc_category: discord.CategoryChannel | None = None,
        spoiler_delay: app_commands.Range[int, 0, 180] | None = None,
        reminder_minutes: app_commands.Range[int, 5, 120] | None = None,
        vc_cleanup_minutes: app_commands.Range[int, 1, 60] | None = None,
        auto_create_events: bool | None = None,
    ) -> None:
        assert interaction.guild is not None
        updated = await self.bot.db.upsert_guild_settings(
            interaction.guild.id,
            announce_channel_id=announce_channel.id if announce_channel else None,
            ping_role_id=ping_role.id if ping_role else None,
            vc_category_id=vc_category.id if vc_category else None,
            spoiler_delay_seconds=spoiler_delay,
            reminder_minutes=reminder_minutes,
            vc_cleanup_minutes=vc_cleanup_minutes,
            auto_create_events=auto_create_events,
        )
        embed = _build_config_embed(interaction.guild, updated)
        embed.set_footer(text="Configuration saved.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="config",
        description="View current F1 watch-party settings for this server",
    )
    @app_commands.guild_only()
    async def config_cmd(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        cfg = await self.bot.db.get_guild_settings(interaction.guild.id)
        if cfg is None:
            cfg = GuildSettings(guild_id=interaction.guild.id)
        embed = _build_config_embed(interaction.guild, cfg)
        if cfg.announce_channel_id is None:
            embed.set_footer(
                text="Tip: An admin can run /setup announce_channel:#channel to enable reminders."
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: F1WatchPartyBot) -> None:
    await bot.add_cog(SetupCog(bot))
