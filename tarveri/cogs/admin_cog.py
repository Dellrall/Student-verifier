"""
Admin Commands & Audit Tools for TARVeri.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from tarveri.config import FACULTY_ROLE_NAMES, FACULTY_ROLES
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import VerificationService


def is_admin_or_has_role(interaction: discord.Interaction, admin_role_name: str) -> bool:
    """Checks if invoking user has Administrator permission or the configured Admin role."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    return any(r.name == admin_role_name for r in interaction.user.roles)


class AdminCog(commands.Cog, name="Admin"):
    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        service: VerificationService,
        rate_limiter: RateLimiter,
        admin_role_name: str,
    ):
        self.bot = bot
        self.db = db
        self.service = service
        self.rate_limiter = rate_limiter
        self.admin_role_name = admin_role_name

    def _check_admin(self, interaction: discord.Interaction) -> bool:
        return is_admin_or_has_role(interaction, self.admin_role_name)

    @app_commands.command(name="stats", description="View student verification statistics.")
    async def stats(self, interaction: discord.Interaction) -> None:
        """Displays total verifications, faculty breakdown, and recent activity."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        total = await self.db.total_verified()
        faculty_counts = await self.db.counts_by_faculty()
        last_24h = await self.db.verified_in_last(24)
        last_7d = await self.db.verified_in_last(24 * 7)

        embed = discord.Embed(
            title="📊 TARVeri — Verification Statistics",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Total Verified", value=f"**{total}** students", inline=True)
        embed.add_field(name="Past 24 Hours", value=f"**{last_24h}** new", inline=True)
        embed.add_field(name="Past 7 Days", value=f"**{last_7d}** new", inline=True)

        if faculty_counts:
            breakdown_lines = []
            for f_code, count in faculty_counts:
                faculty_name = FACULTY_ROLES.get(f_code, f"Code {f_code}")
                percentage = (count / total * 100) if total > 0 else 0
                breakdown_lines.append(f"• **{faculty_name}** (`{f_code}`): {count} ({percentage:.1f}%)")
            embed.add_field(name="Faculty Breakdown", value="\n".join(breakdown_lines), inline=False)
        else:
            embed.add_field(name="Faculty Breakdown", value="No verifications recorded yet.", inline=False)

        embed.set_footer(text=f"TARVeri Bot • Active in {len(self.bot.guilds)} servers")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="unverify", description="Unlink a member's student ID and revoke faculty roles.")
    @app_commands.describe(user="The Discord user to unverify", reason="Optional reason for unlinking")
    async def unverify(
        self, interaction: discord.Interaction, user: discord.User, reason: str = "Admin unverified"
    ) -> None:
        """Unbinds a Discord user and strips faculty roles across mutual guilds."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        existing = await self.db.get_verification_by_user(user.id)
        if not existing:
            await interaction.followup.send(f"⚠️ User {user.mention} is not verified in TARVeri.", ephemeral=True)
            return

        deleted = await self.db.delete_verification(user.id)
        self.rate_limiter.reset(user.id)

        # Remove faculty roles across mutual guilds
        removed_from: list[str] = []
        mutual_guilds = await self.service.get_mutual_guilds_for_user(user.id)
        for guild in mutual_guilds:
            member = await self.service.get_or_fetch_member(guild, user.id)
            if member:
                roles_to_remove = [r for r in member.roles if r.name in FACULTY_ROLE_NAMES]
                for r in roles_to_remove:
                    try:
                        await member.remove_roles(r, reason=f"TARVeri unverify by {interaction.user}: {reason}")
                        removed_from.append(f"{guild.name} ({r.name})")
                    except discord.HTTPException:
                        pass

        await self.db.log(
            "INFO",
            "ADMIN_UNVERIFY",
            f"Admin {interaction.user} (ID: {interaction.user.id}) unverified {user} (ID: {user.id}). Reason: {reason}",
            guild=interaction.guild,
            user_id=user.id,
        )

        removed_summary = ", ".join(removed_from) if removed_from else "No active roles removed"
        await interaction.followup.send(
            f"✅ Successfully unverified {user.mention}.\nRoles removed: {removed_summary}",
            ephemeral=True,
        )

    @app_commands.command(name="audit", description="Query recent audit log entries.")
    @app_commands.describe(
        limit="Number of entries to fetch (max 25)",
        event_type="Filter by event type (e.g. VERIFIED, RATE_LIMITED, ADMIN_UNVERIFY)",
    )
    async def audit(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
        event_type: str | None = None,
    ) -> None:
        """Retrieves and formats recent audit log entries from the database."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        filter_type = event_type.strip().upper() if event_type else None
        entries = await self.db.recent_audit(limit=limit, event_type=filter_type)

        if not entries:
            msg = f"No audit log records found{' for event `' + filter_type + '`' if filter_type else ''}."
            await interaction.followup.send(msg, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"📋 Audit Log Entries ({len(entries)})",
            color=discord.Color.gold(),
        )

        for ts, level, ev_type, g_name, uid, msg in entries:
            guild_str = f" • Server: {g_name}" if g_name else ""
            user_str = f" • User ID: `{uid}`" if uid else ""
            name = f"[{level}] {ev_type} ({ts}){guild_str}{user_str}"
            embed.add_field(name=name[:256], value=msg[:1024], inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="resync", description="Force resynchronization of verification roles.")
    @app_commands.describe(user="User to resynchronize (defaults to yourself if omitted)")
    async def resync(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        """Resyncs roles for a user across all shared guilds."""
        target = user or interaction.user
        is_admin = self._check_admin(interaction)

        if target.id != interaction.user.id and not is_admin:
            await interaction.response.send_message(
                "❌ You can only resync your own roles unless you are an administrator.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        existing = await self.db.get_verification_by_user(target.id)
        if not existing:
            await interaction.followup.send(
                f"⚠️ {'You are' if target.id == interaction.user.id else f'{target.mention} is'} not verified.",
                ephemeral=True,
            )
            return

        _, stored_faculty, _ = existing
        faculty_role = FACULTY_ROLES.get(stored_faculty)
        if not faculty_role:
            await interaction.followup.send("❌ Stored faculty role is invalid.", ephemeral=True)
            return

        mutual_guilds = await self.service.get_mutual_guilds_for_user(target.id)
        result = await self.service.assign_role_across_guilds(target.id, faculty_role, mutual_guilds)

        if target.id != interaction.user.id:
            await self.db.log(
                "INFO",
                "ADMIN_RESYNC",
                f"Admin {interaction.user} (ID: {interaction.user.id}) force-resynced roles for {target} (ID: {target.id})",
                guild=interaction.guild,
                user_id=target.id,
            )

        summary = self.service.format_role_summary(result)
        await interaction.followup.send(
            summary or "ℹ️ All roles are already up to date.",
            ephemeral=True,
        )

    @app_commands.command(name="backup", description="Create an immediate point-in-time database backup.")
    async def backup(self, interaction: discord.Interaction) -> None:
        """Creates a consistent SQLite backup snapshot."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            backup_path = await self.db.create_backup()
            await self.db.log(
                "INFO",
                "ADMIN_BACKUP",
                f"Admin {interaction.user} (ID: {interaction.user.id}) created database snapshot at '{backup_path}'",
                guild=interaction.guild,
                user_id=interaction.user.id,
            )
            await interaction.followup.send(
                f"✅ Database backup created successfully at `{backup_path}`.", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Backup failed: {e}", ephemeral=True)

    @app_commands.command(name="sync_commands", description="Force sync application slash commands.")
    @app_commands.describe(guild_only="Sync only to this guild (faster) or globally")
    async def sync_commands(self, interaction: discord.Interaction, guild_only: bool = False) -> None:
        """Manually forces a sync of the Discord application command tree."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            if guild_only and interaction.guild:
                self.bot.tree.copy_global_to(guild=interaction.guild)
                synced = await self.bot.tree.sync(guild=interaction.guild)
                scope = f"server '{interaction.guild.name}'"
            else:
                synced = await self.bot.tree.sync()
                scope = "globally"

            await self.db.log(
                "INFO",
                "ADMIN_SYNC_COMMANDS",
                f"Admin {interaction.user} (ID: {interaction.user.id}) synced {len(synced)} command(s) {scope}",
                guild=interaction.guild,
                user_id=interaction.user.id,
            )
            await interaction.followup.send(
                f"✅ Successfully synced {len(synced)} command(s) {scope}.", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to sync commands: {e}", ephemeral=True)
