import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import config
from ..checks import admin_only
from ..db import Database
from ..validation import format_phone

log = logging.getLogger(__name__)


class CreateGroupModal(discord.ui.Modal, title="Create a group"):
    name = discord.ui.Label(
        text="Group name",
        description="Used as the channel name.",
        component=discord.ui.TextInput(placeholder="e.g. Doubles practice", max_length=100),
    )
    members = discord.ui.Label(
        text="Members",
        description="You're added automatically.",
        component=discord.ui.UserSelect(min_values=1, max_values=25),
    )
    include_contacts = discord.ui.Label(
        text="Include names and phone numbers",
        description="Lists everyone's name and phone number (from the player database) in the welcome message.",
        component=discord.ui.Checkbox(),
    )

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        creator = interaction.user
        name = self.name.component.value.strip()
        picked = [m for m in self.members.component.values if not m.bot and m.id != creator.id]
        members = [creator, *picked]

        can_see = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: can_see,
            **{m: can_see for m in members},
        }
        category = guild.get_channel(config.GROUP_CATEGORY_ID) if config.GROUP_CATEGORY_ID else None

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await guild.create_text_channel(
                name,
                category=category,
                overwrites=overwrites,
                topic=f"Group created by {creator.display_name}",
                reason=f"/group create by {creator}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to create channels. Give my role **Manage Channels**.", ephemeral=True
            )
            return

        log.info("%s created group #%s with %d member(s)", creator, channel.name, len(members))
        missing = []
        if self.include_contacts.component.value:
            roster, missing = await self._contact_roster(members)
            welcome = f"Welcome to **{name}**! Group created by {creator.mention}.\n\n{roster}"
        else:
            welcome = (
                f"Welcome to **{name}**! Group created by {creator.mention} with "
                + ", ".join(m.mention for m in picked or [creator])
                + "."
            )
        await channel.send(welcome)

        reply = f"Created {channel.mention} with {len(members)} member(s)."
        if missing:
            reply += (
                f"\nNo name or phone on file for {', '.join(m.mention for m in missing)}. "
                "Add them with `/player update`."
            )
        await interaction.followup.send(reply, ephemeral=True)

    async def _contact_roster(self, members: list[discord.Member]) -> tuple[str, list[discord.Member]]:
        """One line per member: @mention · name · phone. Also returns members missing either."""
        lines, missing = [], []
        for m in members:
            player = await self.db.get_player(m.id)
            full_name = player and " ".join(filter(None, [player.first_name, player.last_name]))
            phone = player and format_phone(player.phone)
            if not (full_name and phone):
                missing.append(m)
            lines.append(f"• {m.mention} · {full_name or 'no name on file'} · {phone or 'no phone on file'}")
        return "\n".join(lines), missing

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Error creating group", exc_info=error)
        msg = "Something went wrong creating the group."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


@app_commands.guild_only()
class Groups(commands.GroupCog, group_name="group", group_description="Make private group channels."):
    def __init__(self, bot):
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await admin_only(interaction)

    @app_commands.command(description="Create a private channel for you and the people you pick.")
    async def create(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CreateGroupModal(self.bot.db))


async def setup(bot: commands.Bot):
    await bot.add_cog(Groups(bot))
