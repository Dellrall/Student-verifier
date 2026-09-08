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
from tarveri.services.update_checker import UpdateCheckerService
from tarveri.services.verification_service import VerificationService
from tarveri.utils import format_ticket_seq, schedule_ttl_delete


def is_admin_or_has_role(interaction: discord.Interaction, admin_role_name: str) -> bool:
    """Checks if invoking user has Administrator permission, the configured Admin role, or a standard admin/staff role."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    from tarveri.cogs.guest_cog import get_admin_role_or_fallback
    admin_role = get_admin_role_or_fallback(interaction.guild, admin_role_name)
    if admin_role and admin_role in interaction.user.roles:
        return True
    return any(r.name.lower() == admin_role_name.lower() for r in interaction.user.roles)



class AdminCog(commands.Cog, name="Admin"):
    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        service: VerificationService,
        rate_limiter: RateLimiter,
        admin_role_name: str,
        update_checker: UpdateCheckerService | None = None,
    ):
        self.bot = bot
        self.db = db
        self.service = service
        self.rate_limiter = rate_limiter
        self.admin_role_name = admin_role_name
        self.update_checker = update_checker

    def _check_admin(self, interaction: discord.Interaction) -> bool:
        return is_admin_or_has_role(interaction, self.admin_role_name)

    @app_commands.command(name="stats", description="View student verification statistics.")
    @app_commands.default_permissions(administrator=True)
    async def stats(self, interaction: discord.Interaction) -> None:
        """Displays total verifications, faculty breakdown, and recent activity."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
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

        if interaction.guild:
            guild_settings = await self.db.get_guild_settings(interaction.guild.id)
            w_id = guild_settings[0] if guild_settings else None
            h_id = guild_settings[1] if guild_settings else None
            g_role = (
                guild_settings[2]
                if guild_settings and len(guild_settings) > 2 and guild_settings[2]
                else "Guest(Approved)"
            )
            r_id = guild_settings[3] if guild_settings and len(guild_settings) > 3 else None

            w_ch = interaction.guild.get_channel(w_id) if w_id else None
            h_ch = interaction.guild.get_channel(h_id) if h_id else None
            r_ch = interaction.guild.get_channel(r_id) if r_id else None

            w_display = w_ch.mention if w_ch else (f"`ID: {w_id}`" if w_id else "*Auto-detect*")
            h_display = h_ch.mention if h_ch else (f"`ID: {h_id}`" if h_id else "*Auto-detect*")
            r_display = r_ch.mention if r_ch else (f"`ID: {r_id}`" if r_id else "*Auto-detect*")

            adm_role = (
                guild_settings[4]
                if guild_settings and len(guild_settings) > 4 and guild_settings[4]
                else f"*Auto-detect ({self.admin_role_name})*"
            )

            embed.add_field(name="Welcome Channel", value=w_display, inline=True)
            embed.add_field(name="Help Channel", value=h_display, inline=True)
            embed.add_field(name="Guest Role", value=f"`{g_role}`", inline=True)
            embed.add_field(name="Review Channel", value=r_display, inline=True)
            embed.add_field(name="Admin / Review Role", value=f"`{adm_role}`", inline=True)

        embed.set_footer(text=f"TARVeri Bot • Active in {len(self.bot.guilds)} servers")
        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(
        name="diagnose",
        description="Run self-healing diagnostics and verify server permissions, roles, and channels.",
    )
    @app_commands.default_permissions(administrator=True)
    async def diagnose(self, interaction: discord.Interaction) -> None:
        """Runs role hierarchy check, channel validity checks, and auto-reconciliation."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message("❌ This command must be used within a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        warnings = self.service.diagnose_guild_permissions(guild)

        # Trigger member role reconciliation for this guild
        reconcile_stats = await self.service.reconcile_verified_members(guild)

        # Check channel configurations
        settings = await self.db.get_guild_settings(guild.id)
        w_id = settings[0] if settings else None
        h_id = settings[1] if settings else None
        r_id = settings[3] if settings and len(settings) > 3 else None

        channel_status = []
        if w_id:
            w_ch = guild.get_channel(w_id)
            if not w_ch:
                await self.db.clear_stale_channel_setting(guild.id, "welcome")
                channel_status.append("⚠️ Stale welcome channel ID was detected and auto-cleared.")
            else:
                channel_status.append(f"✅ Welcome channel: {w_ch.mention}")
        else:
            channel_status.append("ℹ️ Welcome channel: *Auto-detected*")

        if h_id:
            h_ch = guild.get_channel(h_id)
            if not h_ch:
                await self.db.clear_stale_channel_setting(guild.id, "help")
                channel_status.append("⚠️ Stale help channel ID was detected and auto-cleared.")
            else:
                channel_status.append(f"✅ Help channel: {h_ch.mention}")
        else:
            channel_status.append("ℹ️ Help channel: *Auto-detected*")

        if r_id:
            r_ch = guild.get_channel(r_id)
            if not r_ch:
                await self.db.clear_stale_channel_setting(guild.id, "review")
                channel_status.append("⚠️ Stale review channel ID was detected and auto-cleared.")
            else:
                channel_status.append(f"✅ Review channel: {r_ch.mention}")
        else:
            channel_status.append("ℹ️ Review channel: *Auto-detected*")

        embed = discord.Embed(
            title="🛡️ TARVeri — Server Health & Diagnostics",
            color=discord.Color.green() if not warnings else discord.Color.orange(),
        )

        if warnings:
            embed.add_field(
                name="⚠️ Permission / Hierarchy Issues Detected",
                value="\n".join(f"• {w}" for w in warnings),
                inline=False,
            )
        else:
            embed.add_field(
                name="✅ Permissions & Role Hierarchy",
                value="All required permissions and role positions are properly configured.",
                inline=False,
            )

        embed.add_field(
            name="🔄 Self-Healing Member Reconciliation",
            value=f"• Checked **{reconcile_stats['checked']}** verified student(s)\n• Restored **{reconcile_stats['restored']}** missing role(s)\n• Failed **{reconcile_stats['failed']}** role(s)",
            inline=False,
        )

        embed.add_field(
            name="📁 Channel Configuration & Auto-Recovery",
            value="\n".join(channel_status),
            inline=False,
        )

        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(name="unverify", description="Unlink a member's student ID and revoke faculty roles.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user="The Discord user to unverify", reason="Optional reason for unlinking")
    async def unverify(
        self, interaction: discord.Interaction, user: discord.User, reason: str = "Admin unverified"
    ) -> None:
        """Unbinds a Discord user and strips faculty roles across mutual guilds."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        existing = await self.db.get_verification_by_user(user.id)
        if not existing:
            await interaction.followup.send(f"⚠️ User {user.mention} is not verified in TARVeri.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
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
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(name="audit", description="Query recent audit log entries.")
    @app_commands.default_permissions(administrator=True)
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
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        filter_type = event_type.strip().upper() if event_type else None
        entries = await self.db.recent_audit(limit=limit, event_type=filter_type)

        if not entries:
            msg = f"No audit log records found{' for event `' + filter_type + '`' if filter_type else ''}."
            await interaction.followup.send(msg, ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
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
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(name="resync", description="Force resynchronization of verification roles.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user="User to resynchronize (defaults to yourself if omitted)")
    async def resync(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        """Resyncs roles for a user across all shared guilds."""
        target = user or interaction.user
        is_admin = self._check_admin(interaction)

        if target.id != interaction.user.id and not is_admin:
            await interaction.response.send_message(
                "❌ You can only resync your own roles unless you are an administrator.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        existing = await self.db.get_verification_by_user(target.id)
        if not existing:
            await interaction.followup.send(
                f"⚠️ {'You are' if target.id == interaction.user.id else f'{target.mention} is'} not verified.",
                ephemeral=True,
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        _, stored_faculty, _ = existing
        faculty_role = FACULTY_ROLES.get(stored_faculty)
        if not faculty_role:
            await interaction.followup.send("❌ Stored faculty role is invalid.", ephemeral=True)
            schedule_ttl_delete(interaction, delay=60.0)
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
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(name="backup", description="Create an immediate point-in-time database backup.")
    @app_commands.default_permissions(administrator=True)
    async def backup(self, interaction: discord.Interaction) -> None:
        """Creates a consistent SQLite backup snapshot."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
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
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(
        name="sync_commands",
        description="Sync slash commands or clean up duplicate guild command overrides.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        clean_duplicates="Clean and remove duplicate guild commands, leaving only global commands (Recommended)",
        guild_only="Sync commands only to this guild (useful for immediate developer testing)",
    )
    async def sync_commands(
        self,
        interaction: discord.Interaction,
        clean_duplicates: bool = True,
        guild_only: bool = False,
    ) -> None:
        """Manually forces a sync or cleanup of the Discord application command tree."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            if guild_only and interaction.guild:
                self.bot.tree.copy_global_to(guild=interaction.guild)
                synced = await self.bot.tree.sync(guild=interaction.guild)
                scope = f"server '{interaction.guild.name}'"
                msg_suffix = "*(Note: If commands appear duplicated, run `/sync_commands clean_duplicates:True`)*"
            else:
                if clean_duplicates and interaction.guild:
                    self.bot.tree.clear_commands(guild=interaction.guild)
                    await self.bot.tree.sync(guild=interaction.guild)
                synced = await self.bot.tree.sync()
                scope = "globally (deduplicated)"
                msg_suffix = "*(Duplicate guild commands cleared. Press `Ctrl + R` on Discord Desktop or restart your app to refresh your cache)*"

            await self.db.log(
                "INFO",
                "ADMIN_SYNC_COMMANDS",
                f"Admin {interaction.user} (ID: {interaction.user.id}) synced {len(synced)} command(s) {scope}",
                guild=interaction.guild,
                user_id=interaction.user.id,
            )
            await interaction.followup.send(
                f"✅ Successfully synced {len(synced)} command(s) {scope}.\n{msg_suffix}",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Failed to sync commands: {e}", ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @commands.command(name="sync", aliases=["sync_commands"])
    async def sync_prefix(self, ctx: commands.Context, scope: str = "clean") -> None:
        """Text fallback command to immediately sync slash commands or clean duplicates."""
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return
        if not (ctx.author.guild_permissions.administrator or any(r.name == self.admin_role_name for r in ctx.author.roles)):
            await ctx.send("❌ You do not have permission to sync commands.")
            return

        msg = await ctx.send("🔄 Syncing slash commands...")
        try:
            if scope.lower() in ("guild", "here"):
                self.bot.tree.copy_global_to(guild=ctx.guild)
                synced = await self.bot.tree.sync(guild=ctx.guild)
                await msg.edit(
                    content=(
                        f"✅ Instantly synced **{len(synced)}** slash command(s) to **{ctx.guild.name}**!\n"
                        f"*(Tip: If commands appear duplicated in your menu, run `!sync clean` to remove duplicate guild overrides)*"
                    )
                )
            elif scope.lower() in ("clean", "clear", "fix", "dedupe"):
                self.bot.tree.clear_commands(guild=ctx.guild)
                await self.bot.tree.sync(guild=ctx.guild)
                synced = await self.bot.tree.sync()
                await msg.edit(
                    content=(
                        f"✅ **Deduplicated & Synced!** Cleared duplicate guild commands in **{ctx.guild.name}** and synced **{len(synced)}** global command(s)!\n"
                        f"*(Tip: Press `Ctrl + R` on Discord Desktop or restart your Discord app to refresh your command cache)*"
                    )
                )
            else:
                synced = await self.bot.tree.sync()
                await msg.edit(
                    content=f"✅ Synced **{len(synced)}** global command(s)!"
                )
        except Exception as e:
            await msg.edit(content=f"❌ Failed to sync commands: {e}")

    @app_commands.command(
        name="check_updates",
        description="Check if bot updates are available from git upstream.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        stream="Optional branch/stream name to check against (defaults to configured stream)"
    )
    async def check_updates(
        self, interaction: discord.Interaction, stream: str | None = None
    ) -> None:
        """Checks git upstream for new commits on the configured or specified branch."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        checker = self.update_checker
        if not checker:
            checker = UpdateCheckerService(
                bot=self.bot,
                db=self.db,
                update_stream=stream or "auto",
            )

        is_avail, count, local_h, remote_h, target_stream = await checker.check_for_updates(
            custom_stream=stream
        )

        embed = discord.Embed(
            title="🔄 TARVeri Update Status",
            color=discord.Color.green() if not is_avail else discord.Color.gold(),
        )
        embed.add_field(name="Target Stream", value=f"`{target_stream}`", inline=False)
        embed.add_field(name="Local Version", value=f"`{local_h[:7]}`" if local_h else "*Unknown*", inline=True)
        embed.add_field(name="Remote Version", value=f"`{remote_h[:7]}`" if remote_h else "*Unknown*", inline=True)

        if is_avail:
            branch_arg = target_stream.replace("origin/", "").strip()
            embed.description = (
                f"🔔 **Update available!** Remote is **{count} commit(s)** ahead.\n\n"
                f"To update, run on your server terminal:\n"
                f"```bash\n./scripts/update.sh {branch_arg}\n```"
            )
        else:
            if not remote_h:
                embed.description = f"⚠️ Could not resolve remote branch `{target_stream}`. Check if the branch exists on remote."
            else:
                embed.description = "✅ TARVeri is up to date on this stream!"

        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(
        name="setwelcomec",
        description="Set or reset the server's welcome channel where new members are tagged to verify.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        channel="The text channel where new members will be tagged (leave empty to reset to auto-detect)"
    )
    async def setwelcomec(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        """Configures or clears the welcome channel for this server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        channel_id = channel.id if channel else None
        await self.db.set_guild_welcome_channel(interaction.guild.id, channel_id)

        # Invalidate VerificationCog channel cache if loaded
        verification_cog = self.bot.get_cog("Verification")
        if verification_cog and hasattr(verification_cog, "invalidate_guild_cache"):
            verification_cog.invalidate_guild_cache(interaction.guild.id)

        await self.db.log(
            "INFO",
            "CONFIG_WELCOME_CHANNEL",
            f"Admin {interaction.user} set welcome channel for server '{interaction.guild.name}' (ID: {interaction.guild.id}) to '{channel.name if channel else 'Auto-detect'}' (Channel ID: {channel_id})",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        if channel:
            await interaction.followup.send(
                f"✅ Welcome channel set to {channel.mention}.\n"
                f"New unverified members joining this server will be tagged here with verification instructions.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "🔄 Welcome channel reset to **auto-detect** mode (searches for #welcome, #verify, or system channel).",
                ephemeral=True,
            )
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(
        name="sethelpc",
        description="Set or reset the server's help channel for automated role verification tips.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        channel="The text channel for role help tips (leave empty to reset to auto-detect)"
    )
    async def sethelpc(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        """Configures or clears the help channel for this server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        channel_id = channel.id if channel else None
        await self.db.set_guild_help_channel(interaction.guild.id, channel_id)

        # Invalidate VerificationCog channel cache if loaded
        verification_cog = self.bot.get_cog("Verification")
        if verification_cog and hasattr(verification_cog, "invalidate_guild_cache"):
            verification_cog.invalidate_guild_cache(interaction.guild.id)

        await self.db.log(
            "INFO",
            "CONFIG_HELP_CHANNEL",
            f"Admin {interaction.user} set help channel for server '{interaction.guild.name}' (ID: {interaction.guild.id}) to '{channel.name if channel else 'Auto-detect'}' (Channel ID: {channel_id})",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        if channel:
            await interaction.followup.send(
                f"✅ Help channel set to {channel.mention}.\n"
                f"Unverified members asking about roles in {channel.mention} will receive helpful verification tips.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "🔄 Help channel reset to **auto-detect** mode (searches for channels with 'help', 'support', 'faq', etc.).",
                ephemeral=True,
            )
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(
        name="setguestrole",
        description="Set or reset the server's custom role name for verified guests (default: Guest).",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role_name="The name of the guest role (leave empty to reset to 'Guest')")
    async def setguestrole(
        self, interaction: discord.Interaction, role_name: str | None = None
    ) -> None:
        """Configures or clears the guest role name for this server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        role_to_set = role_name.strip() if role_name else "Guest"
        await self.db.set_guild_guest_role(interaction.guild.id, role_to_set)

        await self.db.log(
            "INFO",
            "CONFIG_GUEST_ROLE",
            f"Admin {interaction.user} set guest role for server '{interaction.guild.name}' (ID: {interaction.guild.id}) to '{role_to_set}'",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        await interaction.followup.send(
            f"✅ Guest role name for this server set to **{role_to_set}**.",
            ephemeral=True,
        )
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(
        name="setreviewchannel",
        description="Set or reset the parent channel where private guest review threads are created.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The text channel for private review threads (leave empty for auto-detect)")
    async def setreviewchannel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        """Configures or clears the parent review channel for private guest threads."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        channel_id = channel.id if channel else None
        await self.db.set_guild_review_channel(interaction.guild.id, channel_id)

        await self.db.log(
            "INFO",
            "CONFIG_REVIEW_CHANNEL",
            f"Admin {interaction.user} set guest review channel for server '{interaction.guild.name}' (ID: {interaction.guild.id}) to '{channel.name if channel else 'Auto-detect'}' (Channel ID: {channel_id})",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        if channel:
            await interaction.followup.send(
                f"✅ Guest review channel set to {channel.mention}.\n"
                f"Private review threads for guest applications will be created under this channel.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "🔄 Guest review channel reset to **auto-detect** mode.",
                ephemeral=True,
            )
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(
        name="setadminrole",
        description="Set or reset the server's reviewer/admin role for private guest review threads.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        role="The role whose members should manage verifications/reviews (leave empty for auto-detect)"
    )
    async def setadminrole(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        """Configures or clears the custom admin/reviewer role for this server."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)
        role_name = role.name if role else None
        await self.db.set_guild_admin_role(interaction.guild.id, role_name)

        await self.db.log(
            "INFO",
            "CONFIG_ADMIN_ROLE",
            f"Admin {interaction.user} set admin role for server '{interaction.guild.name}' (ID: {interaction.guild.id}) to '{role_name if role_name else 'Auto-detect'}'",
            guild=interaction.guild,
            user_id=interaction.user.id,
        )

        if role:
            await interaction.followup.send(
                f"✅ Review/Admin role for this server set to {role.mention}.\n"
                f"Members with this role will be automatically invited to review new guest applications and referral tickets.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "🔄 Review/Admin role reset to **auto-detect** mode (checks server admins, moderators, and staff roles).",
                ephemeral=True,
            )
        schedule_ttl_delete(interaction, delay=60.0)

    @app_commands.command(
        name="guest_tickets",
        description="List and inspect recent guest review tickets with links to threads.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        status="Filter by status (OPEN, APPROVED, REJECTED, EXPIRED, LEFT_SERVER)",
        limit="Number of records to show (1-20, default 10)",
    )
    async def guest_tickets(
        self,
        interaction: discord.Interaction,
        status: str | None = None,
        limit: app_commands.Range[int, 1, 20] = 10,
    ) -> None:
        """Displays recent guest review tickets with clickable thread links and resolution details."""
        if not self._check_admin(interaction):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used inside a server.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        await interaction.response.defer(ephemeral=True)

        tickets = await self.db.list_guest_tickets(
            interaction.guild.id, status=status, limit=limit
        )

        if not tickets:
            filter_text = f" with status `{status}`" if status else ""
            await interaction.followup.send(
                f"ℹ️ No guest tickets found in this server{filter_text}.", ephemeral=True
            )
            schedule_ttl_delete(interaction, delay=60.0)
            return

        embed = discord.Embed(
            title=f"📋 Guest Review Tickets — {interaction.guild.name}",
            description=f"Showing **{len(tickets)}** recent ticket(s)" + (f" filtered by `{status.upper()}`" if status else "") + ":",
            color=discord.Color.blue(),
        )

        for t in tickets:
            seq = t.get("ticket_seq") or t.get("ticket_id")
            seq_code = format_ticket_seq(seq)
            t_status = t.get("status", "OPEN")
            status_emoji = {
                "OPEN": "⏳",
                "APPROVED": "✅",
                "REJECTED": "🛑",
                "EXPIRED": "⏰",
                "LEFT_SERVER": "🚪",
                "BANNED": "🔨",
            }.get(t_status, "📄")

            thread_mention = f"<#{t['channel_id']}>"
            applicant_mention = f"<@{t['applicant_id']}>"
            admin_info = f" • Closed by <@{t['closed_by_admin_id']}>" if t.get("closed_by_admin_id") else ""
            reason_info = f"\n> Reason: *\"{t['close_reason']}\"*" if t.get("close_reason") else ""

            field_name = f"{status_emoji} Ticket #{seq_code} — {t_status}"
            field_value = (
                f"**Applicant:** {applicant_mention} | **Thread:** {thread_mention}{admin_info}\n"
                f"**Created:** `{t.get('created_at', 'N/A')}`{reason_info}"
            )
            embed.add_field(name=field_name, value=field_value, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
        schedule_ttl_delete(interaction, delay=90.0)





