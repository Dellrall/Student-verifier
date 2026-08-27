import pytest
from unittest.mock import MagicMock
import discord
from tarveri.cogs.admin_cog import is_admin_or_has_role


def test_is_admin_or_has_role():
    admin_role_name = "TARVeri Admin"

    # User without guild
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = None
    assert is_admin_or_has_role(interaction, admin_role_name) is False

    # Member with administrator permission
    interaction.guild = MagicMock(spec=discord.Guild)
    member_admin = MagicMock(spec=discord.Member)
    member_admin.guild_permissions.administrator = True
    member_admin.roles = []
    interaction.user = member_admin
    assert is_admin_or_has_role(interaction, admin_role_name) is True

    # Member with admin role
    member_role = MagicMock(spec=discord.Member)
    member_role.guild_permissions.administrator = False
    role = MagicMock(spec=discord.Role)
    role.name = "TARVeri Admin"
    member_role.roles = [role]
    interaction.user = member_role
    assert is_admin_or_has_role(interaction, admin_role_name) is True

    # Regular member without admin permission or role
    member_regular = MagicMock(spec=discord.Member)
    member_regular.guild_permissions.administrator = False
    other_role = MagicMock(spec=discord.Role)
    other_role.name = "Member"
    member_regular.roles = [other_role]
    interaction.user = member_regular
    assert is_admin_or_has_role(interaction, admin_role_name) is False
