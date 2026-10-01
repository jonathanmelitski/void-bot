"""/throwing-mgr: throwing groups, for admins.

A group is a set of members with a life (start and end date). `creategroups` splits a pool of people
into random groups, shows a private preview, and on Confirm saves them and posts them in the channel.
The other commands list, edit, and delete groups. Which sessions count towards a group's minutes is
worked out by the session_groups view (bot/db.py) from each group's two settings.
"""

import logging
import random
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from .. import config
from ..checks import admin_only
from .players import RetryView

log = logging.getLogger(__name__)

TZ = ZoneInfo(config.TIMEZONE)
PREVIEW_SECONDS = 15 * 60
MAX_GROUP_SIZE = 25
SETTINGS = {
    # value: (label, description)
    "allow_solo": ("Allow solo groups", "If one person is left over they get a group of their own. Otherwise they join another group."),
    "count_solo": ("Group minutes count solo throwing", "A session with only one of the group's members counts."),
    "require_all": ("Group minutes require all members", "A session with several of the group's members, but not all of them, doesn't count."),
    "end_others": ("End all other groups", "Every group that's running or yet to start ends now."),
}


class InputError(Exception):
    """Something typed into a form is wrong; the message is shown to the admin."""


# ---- dates ----

def parse_day(text: str, what: str) -> date:
    """2026-10-02, or 10/2 (this year), or 10/2/2026."""
    text = text.strip()
    try:
        if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text):
            return date(int(m[3] or datetime.now(TZ).year), int(m[1]), int(m[2]))
        return date.fromisoformat(text)
    except ValueError:
        raise InputError(f"{what} `{text}` isn't a date. Use YYYY-MM-DD, like {datetime.now(TZ):%Y-%m-%d}.") from None


def parse_life(start_text: str, end_text: str) -> tuple[datetime, datetime]:
    """First and last day (inclusive) -> [start of the first day, start of the day after the last)."""
    first, last = parse_day(start_text, "Start date"), parse_day(end_text, "End date")
    if last < first:
        raise InputError("The end date is before the start date.")
    return datetime.combine(first, time(), TZ), datetime.combine(last + timedelta(days=1), time(), TZ)


def split_dates(text: str) -> tuple[str, str]:
    """ "2026-10-02 to 2026-10-08" -> its two dates. A single date means a one-day life."""
    found = re.findall(r"\d[\d/-]*\d|\d", text)
    if len(found) == 1:
        return found[0], found[0]
    if len(found) != 2:
        raise InputError(f"Dates `{text}` should be a first and last day, like `2026-10-02 to 2026-10-08`.")
    return found[0], found[1]


def first_day(group: dict) -> date:
    return datetime.fromisoformat(group["starts_at"]).astimezone(TZ).date()


def last_day(group: dict) -> date:
    """The last day the group is alive on (its end is exclusive)."""
    return (datetime.fromisoformat(group["ends_at"]) - timedelta(seconds=1)).astimezone(TZ).date()


def life_text(starts_at: datetime, ends_at: datetime) -> str:
    first, last = starts_at.astimezone(TZ), (ends_at - timedelta(seconds=1)).astimezone(TZ)
    return f"{first:%b} {first.day} – {last:%b} {last.day}"


def group_life_text(group: dict) -> str:
    return life_text(datetime.fromisoformat(group["starts_at"]), datetime.fromisoformat(group["ends_at"]))


def has_ended(group: dict) -> bool:
    return datetime.fromisoformat(group["ends_at"]) <= datetime.now(timezone.utc)


def rules_text(count_solo: bool, require_all: bool) -> str:
    """What counts towards a group's minutes, in a line."""
    counted = ["sessions with the whole group"]
    if not require_all:
        counted.append("two or more of its members")
    if count_solo:
        counted.append("one member on their own")
    return "Group minutes count " + ", ".join(counted) + "."


# ---- making groups ----

def make_groups(member_ids: list[int], size: int, allow_solo: bool) -> list[list[int]]:
    """Shuffle the pool into groups of `size`. A smaller group takes the leftovers; a single
    leftover person joins the last full group unless solo groups are allowed (or the size is 1)."""
    pool = list(member_ids)
    random.shuffle(pool)
    groups = [pool[i:i + size] for i in range(0, len(pool), size)]
    if size > 1 and len(groups) > 1 and len(groups[-1]) == 1 and not allow_solo:
        groups[-2].extend(groups.pop())
    return groups


def chunks(lines: list[str], limit: int = 2000) -> list[str]:
    """Join lines into as few messages as fit Discord's length limit."""
    out, current = [], ""
    for line in lines:
        if current and len(current) + 1 + len(line) > limit:
            out.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line[:limit]
    return out + [current] if current else out


class CreateGroupsModal(discord.ui.Modal, title="Create throwing groups"):
    # A form holds five fields at most, which is why the two dates share one.
    pool = discord.ui.Label(
        text="Pool",
        description="Roles and/or people to split into groups. Bots are left out.",
        component=discord.ui.MentionableSelect(min_values=1, max_values=25),
    )
    exclude = discord.ui.Label(
        text="Exclude",
        description="Roles and/or people to leave out, even if they're in the pool.",
        component=discord.ui.MentionableSelect(min_values=0, max_values=25, required=False),
    )
    size = discord.ui.Label(
        text="Group size",
        component=discord.ui.TextInput(default="2", max_length=2),
    )
    dates = discord.ui.Label(
        text="Dates",
        description="First and last day the groups count for: YYYY-MM-DD to YYYY-MM-DD.",
        component=discord.ui.TextInput(max_length=30),
    )
    settings = discord.ui.Label(
        text="Settings",
        component=discord.ui.CheckboxGroup(
            required=False,
            options=[
                discord.CheckboxGroupOption(label=label, value=value, description=description)
                for value, (label, description) in SETTINGS.items()
            ],
        ),
    )

    def __init__(self, db, typed: dict[str, str] | None = None):
        super().__init__()
        self.db = db
        today = datetime.now(TZ).date()
        typed = typed or {"size": "2", "dates": f"{today} to {today + timedelta(days=6)}"}
        self.size.component.default = typed["size"]
        self.dates.component.default = typed["dates"]

    @staticmethod
    def _people(picked: list) -> dict[int, discord.Member]:
        """The human members behind a mix of picked roles and people."""
        people = {}
        for item in picked:
            for m in item.members if isinstance(item, discord.Role) else [item]:
                if isinstance(m, discord.Member) and not m.bot:
                    people[m.id] = m
        return people

    async def on_submit(self, interaction: discord.Interaction):
        typed = {name: getattr(self, name).component.value.strip() for name in ("size", "dates")}
        chosen = set(self.settings.component.values)
        if not interaction.guild.chunked:
            await interaction.guild.chunk()  # role.members is only complete once the member list is loaded
        excluded = self._people(self.exclude.component.values)
        members = {i: m for i, m in self._people(self.pool.component.values).items() if i not in excluded}
        try:
            if not typed["size"].isdigit() or not 1 <= int(typed["size"]) <= MAX_GROUP_SIZE:
                raise InputError(f"Group size has to be a number from 1 to {MAX_GROUP_SIZE}.")
            starts_at, ends_at = parse_life(*split_dates(typed["dates"]))
            if not members:
                raise InputError("Nobody is left in that pool." if excluded else "Nobody is in that pool.")
            if len(members) == 1 and "allow_solo" not in chosen and int(typed["size"]) > 1:
                raise InputError("There's only one person in that pool, and solo groups aren't allowed.")
        except InputError as e:
            # The pool, exclusions and checkboxes have to be picked again; the typed fields are kept.
            await interaction.response.send_message(
                f"Not created: {e}", view=RetryView(lambda: CreateGroupsModal(self.db, typed)), ephemeral=True
            )
            return

        preview = PreviewView(
            self.db, list(members), int(typed["size"]), starts_at, ends_at,
            allow_solo="allow_solo" in chosen, count_solo="count_solo" in chosen,
            require_all="require_all" in chosen, end_others="end_others" in chosen,
        )
        await interaction.response.send_message(preview.text(), view=preview, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Error in the create-groups form", exc_info=error)
        msg = "Something went wrong making the groups."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class PreviewView(discord.ui.View):
    """The private preview: Confirm saves the groups and posts them in the channel."""

    def __init__(self, db, member_ids, size, starts_at, ends_at, *, allow_solo, count_solo, require_all, end_others):
        super().__init__(timeout=PREVIEW_SECONDS)
        self.db = db
        self.member_ids, self.size, self.allow_solo = member_ids, size, allow_solo
        self.starts_at, self.ends_at = starts_at, ends_at
        self.count_solo, self.require_all, self.end_others = count_solo, require_all, end_others
        self.groups = make_groups(member_ids, size, allow_solo)
        self.settled = False

    def group_lines(self) -> list[str]:
        return [f"**Group {n}** " + " ".join(f"<@{i}>" for i in group) for n, group in enumerate(self.groups, 1)]

    def text(self) -> str:
        lines = [
            f"**Preview: {len(self.groups)} group(s) from {len(self.member_ids)} people**, {life_text(self.starts_at, self.ends_at)}",
            *self.group_lines(),
            f"-# {rules_text(self.count_solo, self.require_all)}",
        ]
        if self.end_others:
            lines.append("-# Confirming also ends every other group that's running or yet to start.")
        text = "\n".join(lines)
        return text if len(text) <= 2000 else text[:1900] + "\n… (too long to show in full; Confirm posts all of them)"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.settled:
            await interaction.response.send_message("This preview has already been used.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.settled = True
        self.stop()
        ids = await self.db.create_groups(
            [(f"Group {n}", group) for n, group in enumerate(self.groups, 1)],
            starts_at=self.starts_at, ends_at=self.ends_at,
            count_solo=self.count_solo, require_all=self.require_all,
            created_by=interaction.user.id, end_others=self.end_others,
        )
        log.info("%s created throwing groups %s (%s)", interaction.user, ids, life_text(self.starts_at, self.ends_at))
        await interaction.response.edit_message(content=f"Created {len(ids)} group(s) and posted them here.", view=None)
        lines = [
            f"**Throwing groups, {life_text(self.starts_at, self.ends_at)}**",
            *self.group_lines(),
            f"-# {rules_text(self.count_solo, self.require_all)}",
        ]
        try:
            for part in chunks(lines):
                # Silent: everyone sees their mention, but nobody gets a notification.
                await interaction.channel.send(
                    part, silent=True, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False)
                )
        except discord.HTTPException as e:
            log.warning("Couldn't post the new groups in %s: %s", interaction.channel, e)
            await interaction.followup.send(
                "The groups are saved, but I couldn't post them in this channel. `/throwing-mgr list` shows them.",
                ephemeral=True,
            )

    @discord.ui.button(label="Reshuffle", style=discord.ButtonStyle.secondary)
    async def reshuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.groups = make_groups(self.member_ids, self.size, self.allow_solo)
        await interaction.response.edit_message(content=self.text(), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.settled = True
        self.stop()
        await interaction.response.edit_message(content="Cancelled. No groups were created.", view=None)


class EditGroupModal(discord.ui.Modal):
    def __init__(self, db, group: dict, typed: dict[str, str] | None = None):
        super().__init__(title=f"Edit {group['name']}"[:45])
        self.db = db
        self.group = group
        typed = typed or {"name": group["name"], "start": str(first_day(group)), "end": str(last_day(group))}
        self.name = discord.ui.TextInput(default=typed["name"], max_length=50)
        self.start = discord.ui.TextInput(default=typed["start"], max_length=10)
        self.end = discord.ui.TextInput(default=typed["end"], max_length=10)
        self.settings = discord.ui.CheckboxGroup(
            required=False,
            options=[
                discord.CheckboxGroupOption(label=SETTINGS[v][0], value=v, description=SETTINGS[v][1], default=group[v])
                for v in ("count_solo", "require_all")
            ],
        )
        self.add_item(discord.ui.Label(text="Name", component=self.name))
        self.add_item(discord.ui.Label(text="Start date", description="First day it counts for. YYYY-MM-DD.", component=self.start))
        self.add_item(discord.ui.Label(text="End date", description="Last day it counts for. YYYY-MM-DD.", component=self.end))
        self.add_item(discord.ui.Label(text="Settings", component=self.settings))

    async def on_submit(self, interaction: discord.Interaction):
        typed = {"name": self.name.value.strip(), "start": self.start.value.strip(), "end": self.end.value.strip()}
        chosen = set(self.settings.values)
        try:
            if not typed["name"]:
                raise InputError("The name is empty.")
            starts_at, ends_at = parse_life(typed["start"], typed["end"])
        except InputError as e:
            await interaction.response.send_message(
                f"Not saved: {e}", view=RetryView(lambda: EditGroupModal(self.db, self.group, typed)), ephemeral=True
            )
            return
        saved = await self.db.update_group(
            self.group["id"], name=typed["name"], starts_at=starts_at, ends_at=ends_at,
            count_solo="count_solo" in chosen, require_all="require_all" in chosen,
        )
        if not saved:
            await interaction.response.send_message("That group no longer exists.", ephemeral=True)
            return
        log.info("%s edited throwing group %s", interaction.user, self.group["id"])
        await interaction.response.send_message(
            "Saved.\n" + describe(await self.db.get_group(self.group["id"])), ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Error editing throwing group %s", self.group["id"], exc_info=error)
        msg = "Something went wrong saving the group."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


def one_line(group: dict) -> str:
    ended = " · ended" if has_ended(group) else ""
    members = " ".join(f"<@{i}>" for i in group["members"]) or "no members"
    return (
        f"`#{group['id']}` **{group['name']}** · {group_life_text(group)}{ended} · "
        f"{group['minutes']} min in {group['sessions']} session(s) · {members}"
    )


def describe(group: dict) -> str:
    return f"{one_line(group)}\n-# {rules_text(group['count_solo'], group['require_all'])}"


@app_commands.guild_only()
class ThrowingMgr(commands.GroupCog, group_name="throwing-mgr", group_description="Manage throwing groups."):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await admin_only(interaction)

    async def _group_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        """Autocomplete for a group: running ones first, matched on ID, name, or a member's name."""
        choices = []
        groups = await self.db.list_groups(include_ended=True)
        for group in sorted(groups, key=has_ended):
            names = ", ".join(
                m.display_name if (m := interaction.guild.get_member(i)) else str(i) for i in group["members"]
            )
            label = f"#{group['id']} {group['name']} · {group_life_text(group)}{' · ended' if has_ended(group) else ''} · {names}"
            if current.lower().lstrip("#") in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=group["id"]))
        return choices[:25]

    async def _find(self, interaction: discord.Interaction, group_id: int) -> dict | None:
        group = await self.db.get_group(group_id)
        if group is None:
            await interaction.response.send_message(
                f"There's no group #{group_id}. Pick one from the list that appears as you type.", ephemeral=True
            )
        return group

    @app_commands.command(description="Split a pool of people into random throwing groups.")
    async def creategroups(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CreateGroupsModal(self.db))

    # Named list_ so it doesn't shadow the built-in `list` in the class body.
    @app_commands.command(name="list", description="Show throwing groups and their minutes.")
    @app_commands.describe(include_ended="Also show groups that have ended")
    async def list_(self, interaction: discord.Interaction, include_ended: bool = False):
        groups = await self.db.list_groups(include_ended=include_ended)
        if not groups:
            await interaction.response.send_message(
                "No groups." if include_ended else "No running or upcoming groups. Try `include_ended`.", ephemeral=True
            )
            return
        parts = chunks([one_line(g) for g in groups])
        await interaction.response.send_message(parts[0], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        for part in parts[1:]:
            await interaction.followup.send(part, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(description="Show one throwing group: members, dates, settings, minutes.")
    @app_commands.autocomplete(group=_group_choices)
    async def show(self, interaction: discord.Interaction, group: int):
        if found := await self._find(interaction, group):
            await interaction.response.send_message(
                describe(found), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )

    @app_commands.command(description="Change a throwing group's name, dates, or settings.")
    @app_commands.autocomplete(group=_group_choices)
    async def edit(self, interaction: discord.Interaction, group: int):
        if found := await self._find(interaction, group):
            await interaction.response.send_modal(EditGroupModal(self.db, found))

    @app_commands.command(name="add-member", description="Add someone to a throwing group.")
    @app_commands.autocomplete(group=_group_choices)
    async def add_member(self, interaction: discord.Interaction, group: int, member: discord.Member):
        if not (found := await self._find(interaction, group)):
            return
        if member.bot:
            await interaction.response.send_message("Bots can't be in a group.", ephemeral=True)
            return
        added = await self.db.add_group_member(group, member.id)
        log.info("%s added %s to throwing group %s", interaction.user, member, group)
        await interaction.response.send_message(
            (f"Added {member.mention}.\n" if added else f"{member.mention} was already in it.\n")
            + describe(await self.db.get_group(group)),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="remove-member", description="Remove someone from a throwing group.")
    @app_commands.autocomplete(group=_group_choices)
    async def remove_member(self, interaction: discord.Interaction, group: int, member: discord.User):
        if not (found := await self._find(interaction, group)):
            return
        removed = await self.db.remove_group_member(group, member.id)
        log.info("%s removed %s from throwing group %s", interaction.user, member, group)
        await interaction.response.send_message(
            (f"Removed {member.mention}.\n" if removed else f"{member.mention} wasn't in it.\n")
            + describe(await self.db.get_group(group)),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(description="End a throwing group now. It keeps its minutes.")
    @app_commands.autocomplete(group=_group_choices)
    async def end(self, interaction: discord.Interaction, group: int):
        if not (found := await self._find(interaction, group)):
            return
        ended = await self.db.end_group(group)
        log.info("%s ended throwing group %s", interaction.user, group)
        await interaction.response.send_message(
            ("Ended.\n" if ended else "It had already ended.\n") + describe(await self.db.get_group(group)),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(description="Delete a throwing group. Logged sessions aren't touched.")
    @app_commands.autocomplete(group=_group_choices)
    async def delete(self, interaction: discord.Interaction, group: int):
        if not (found := await self._find(interaction, group)):
            return
        await self.db.delete_group(group)
        log.info("%s deleted throwing group %s", interaction.user, group)
        await interaction.response.send_message(
            "Deleted:\n" + one_line(found), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ThrowingMgr(bot))
