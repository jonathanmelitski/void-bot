import logging

import discord
from discord import app_commands

from . import config

log = logging.getLogger(__name__)


async def bot_in_guild(interaction: discord.Interaction) -> bool:
    """False (after telling the user) if the command was used in a server the bot hasn't joined,
    which happens when it was invited without the `bot` scope. The bot can't see roles or
    create channels there."""
    if interaction.guild is not None:
        return True
    log.warning("Command used in guild %s, which the bot is not a member of", interaction.guild_id)
    await interaction.response.send_message(
        "I'm not a member of this server. Re-invite me with the `bot` and `applications.commands` scopes.",
        ephemeral=True,
    )
    interaction.extras["handled"] = True
    return False


def is_bot_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or any(r.id in config.ADMIN_ROLE_IDS for r in member.roles)


async def admin_only(interaction: discord.Interaction) -> bool:
    """Cog interaction_check for admin commands: server admins and ADMIN_ROLE_IDS only."""
    if not await bot_in_guild(interaction):
        return False
    if not is_bot_admin(interaction.user):
        log.warning(
            "Denied /%s to %s: their roles %s, allowed roles %s",
            interaction.command and interaction.command.qualified_name,
            interaction.user,
            [r.id for r in interaction.user.roles],
            sorted(config.ADMIN_ROLE_IDS),
        )
        raise app_commands.CheckFailure()
    return True
