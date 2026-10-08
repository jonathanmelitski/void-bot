"""/group: private channels the bot makes, and everything that can be done to them afterwards.

Every channel /group create makes is recorded in the group_channels table, and the other commands
only act on channels in that table, so the bot can never be pointed at a channel it didn't make.
Who is in a group isn't stored anywhere: it's the members named in the channel's permissions.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import config
from ..checks import admin_only
from ..db import Database
from ..validation import format_phone

log = logging.getLogger(__name__)

CAN_SEE = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
# What archiving takes away from everyone in the channel. They can still read it.
NO_NEW_MESSAGES = dict(
    send_messages=False, send_messages_in_threads=False, create_public_threads=False, create_private_threads=False
)
TOPIC_PREFIX = "Group created by "
ARCHIVE_CATEGORY_NAME = "Archived groups"
CATEGORY_CAPACITY = 50  # Discord's limit on channels in a category
VISIT_CHECK_SECONDS = 20
MISSING_PERMISSIONS = (
    "I don't have permission to do that. My role needs **Manage Channels** and **Manage Roles** "
    "(Discord needs the second one to change who can see a channel)."
)


def group_members(channel: discord.TextChannel) -> list[discord.Member]:
    """The people in a group: members the channel's permissions name and let see it."""
    return [
        target for target, overwrite in channel.overwrites.items()
        if isinstance(target, discord.Member) and not target.bot and overwrite.view_channel
    ]


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

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: CAN_SEE,
            **{m: CAN_SEE for m in members},
        }
        category = guild.get_channel(config.GROUP_CATEGORY_ID) if config.GROUP_CATEGORY_ID else None

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            channel = await guild.create_text_channel(
                name,
                category=category,
                overwrites=overwrites,
                topic=f"{TOPIC_PREFIX}{creator.display_name}",
                reason=f"/group create by {creator}",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to create channels. Give my role **Manage Channels**.", ephemeral=True
            )
            return

        await self.db.add_group_channel(channel.id, channel.name, creator.id)
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


async def _group_choices(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Active groups whose channel name contains what's been typed. The value is the channel ID, as
    text: Discord IDs are too big for a number option."""
    needle = current.strip().lstrip("#").lower()
    choices = []
    for row in await interaction.client.db.list_group_channels():
        channel = interaction.guild.get_channel(row["channel_id"])
        if channel and needle in channel.name.lower():
            choices.append(app_commands.Choice(name=f"#{channel.name}"[:100], value=str(channel.id)))
    return choices[:25]


@app_commands.guild_only()
class Groups(commands.GroupCog, group_name="group", group_description="Make and manage private group channels."):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await admin_only(interaction)

    async def cog_load(self):
        self.end_visits.start()

    async def cog_unload(self):
        self.end_visits.cancel()

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if not isinstance(getattr(error, "original", error), discord.Forbidden):
            return  # fall through to the bot-wide handler
        if interaction.response.is_done():
            await interaction.followup.send(MISSING_PERMISSIONS, ephemeral=True)
        else:
            await interaction.response.send_message(MISSING_PERMISSIONS, ephemeral=True)
        interaction.extras["handled"] = True

    async def _find(self, interaction: discord.Interaction, group: str) -> discord.TextChannel | None:
        """The active group's channel, or None after telling the user why not. Only channels in the
        bot's own list are ever returned."""
        row = await self.db.get_group_channel(int(group)) if group.isdigit() else None
        channel = row and interaction.guild.get_channel(row["channel_id"])
        if not row:
            msg = "That isn't a group I created. Pick one from the list that appears as you type."
        elif not channel:
            await self.db.delete_group_channel(row["channel_id"])
            msg = f"The channel for **{row['name']}** has been deleted, so I've dropped it from my list."
        elif row["archived_at"]:
            msg = f"{channel.mention} is archived."
        else:
            return channel
        await interaction.response.send_message(msg, ephemeral=True)
        return None

    @app_commands.command(description="Create a private channel for you and the people you pick.")
    async def create(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CreateGroupModal(self.bot.db))

    @app_commands.command(description="Add a group channel I made before I kept a list, so it can be managed.")
    async def adopt(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if await self.db.get_group_channel(channel.id):
            await interaction.response.send_message(f"{channel.mention} is already in my list.", ephemeral=True)
            return
        # The topic every /group create channel is given. Without it there's no sign the bot made it.
        if not (channel.topic or "").startswith(TOPIC_PREFIX):
            await interaction.response.send_message(
                f"{channel.mention} doesn't look like a channel I created, so I'm leaving it alone.", ephemeral=True
            )
            return
        await self.db.add_group_channel(channel.id, channel.name, interaction.user.id)
        log.info("%s adopted #%s as a group", interaction.user, channel.name)
        await interaction.response.send_message(f"Added {channel.mention} to my list of groups.", ephemeral=True)

    # Named list_ so it doesn't shadow the built-in `list` in the class body.
    @app_commands.command(name="list", description="The groups I've created.")
    @app_commands.describe(include_archived="Also show archived groups")
    async def list_(self, interaction: discord.Interaction, include_archived: bool = False):
        visits = await self.db.group_visits()
        lines = []
        for row in await self.db.list_group_channels(include_archived=include_archived):
            channel = interaction.guild.get_channel(row["channel_id"])
            if not channel:
                await self.db.delete_group_channel(row["channel_id"])  # deleted while the bot was off
                continue
            visiting = {v["discord_id"] for v in visits if v["channel_id"] == channel.id}
            members = [m for m in group_members(channel) if m.id not in visiting]
            line = f"{channel.mention}{' (archived)' if row['archived_at'] else ''} · {len(members)} member(s)"
            if members:
                line += ": " + ", ".join(m.mention for m in members)
            if visiting:
                line += " · visiting: " + ", ".join(f"<@{i}>" for i in visiting)
            lines.append(line)
        if not lines:
            await interaction.response.send_message("No groups yet. Make one with `/group create`.", ephemeral=True)
            return
        embed = discord.Embed(
            title=f"Groups ({len(lines)})", description="\n".join(lines)[:4096], color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Add someone to a group.")
    @app_commands.autocomplete(group=_group_choices)
    async def add(self, interaction: discord.Interaction, group: str, member: discord.Member):
        if not (channel := await self._find(interaction, group)):
            return
        if member.bot:
            await interaction.response.send_message("Bots can't be in a group.", ephemeral=True)
            return
        # Someone who was only visiting becomes a member: they keep their access and the timer is dropped.
        was_visiting = await self.db.remove_group_visit(channel.id, member.id)
        if member in group_members(channel) and not was_visiting:
            await interaction.response.send_message(f"{member.mention} is already in {channel.mention}.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await channel.set_permissions(member, overwrite=CAN_SEE, reason=f"/group add by {interaction.user}")
        log.info("%s added %s to group #%s", interaction.user, member, channel.name)
        # Discord doesn't tell people when they're let into a channel, so say it there.
        await channel.send(f"{member.mention} was added to the group by {interaction.user.mention}.")
        await interaction.followup.send(f"Added {member.mention} to {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Remove someone from a group.")
    @app_commands.autocomplete(group=_group_choices)
    async def remove(self, interaction: discord.Interaction, group: str, member: discord.User):
        if not (channel := await self._find(interaction, group)):
            return
        await self.db.remove_group_visit(channel.id, member.id)
        target = next((t for t in channel.overwrites if isinstance(t, discord.Member) and t.id == member.id), None)
        if target is None or target == interaction.guild.me:
            await interaction.response.send_message(f"{member.mention} isn't in {channel.mention}.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await channel.set_permissions(target, overwrite=None, reason=f"/group remove by {interaction.user}")
        log.info("%s removed %s from group #%s", interaction.user, member, channel.name)
        await interaction.followup.send(f"Removed {member.mention} from {channel.mention}.", ephemeral=True)

    @app_commands.command(description="Let yourself into a group for a few minutes, e.g. to post a message.")
    @app_commands.describe(minutes="How long to stay (default 5)")
    @app_commands.autocomplete(group=_group_choices)
    async def join(
        self, interaction: discord.Interaction, group: str, minutes: app_commands.Range[int, 1, 60] = 5
    ):
        if not (channel := await self._find(interaction, group)):
            return
        me = interaction.user
        visiting = any(v["discord_id"] == me.id for v in await self.db.group_visits(channel_id=channel.id))
        if me in group_members(channel) and not visiting:
            await interaction.response.send_message(f"You're already a member of {channel.mention}.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        # Recorded first: if the bot dies between the two steps, the visit still ends on time.
        await self.db.add_group_visit(channel.id, me.id, until)
        await channel.set_permissions(me, overwrite=CAN_SEE, reason=f"/group join by {me}, for {minutes} min")
        log.info("%s joined group #%s for %d min", me, channel.name, minutes)
        await interaction.followup.send(
            f"You're in {channel.mention} until {discord.utils.format_dt(until, 't')} "
            f"({discord.utils.format_dt(until, 'R')}). I'll take you out then.",
            ephemeral=True,
        )

    @app_commands.command(description="Archive a group: rename it, move it to the archive, and stop new messages.")
    @app_commands.autocomplete(group=_group_choices)
    async def archive(self, interaction: discord.Interaction, group: str):
        if not (channel := await self._find(interaction, group)):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        visiting = {v["discord_id"] for v in await self.db.group_visits(channel_id=channel.id)}
        overwrites = {}
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Member) and target.id in visiting:
                continue  # visitors don't stay on in the archive
            if target != guild.me:
                overwrite.update(**NO_NEW_MESSAGES)
            overwrites[target] = overwrite
        category = await self._archive_category(guild)
        await channel.edit(
            name=f"archived-{channel.name}"[:100],
            category=category,
            overwrites=overwrites,
            reason=f"/group archive by {interaction.user}",
        )
        await self.db.archive_group_channel(channel.id)
        log.info("%s archived group #%s", interaction.user, channel.name)
        await interaction.followup.send(
            f"Archived {channel.mention} under **{category.name}**. Its members can still read it, but not post.",
            ephemeral=True,
        )

    async def _archive_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        """A category with room: GROUP_ARCHIVE_CATEGORY_ID, then any the bot made earlier, then a new one."""
        configured = guild.get_channel(config.GROUP_ARCHIVE_CATEGORY_ID) if config.GROUP_ARCHIVE_CATEGORY_ID else None
        ours = [c for c in guild.categories if c.name.startswith(ARCHIVE_CATEGORY_NAME)]
        for category in filter(None, [configured, *ours]):
            if isinstance(category, discord.CategoryChannel) and len(category.channels) < CATEGORY_CAPACITY:
                return category
        name = ARCHIVE_CATEGORY_NAME if not ours else f"{ARCHIVE_CATEGORY_NAME} {len(ours) + 1}"
        return await guild.create_category(
            name,
            overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)},
            reason="Somewhere to put archived groups",
        )

    # ---- keeping the list and the visits true ----

    @tasks.loop(seconds=VISIT_CHECK_SECONDS)
    async def end_visits(self):
        try:
            await self._end_expired_visits()
        except Exception:  # an error here would stop the loop for good, leaving visitors in place
            log.exception("Ending group visits failed; trying again in %d s", VISIT_CHECK_SECONDS)

    async def _end_expired_visits(self):
        for visit in await self.db.group_visits(expired_only=True):
            channel = self.bot.get_channel(visit["channel_id"])
            member = channel and channel.guild.get_member(visit["discord_id"])
            if member:
                try:
                    await channel.set_permissions(member, overwrite=None, reason="/group join ran out")
                except discord.NotFound:
                    pass
                except discord.HTTPException as e:
                    log.error("Couldn't take %s out of #%s after their visit, will retry: %s", member, channel.name, e)
                    continue
                log.info("%s's visit to group #%s ended", member, channel.name)
            await self.db.remove_group_visit(visit["channel_id"], visit["discord_id"])

    @end_visits.before_loop
    async def _wait_until_ready(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await self.db.delete_group_channel(channel.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Groups(bot))
