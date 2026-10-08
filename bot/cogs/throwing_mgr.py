"""/throwing-mgr: throwing groups, for admins.

A group is a set of members with a life (start and end date). `creategroups` splits a pool of people
into random groups, shows a private preview, and on Confirm saves them and posts them in the channel.
The other commands list, edit, and delete groups. Which sessions count towards a group's minutes is
worked out by the session_groups view (bot/db.py) from each group's two settings.

A goal (`/throwing-mgr goal ...`) is a number of minutes each person in a pool should throw every
cycle. Once a minute the bot checks each goal: at the start of a cycle it can make that cycle's
groups, and at the goal's reminder times it pings the people who aren't there yet.

How groups are made is described once, by GroupConfig: its fields are the settings, SETTINGS is how
they're labelled, and GroupConfigFields is how a form asks for them. `creategroups` and goals both go
through those, and a goal stores its GroupConfig as JSON, so a new setting is added in one place.
"""

import logging
import random
import re
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import config, goals
from ..checks import admin_only
from .players import RetryView

log = logging.getLogger(__name__)

TZ = ZoneInfo(config.TIMEZONE)
PREVIEW_SECONDS = 15 * 60
MAX_GROUP_SIZE = 25
REMINDER_GRACE = timedelta(hours=3)  # a reminder the bot was down for is skipped if it's later than this
MENTION_USERS = discord.AllowedMentions(users=True, roles=False, everyone=False)
SETTINGS = {
    # GroupConfig field: (label, description)
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

@dataclass
class GroupConfig:
    """How a pool is split into groups, and what the groups count. Every on/off field needs an entry
    in SETTINGS; that's all a new setting takes to show up in the forms and be stored with goals."""

    size: int = 2
    allow_solo: bool = False
    count_solo: bool = False
    require_all: bool = False
    end_others: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "GroupConfig":
        """From a goal's stored JSON. Settings added since it was saved get their defaults."""
        return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})

    def summary(self) -> str:
        return f"groups of {self.size}" + (", ending all other groups" if self.end_others else "")


class GroupConfigFields:
    """The form fields that ask for a GroupConfig: the size and a checkbox per setting."""

    def __init__(self, typed_size: str | None = None):
        self.size = discord.ui.TextInput(default=typed_size or str(GroupConfig.size), max_length=2)
        self.settings = discord.ui.CheckboxGroup(
            required=False,
            options=[
                discord.CheckboxGroupOption(label=label, value=value, description=description)
                for value, (label, description) in SETTINGS.items()
            ],
        )

    def add_to(self, modal: discord.ui.Modal):
        modal.add_item(discord.ui.Label(text="Group size", component=self.size))
        modal.add_item(discord.ui.Label(text="Group settings", component=self.settings))

    @property
    def typed_size(self) -> str:
        return self.size.value.strip()

    def read(self) -> GroupConfig:
        if not self.typed_size.isdigit() or not 1 <= int(self.typed_size) <= MAX_GROUP_SIZE:
            raise InputError(f"Group size has to be a number from 1 to {MAX_GROUP_SIZE}.")
        chosen = set(self.settings.values)
        return GroupConfig(size=int(self.typed_size), **{name: name in chosen for name in SETTINGS})


class PoolFields:
    """The form fields that pick who something applies to: roles and people in, roles and people out."""

    def __init__(self, what: str):
        self.pool = discord.ui.MentionableSelect(min_values=1, max_values=25)
        self.exclude = discord.ui.MentionableSelect(min_values=0, max_values=25, required=False)
        self.what = what

    def add_to(self, modal: discord.ui.Modal):
        modal.add_item(discord.ui.Label(
            text="Pool", description=f"Roles and/or people {self.what}. Bots are left out.", component=self.pool
        ))
        modal.add_item(discord.ui.Label(
            text="Exclude", description="Roles and/or people to leave out, even if they're in the pool.",
            component=self.exclude,
        ))

    def targets(self) -> list[dict]:
        """What was picked, as {target_id, is_role, excluded}. Picked both ways, someone is excluded."""
        picked = {}
        for excluded, select in ((False, self.pool), (True, self.exclude)):
            for item in select.values:
                picked[item.id] = {"target_id": item.id, "is_role": isinstance(item, discord.Role), "excluded": excluded}
        return list(picked.values())


async def resolve_pool(guild: discord.Guild, targets: list[dict]) -> list[int]:
    """The human members behind a pool's roles and people right now, minus the excluded ones."""
    if not guild.chunked:
        await guild.chunk()  # role.members is only complete once the member list is loaded
    found: dict[bool, set[int]] = {False: set(), True: set()}
    for target in targets:
        if target["is_role"]:
            role = guild.get_role(target["target_id"])
            members = role.members if role else []
        else:
            member = guild.get_member(target["target_id"])
            members = [member] if member else []
        found[bool(target["excluded"])].update(m.id for m in members if not m.bot)
    return sorted(found[False] - found[True])


def make_groups(member_ids: list[int], cfg: GroupConfig) -> list[list[int]]:
    """Shuffle the pool into groups of cfg.size. A smaller group takes the leftovers; a single
    leftover person joins the last full group unless solo groups are allowed (or the size is 1)."""
    pool = list(member_ids)
    random.shuffle(pool)
    groups = [pool[i:i + cfg.size] for i in range(0, len(pool), cfg.size)]
    if cfg.size > 1 and len(groups) > 1 and len(groups[-1]) == 1 and not cfg.allow_solo:
        groups[-2].extend(groups.pop())
    return groups


def group_lines(groups: list[list[int]]) -> list[str]:
    return [f"**Group {n}** " + " ".join(f"<@{i}>" for i in group) for n, group in enumerate(groups, 1)]


async def save_and_post_groups(
    db, channel, groups: list[list[int]], cfg: GroupConfig, starts_at: datetime, ends_at: datetime,
    *, created_by: int, title: str = "Throwing groups", goal_cycle: tuple[int, int] | None = None,
) -> tuple[list[int], bool]:
    """Save groups and post them in a channel. Returns the new group IDs and whether the post went through."""
    ids = await db.create_groups(
        [(f"Group {n}", group) for n, group in enumerate(groups, 1)],
        starts_at=starts_at, ends_at=ends_at, count_solo=cfg.count_solo, require_all=cfg.require_all,
        created_by=created_by, end_others=cfg.end_others, goal_cycle=goal_cycle,
    )
    lines = [
        f"**{title}, {life_text(starts_at, ends_at)}**",
        *group_lines(groups),
        f"-# {rules_text(cfg.count_solo, cfg.require_all)}",
    ]
    try:
        for part in chunks(lines):
            # Silent: everyone sees their mention, but nobody gets a notification.
            await channel.send(part, silent=True, allowed_mentions=MENTION_USERS)
    except discord.HTTPException as e:
        log.warning("Couldn't post the new groups in %s: %s", channel, e)
        return ids, False
    return ids, True


def chunks(lines: list[str], limit: int = 2000) -> list[str]:
    """Join lines into as few messages as fit Discord's length limit."""
    out, current = [], ""
    for line in lines:
        if current and len(current) + 1 + len(line) > limit:
            out.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line[:limit]
    return out + [current] if current else out


async def modal_failed(interaction: discord.Interaction, msg: str):
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


class CreateGroupsModal(discord.ui.Modal, title="Create throwing groups"):
    # A form holds five fields at most, which is why the two dates share one.
    def __init__(self, db, typed: dict[str, str] | None = None):
        super().__init__()
        self.db = db
        today = datetime.now(TZ).date()
        typed = typed or {"size": None, "dates": f"{today} to {today + timedelta(days=6)}"}
        self.pool = PoolFields("to split into groups")
        self.dates = discord.ui.TextInput(default=typed["dates"], max_length=30)
        self.config = GroupConfigFields(typed["size"])
        self.pool.add_to(self)
        self.add_item(discord.ui.Label(
            text="Dates", description="First and last day the groups count for: YYYY-MM-DD to YYYY-MM-DD.",
            component=self.dates,
        ))
        self.config.add_to(self)

    async def on_submit(self, interaction: discord.Interaction):
        typed = {"size": self.config.typed_size, "dates": self.dates.value.strip()}
        targets = self.pool.targets()
        members = await resolve_pool(interaction.guild, targets)
        try:
            cfg = self.config.read()
            starts_at, ends_at = parse_life(*split_dates(typed["dates"]))
            if not members:
                excluded = any(t["excluded"] for t in targets)
                raise InputError("Nobody is left in that pool." if excluded else "Nobody is in that pool.")
            if len(members) == 1 and not cfg.allow_solo and cfg.size > 1:
                raise InputError("There's only one person in that pool, and solo groups aren't allowed.")
        except InputError as e:
            # The pool, exclusions and checkboxes have to be picked again; the typed fields are kept.
            await interaction.response.send_message(
                f"Not created: {e}", view=RetryView(lambda: CreateGroupsModal(self.db, typed)), ephemeral=True
            )
            return

        preview = PreviewView(self.db, members, cfg, starts_at, ends_at)
        await interaction.response.send_message(preview.text(), view=preview, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Error in the create-groups form", exc_info=error)
        await modal_failed(interaction, "Something went wrong making the groups.")


class PreviewView(discord.ui.View):
    """The private preview: Confirm saves the groups and posts them in the channel."""

    def __init__(self, db, member_ids: list[int], cfg: GroupConfig, starts_at: datetime, ends_at: datetime):
        super().__init__(timeout=PREVIEW_SECONDS)
        self.db = db
        self.member_ids, self.cfg = member_ids, cfg
        self.starts_at, self.ends_at = starts_at, ends_at
        self.groups = make_groups(member_ids, cfg)
        self.settled = False

    def text(self) -> str:
        lines = [
            f"**Preview: {len(self.groups)} group(s) from {len(self.member_ids)} people**, {life_text(self.starts_at, self.ends_at)}",
            *group_lines(self.groups),
            f"-# {rules_text(self.cfg.count_solo, self.cfg.require_all)}",
        ]
        if self.cfg.end_others:
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
        await interaction.response.edit_message(content="Creating the groups…", view=None)
        ids, posted = await save_and_post_groups(
            self.db, interaction.channel, self.groups, self.cfg, self.starts_at, self.ends_at,
            created_by=interaction.user.id,
        )
        log.info("%s created throwing groups %s (%s)", interaction.user, ids, life_text(self.starts_at, self.ends_at))
        await interaction.edit_original_response(
            content=f"Created {len(ids)} group(s) and posted them here." if posted else
            f"Created {len(ids)} group(s), but I couldn't post them in this channel. `/throwing-mgr list` shows them."
        )

    @discord.ui.button(label="Reshuffle", style=discord.ButtonStyle.secondary)
    async def reshuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.groups = make_groups(self.member_ids, self.cfg)
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


# ---- goals ----

def parse_remind_days(text: str | None, cycle_days: int) -> list[int]:
    """ "2, 0" -> [2, 0]: how many days before a cycle's last day to remind on."""
    days = re.findall(r"\d+", text or "")
    if text and text.strip() and (not days or re.sub(r"[\d\s,]", "", text)):
        raise InputError(f"Reminder days `{text}` should be numbers separated by commas, like `2, 0`.")
    days = sorted({int(d) for d in days}, reverse=True)
    if any(d >= cycle_days for d in days):
        raise InputError(f"Reminder days have to be from 0 (the last day) to {cycle_days - 1} (the first day).")
    return days


def pool_text(targets: list[dict]) -> str:
    def mentions(excluded: bool) -> str:
        return ", ".join(
            f"<@{'&' if t['is_role'] else ''}{t['target_id']}>" for t in targets if bool(t["excluded"]) == excluded
        )

    return mentions(False) + (f", except {mentions(True)}" if mentions(True) else "")


def describe_goal(goal: dict) -> str:
    now = datetime.now(TZ)
    cycle = goals.current_cycle(goal, now, TZ)
    if cycle is None:
        when = f"starts {date.fromisoformat(goal['first_day']):%b %-d}"
    else:
        when = f"now {life_text(*goals.cycle_bounds(goal, cycle, TZ))}"
    every = "week" if goal["cycle_days"] == 7 else "day" if goal["cycle_days"] == 1 else f"{goal['cycle_days']} days"
    lines = [
        f"`#{goal['id']}` **{goal['name']}** · {goal['minutes']} min each, every {every} ({when}) · <#{goal['channel_id']}>",
        f"-# For {pool_text(goal['targets'])}.",
    ]
    if goal["remind_days_before"]:
        days = ", ".join("the last day" if d == 0 else f"{d} day(s) before it" for d in sorted(goal["remind_days_before"]))
        lines.append(f"-# Reminds people who are short at {goal['remind_hour']:02d}:00 on {days}.")
    else:
        lines.append("-# No reminders.")
    if goal["group_config"]:
        cfg = GroupConfig.from_dict(goal["group_config"])
        lines.append(f"-# Makes {cfg.summary()} each cycle. {rules_text(cfg.count_solo, cfg.require_all)}")
    return "\n".join(lines)


async def goal_progress(db, guild: discord.Guild, goal: dict, cycle: int) -> list[tuple[int, int]]:
    """(Discord ID, minutes thrown this cycle) for everyone the goal applies to, fewest minutes first.
    Every session a person took part in counts, whatever group it was or wasn't with."""
    members = await resolve_pool(guild, goal["targets"])
    if not members:
        return []
    start, end = goals.cycle_bounds(goal, cycle, TZ)
    thrown = {r["discord_id"]: r["minutes"] for r in await db.throwing_totals(start, end, members, limit=len(members))}
    return sorted(((i, thrown.get(i, 0)) for i in members), key=lambda pair: pair[1])


class CreateGoalModal(discord.ui.Modal, title="Who is the goal for?"):
    """The second half of /throwing-mgr goal create: the pool, and the groups to make if it makes any."""

    def __init__(self, db, goal: dict, make_groups: bool, typed_size: str | None = None):
        super().__init__()
        self.db = db
        self.goal = goal  # what was given to the command
        self.pool = PoolFields("the goal applies to")
        self.pool.add_to(self)
        self.config = GroupConfigFields(typed_size) if make_groups else None
        if self.config:
            self.config.add_to(self)

    async def on_submit(self, interaction: discord.Interaction):
        targets = self.pool.targets()
        try:
            cfg = self.config.read() if self.config else None
            if not await resolve_pool(interaction.guild, targets):
                raise InputError("Nobody is in that pool.")
        except InputError as e:
            typed_size = self.config and self.config.typed_size
            await interaction.response.send_message(
                f"Not created: {e}",
                view=RetryView(lambda: CreateGoalModal(self.db, self.goal, bool(self.config), typed_size)),
                ephemeral=True,
            )
            return
        goal_id = await self.db.create_goal(
            **self.goal, group_config=cfg and asdict(cfg), targets=targets, created_by=interaction.user.id
        )
        log.info("%s created throwing goal %s (%s)", interaction.user, goal_id, self.goal["name"])
        note = "\nThis cycle's groups will be posted in its channel within a minute." if cfg else ""
        await interaction.response.send_message(
            "Goal created.\n" + describe_goal(await self.db.get_goal(goal_id)) + note,
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Error in the create-goal form", exc_info=error)
        await modal_failed(interaction, "Something went wrong creating the goal.")


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


    # ---- goals ----

    goal = app_commands.Group(name="goal", description="Recurring minutes goals.")

    async def _goal_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
        choices = []
        for g in await self.db.list_goals():
            label = f"#{g['id']} {g['name']} · {g['minutes']} min every {g['cycle_days']} day(s)"
            if current.lower().lstrip("#") in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=g["id"]))
        return choices[:25]

    async def _find_goal(self, interaction: discord.Interaction, goal_id: int) -> dict | None:
        found = await self.db.get_goal(goal_id)
        if found is None:
            await interaction.response.send_message(
                f"There's no goal #{goal_id}. Pick one from the list that appears as you type.", ephemeral=True
            )
        return found

    @goal.command(name="create", description="Set a minutes goal that repeats. A form then asks who it's for.")
    @app_commands.describe(
        name="What to call it, e.g. Weekly throwing",
        minutes="Minutes each person should throw per cycle",
        channel="Where reminders (and new groups) are posted",
        make_groups="Make new random throwing groups from the goal's people at the start of every cycle",
        cycle_days="How long a cycle is. Default 7",
        first_day="First day of the first cycle, YYYY-MM-DD. Default: Monday of this week",
        remind_days_before="Days before a cycle's last day to remind on, e.g. \"2, 0\" (0 is the last day). Blank: no reminders",
        remind_hour="Hour of the day reminders go out, 0-23. Default 18",
    )
    async def goal_create(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 50],
        minutes: app_commands.Range[int, 1, 10000],
        channel: discord.TextChannel,
        make_groups: bool = False,
        cycle_days: app_commands.Range[int, 1, 366] = 7,
        first_day: str | None = None,
        remind_days_before: str | None = None,
        remind_hour: app_commands.Range[int, 0, 23] = 18,
    ):
        today = datetime.now(TZ).date()
        try:
            first = parse_day(first_day, "First day") if first_day else today - timedelta(days=today.weekday())
            remind_days = parse_remind_days(remind_days_before, cycle_days)
        except InputError as e:
            await interaction.response.send_message(f"Not created: {e}", ephemeral=True)
            return
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages):
            await interaction.response.send_message(
                f"I can't post in {channel.mention}. Give me **View Channel** and **Send Messages** there first.",
                ephemeral=True,
            )
            return
        details = dict(
            name=name.strip(), minutes=minutes, first_day=first.isoformat(), cycle_days=cycle_days,
            channel_id=channel.id, remind_days_before=remind_days, remind_hour=remind_hour,
        )
        await interaction.response.send_modal(CreateGoalModal(self.db, details, make_groups))

    @goal.command(name="list", description="Show the minutes goals.")
    async def goal_list(self, interaction: discord.Interaction):
        found = await self.db.list_goals()
        if not found:
            await interaction.response.send_message("No goals. Make one with `/throwing-mgr goal create`.", ephemeral=True)
            return
        parts = chunks([describe_goal(g) for g in found])
        await interaction.response.send_message(parts[0], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        for part in parts[1:]:
            await interaction.followup.send(part, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @goal.command(name="progress", description="Who has and hasn't reached a goal this cycle.")
    @app_commands.autocomplete(goal=_goal_choices)
    async def goal_progress_(self, interaction: discord.Interaction, goal: int):
        if not (found := await self._find_goal(interaction, goal)):
            return
        cycle = goals.current_cycle(found, datetime.now(TZ), TZ)
        if cycle is None:
            await interaction.response.send_message("That goal hasn't started yet.\n" + describe_goal(found), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        progress = await goal_progress(self.db, interaction.guild, found, cycle)
        reached = sum(minutes >= found["minutes"] for _, minutes in progress)
        lines = [
            f"**{found['name']}**, {life_text(*goals.cycle_bounds(found, cycle, TZ))}: "
            f"{reached} of {len(progress)} at {found['minutes']} min",
            *(f"{'✅' if minutes >= found['minutes'] else '▫️'} <@{i}> {minutes}" for i, minutes in progress[::-1]),
        ]
        for part in chunks(lines):
            await interaction.followup.send(part, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @goal.command(name="remind", description="Send a goal's reminder now, pinging everyone who isn't there yet.")
    @app_commands.autocomplete(goal=_goal_choices)
    async def goal_remind(self, interaction: discord.Interaction, goal: int):
        if not (found := await self._find_goal(interaction, goal)):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        log.info("%s sent the reminder for throwing goal %s by hand", interaction.user, goal)
        await interaction.followup.send(await self._remind(found), ephemeral=True)

    @goal.command(name="delete", description="Delete a goal. Sessions and groups it made aren't touched.")
    @app_commands.autocomplete(goal=_goal_choices)
    async def goal_delete(self, interaction: discord.Interaction, goal: int):
        if not (found := await self._find_goal(interaction, goal)):
            return
        await self.db.delete_goal(goal)
        log.info("%s deleted throwing goal %s", interaction.user, goal)
        await interaction.response.send_message(
            "Deleted:\n" + describe_goal(found), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    async def _remind(self, goal: dict) -> str:
        """Ping everyone who is short of the goal this cycle, in the goal's channel. Returns what happened."""
        channel = self.bot.get_channel(goal["channel_id"])
        cycle = goals.current_cycle(goal, datetime.now(TZ), TZ)
        if channel is None:
            return f"The goal's channel (<#{goal['channel_id']}>) is gone or hidden from me."
        if cycle is None:
            return "That goal hasn't started yet."
        short = [(i, m) for i, m in await goal_progress(self.db, channel.guild, goal, cycle) if m < goal["minutes"]]
        if not short:
            return "Nobody to remind: everyone has reached it."
        final = goals.last_day(goal, cycle)
        lines = [
            f"**{goal['name']}: {goal['minutes']} min by the end of {final:%A}, {final:%b} {final.day}.** Not there yet:",
            *(f"<@{i}> {minutes}/{goal['minutes']}" for i, minutes in short),
        ]
        for part in chunks(lines):
            await channel.send(part, allowed_mentions=MENTION_USERS)
        return f"Reminded {len(short)} people in {channel.mention}."

    @tasks.loop(minutes=1)
    async def run_goals(self):
        for goal in await self.db.list_goals():
            try:
                await self._run_goal(goal)
            except Exception:  # one broken goal mustn't stop the others, or the loop
                log.exception("Couldn't run throwing goal %s (%s)", goal["id"], goal["name"])

    async def _run_goal(self, goal: dict):
        now = datetime.now(TZ)
        cycle = goals.current_cycle(goal, now, TZ)
        if cycle is None:
            return

        if goal["group_config"] and goal["groups_cycle"] < cycle:
            channel = self.bot.get_channel(goal["channel_id"])
            members = await resolve_pool(channel.guild, goal["targets"]) if channel else []
            if not members:
                log.warning("Throwing goal %s made no groups this cycle: no channel or nobody in its pool", goal["id"])
                await self.db.mark_goal_groups(goal["id"], cycle)
            else:
                cfg = GroupConfig.from_dict(goal["group_config"])
                ids, _ = await save_and_post_groups(
                    self.db, channel, make_groups(members, cfg), cfg, *goals.cycle_bounds(goal, cycle, TZ),
                    created_by=self.bot.user.id, title=f"{goal['name']}: throwing groups", goal_cycle=(goal["id"], cycle),
                )
                log.info("Throwing goal %s made groups %s for cycle %d", goal["id"], ids, cycle)

        if due := goals.due_reminder(goal, now, TZ):
            # Marked before sending: if the send fails halfway, people aren't pinged again a minute later.
            await self.db.mark_goal_reminded(goal["id"], now)
            if now - due > REMINDER_GRACE:
                log.warning("Skipped throwing goal %s's %s reminder: the bot wasn't running then", goal["id"], f"{due:%a %H:%M}")
            else:
                log.info("Throwing goal %s reminder: %s", goal["id"], await self._remind(goal))

    @run_goals.before_loop
    async def _wait_until_ready(self):
        await self.bot.wait_until_ready()

    async def cog_load(self):
        self.run_goals.start()

    async def cog_unload(self):
        self.run_goals.cancel()


async def setup(bot: commands.Bot):
    await bot.add_cog(ThrowingMgr(bot))
