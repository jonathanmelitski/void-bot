"""/throwing-mgr: throwing groups, for admins.

A group is a set of members with a life (start and end date). `creategroups` splits a pool of people
into random groups, shows a private preview, and on Confirm saves them and posts them in the channel.
The other commands list, edit, and delete groups. Which sessions count towards a group's minutes is
worked out by the session_groups view (bot/db.py) from each group's two settings.

A goal (`/throwing-mgr goal ...`) is a number of minutes each person in a pool should throw every
cycle. Once a minute the bot checks each goal: at the start of a cycle it can make that cycle's
groups, and at the goal's reminder times it pings the people who aren't there yet. When cycles start
and end, daylight saving included, is worked out in bot/goals.py.

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


def moment_text(at: datetime) -> str:
    at = at.astimezone(TZ)
    return f"{at:%a %b} {at.day}, {at.hour % 12 or 12}:{at:%M %p}"


def life_text(starts_at: datetime, ends_at: datetime) -> str:
    """Whole days read as "Oct 5 – Oct 11". Anything that starts or ends during a day gets its times."""
    first, end = starts_at.astimezone(TZ), ends_at.astimezone(TZ)
    if (first.hour, first.minute, end.hour, end.minute) != (0, 0, 0, 0):
        return f"{moment_text(first)} – {moment_text(end)}"
    last = end.date() - timedelta(days=1)
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

    def __init__(self, typed_size: str | None = None, *, optional: bool = False):
        """With optional, the size starts blank and leaving it blank means "no groups"."""
        self.optional = optional
        default = typed_size if optional else typed_size or str(GroupConfig.size)
        self.size = discord.ui.TextInput(default=default or None, max_length=2, required=not optional)
        self.settings = discord.ui.CheckboxGroup(
            required=False,
            options=[
                discord.CheckboxGroupOption(label=label, value=value, description=description)
                for value, (label, description) in SETTINGS.items()
            ],
        )

    def add_to(self, modal: discord.ui.Modal):
        modal.add_item(discord.ui.Label(
            text="Group size", component=self.size,
            description="For new random groups every cycle. Leave blank to make none." if self.optional else None,
        ))
        modal.add_item(discord.ui.Label(text="Group settings", component=self.settings))

    @property
    def typed_size(self) -> str:
        return self.size.value.strip()

    def read(self) -> GroupConfig | None:
        if self.optional and not self.typed_size:
            return None
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

def parse_clock(text: str) -> time:
    """20:00, 8pm, 8:30 pm."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?", text.strip().lower().replace(".", ""))
    hour, minute = (int(m[1]), int(m[2] or 0)) if m else (99, 0)
    if m and m[3]:
        hour = hour % 12 + (12 if m[3][0] == "p" else 0) if 1 <= hour <= 12 else 99
    if not m or (m[2] is None and m[3] is None) or hour > 23 or minute > 59:
        raise InputError(f"`{text}` isn't a time of day. Use 20:00 or 8pm.")
    return time(hour, minute)


def parse_moment(text: str) -> datetime:
    """A date with an optional time of day: "2026-10-04 20:00", "10/4 8pm", "2026-10-05" (midnight).
    It's a time on the clock in the bot's time zone, with no time zone attached."""
    day, _, clock = text.strip().replace("T", " ", 1).partition(" ")
    return datetime.combine(parse_day(day, "Start date"), parse_clock(clock) if clock.strip() else time())


def parse_every(text: str) -> tuple[int, str]:
    """ "week", "2 weeks", "3 days", "12 hours", "1 month" -> (count, unit)."""
    m = re.fullmatch(r"(\d+)?\s*(hours?|hrs?|h|days?|d|weeks?|wks?|w|months?|mos?)", text.strip().lower())
    if not m or not 1 <= int(m[1] or 1) <= 1000:
        raise InputError(f"`{text}` isn't a repeat I understand. Use a number of hours, days, weeks or months, like `1 week`.")
    return int(m[1] or 1), next(u for u in goals.UNITS if u[0] == m[2][0])


def parse_reminders(text: str) -> list[str]:
    """ "2d, 6 hours, 30m" -> ["2d", "6h", "30m"]: how long before a cycle ends."""
    offsets = []
    for part in filter(None, (p.strip() for p in re.split(r",|\band\b", text.lower()))):
        m = re.fullmatch(r"(\d+)\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?)", part)
        if not m or int(m[1]) < 1:
            raise InputError(f"Reminder `{part}` should be a number of days, hours or minutes, like `2d`, `6h` or `30m`.")
        offsets.append(f"{int(m[1])}{m[2][0]}")
    return list(dict.fromkeys(offsets))


def every_text(goal: dict) -> str:
    return goal["every_unit"] if goal["every_count"] == 1 else f"{goal['every_count']} {goal['every_unit']}s"


def offset_text(offset: str) -> str:
    count, unit = goals.parse_offset(offset)
    return f"{count} {goals.OFFSET_UNITS[unit][:-1]}{'' if count == 1 else 's'}"


def pool_text(targets: list[dict]) -> str:
    def mentions(excluded: bool) -> str:
        return ", ".join(
            f"<@{'&' if t['is_role'] else ''}{t['target_id']}>" for t in targets if bool(t["excluded"]) == excluded
        )

    return mentions(False) + (f", except {mentions(True)}" if mentions(True) else "")


def describe_goal(goal: dict) -> str:
    cycle = goals.current_cycle(goal, datetime.now(TZ), TZ)
    if cycle is None:
        when = f"starts {moment_text(goals.cycle_start(goal, 0, TZ))}"
    else:
        when = f"now {life_text(*goals.cycle_bounds(goal, cycle, TZ))}"
    lines = [
        f"`#{goal['id']}` **{goal['name']}** · {goal['minutes']} min each, every {every_text(goal)} ({when}) · <#{goal['channel_id']}>",
        f"-# For {pool_text(goal['targets'])}.",
    ]
    if goal["reminders"]:
        offsets = ", ".join(offset_text(o) for o in goal["reminders"])
        lines.append(f"-# Reminds people who are short {offsets} before each cycle ends.")
    else:
        lines.append("-# No reminders.")
    if goal["group_config"]:
        cfg = GroupConfig.from_dict(goal["group_config"])
        lines.append(f"-# Makes {cfg.summary()} each cycle. {rules_text(cfg.count_solo, cfg.require_all)}")
    return "\n".join(lines)


async def goal_progress(db, goal: dict, cycle: int, members: list[int]) -> list[tuple[int, int]]:
    """(Discord ID, minutes thrown in that cycle) for these people, fewest minutes first. Every
    session a person took part in counts, whatever group it was or wasn't with. Any cycle can be
    asked for, including ones from before the goal was created: it's all read from the sessions."""
    if not members:
        return []
    start, end = goals.cycle_bounds(goal, cycle, TZ)
    thrown = {r["discord_id"]: r["minutes"] for r in await db.throwing_totals(start, end, members, limit=len(members))}
    return sorted(((i, thrown.get(i, 0)) for i in members), key=lambda pair: pair[1])


class OpenFormView(discord.ui.View):
    """One button that opens a form. A form can't open another form, so this goes in between."""

    def __init__(self, label: str, make_modal):
        super().__init__(timeout=PREVIEW_SECONDS)
        self.make_modal = make_modal
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        button.callback = self._open
        self.add_item(button)

    async def _open(self, interaction: discord.Interaction):
        await interaction.response.send_modal(self.make_modal())


class GoalBasicsModal(discord.ui.Modal, title="New goal (1 of 2): who and how much"):
    """A goal takes more than the five fields one form holds, so it's two forms with a button between."""

    def __init__(self, db, channel, typed: dict[str, str] | None = None):
        super().__init__()
        self.db = db
        self.default_channel = channel
        typed = typed or {}
        self.name = discord.ui.TextInput(default=typed.get("name"), placeholder="e.g. Weekly throwing", max_length=50)
        self.minutes = discord.ui.TextInput(default=typed.get("minutes"), placeholder="e.g. 100", max_length=5)
        self.pool = PoolFields("the goal applies to")
        self.channel = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], min_values=1, max_values=1,
            default_values=[channel] if isinstance(channel, discord.TextChannel) else [],
        )
        self.add_item(discord.ui.Label(text="Name", component=self.name))
        self.add_item(discord.ui.Label(
            text="Minutes", description="How many minutes each person should throw per cycle.", component=self.minutes
        ))
        self.pool.add_to(self)
        self.add_item(discord.ui.Label(
            text="Channel", description="Where reminders, and new groups, are posted.", component=self.channel
        ))

    async def on_submit(self, interaction: discord.Interaction):
        typed = {"name": self.name.value.strip(), "minutes": self.minutes.value.strip()}
        targets = self.pool.targets()
        channel = interaction.guild.get_channel(self.channel.values[0].id)
        try:
            if not typed["name"]:
                raise InputError("The name is empty.")
            if not typed["minutes"].isdigit() or not 1 <= int(typed["minutes"]) <= 10000:
                raise InputError("Minutes has to be a number from 1 to 10000.")
            perms = channel and channel.permissions_for(interaction.guild.me)
            if not (perms and perms.view_channel and perms.send_messages):
                raise InputError(f"I can't post in <#{self.channel.values[0].id}>. Give me **View Channel** and **Send Messages** there.")
            if not await resolve_pool(interaction.guild, targets):
                raise InputError("Nobody is in that pool.")
        except InputError as e:
            # The pool and exclusions have to be picked again; what was typed is kept.
            await interaction.response.send_message(
                f"Not saved: {e}",
                view=RetryView(lambda: GoalBasicsModal(self.db, channel or self.default_channel, typed)),
                ephemeral=True,
            )
            return
        details = dict(name=typed["name"], minutes=int(typed["minutes"]), channel_id=channel.id, targets=targets)
        await interaction.response.send_message(
            f"**{details['name']}**: {details['minutes']} min each, for {pool_text(targets)}, in {channel.mention}.\n"
            "Nothing is saved yet. Next, when it repeats.",
            view=OpenFormView("Next: schedule and groups", lambda: GoalScheduleModal(self.db, details)),
            ephemeral=True, allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Error in the create-goal form", exc_info=error)
        await modal_failed(interaction, "Something went wrong creating the goal.")


class GoalScheduleModal(discord.ui.Modal, title="New goal (2 of 2): schedule and groups"):
    def __init__(self, db, details: dict, typed: dict[str, str] | None = None):
        super().__init__()
        self.db = db
        self.details = details  # from the first form
        today = datetime.now(TZ).date()
        typed = typed or {"start": f"{today - timedelta(days=today.weekday())} 00:00", "every": "1 week", "reminders": "", "size": ""}
        self.start = discord.ui.TextInput(default=typed["start"], max_length=30)
        self.every = discord.ui.TextInput(default=typed["every"], max_length=20)
        self.reminders = discord.ui.TextInput(default=typed["reminders"] or None, required=False, max_length=60)
        self.config = GroupConfigFields(typed["size"], optional=True)
        self.add_item(discord.ui.Label(
            text="First cycle starts",
            description="Date and time, like 2026-10-04 20:00 or 10/4 8pm. A past date counts the cycles since then too.",
            component=self.start,
        ))
        self.add_item(discord.ui.Label(
            text="Repeats every", description="A number of hours, days, weeks or months: 1 week, 3 days, 12 hours.",
            component=self.every,
        ))
        self.add_item(discord.ui.Label(
            text="Reminders",
            description="How long before each cycle ends to ping people who are short: 2d, 6h. Blank for none.",
            component=self.reminders,
        ))
        self.config.add_to(self)

    async def on_submit(self, interaction: discord.Interaction):
        typed = {
            "start": self.start.value.strip(), "every": self.every.value.strip(),
            "reminders": self.reminders.value.strip(), "size": self.config.typed_size,
        }
        try:
            count, unit = parse_every(typed["every"])
            schedule = dict(
                starts_at=parse_moment(typed["start"]).isoformat(timespec="minutes"),
                every_count=count, every_unit=unit, reminders=parse_reminders(typed["reminders"]),
            )
            for offset in schedule["reminders"]:
                if not goals.reminder_times({**schedule, "reminders": [offset]}, 0, TZ):
                    raise InputError(f"A reminder {offset_text(offset)} before the end doesn't fit in a cycle of {every_text(schedule)}.")
            cfg = self.config.read()
        except InputError as e:
            await interaction.response.send_message(
                f"Not created: {e}",
                view=RetryView(lambda: GoalScheduleModal(self.db, self.details, typed)),
                ephemeral=True,
            )
            return
        goal_id = await self.db.create_goal(
            **self.details, **schedule, group_config=cfg and asdict(cfg), created_by=interaction.user.id
        )
        log.info("%s created throwing goal %s (%s)", interaction.user, goal_id, self.details["name"])
        goal = await self.db.get_goal(goal_id)
        reply = "Goal created.\n" + describe_goal(goal)
        if cycle := goals.current_cycle(goal, datetime.now(TZ), TZ):
            reply += f"\nIt started {cycle} cycle(s) ago. `/throwing-mgr goal history` shows how those went."
        if cfg:
            reply += "\nThis cycle's groups will be posted in its channel within a minute."
        await interaction.response.send_message(reply, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Error in the create-goal schedule form", exc_info=error)
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
            label = f"#{g['id']} {g['name']} · {g['minutes']} min every {every_text(g)}"
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

    @goal.command(name="create", description="Set a minutes goal that repeats. Opens a form.")
    async def goal_create(self, interaction: discord.Interaction):
        await interaction.response.send_modal(GoalBasicsModal(self.db, interaction.channel))

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

    @goal.command(name="progress", description="Who has and hasn't reached a goal, this cycle or an earlier one.")
    @app_commands.describe(cycles_ago="0 is the current cycle (the default), 1 the one before, and so on")
    @app_commands.autocomplete(goal=_goal_choices)
    async def goal_progress_(
        self, interaction: discord.Interaction, goal: int, cycles_ago: app_commands.Range[int, 0, 10000] = 0
    ):
        if not (found := await self._find_goal(interaction, goal)):
            return
        current = goals.current_cycle(found, datetime.now(TZ), TZ)
        if current is None:
            await interaction.response.send_message("That goal hasn't started yet.\n" + describe_goal(found), ephemeral=True)
            return
        if cycles_ago > current:
            await interaction.response.send_message(
                f"That goal only goes back {current} cycle(s): it started {moment_text(goals.cycle_start(found, 0, TZ))}.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        cycle = current - cycles_ago
        progress = await goal_progress(self.db, found, cycle, await resolve_pool(interaction.guild, found["targets"]))
        reached = sum(minutes >= found["minutes"] for _, minutes in progress)
        lines = [
            f"**{found['name']}**, {life_text(*goals.cycle_bounds(found, cycle, TZ))}"
            f"{'' if cycles_ago else ' (still running)'}: {reached} of {len(progress)} at {found['minutes']} min",
            *(f"{'✅' if minutes >= found['minutes'] else '▫️'} <@{i}> {minutes}" for i, minutes in progress[::-1]),
        ]
        if cycles_ago:
            lines.append("-# For the people the goal applies to today, whoever it applied to then.")
        for part in chunks(lines):
            await interaction.followup.send(part, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @goal.command(name="history", description="How a goal went over its recent cycles, including any before it was created.")
    @app_commands.describe(cycles="How many cycles back to show, counting the current one. Default 8")
    @app_commands.autocomplete(goal=_goal_choices)
    async def goal_history(self, interaction: discord.Interaction, goal: int, cycles: app_commands.Range[int, 1, 26] = 8):
        if not (found := await self._find_goal(interaction, goal)):
            return
        current = goals.current_cycle(found, datetime.now(TZ), TZ)
        if current is None:
            await interaction.response.send_message("That goal hasn't started yet.\n" + describe_goal(found), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        members = await resolve_pool(interaction.guild, found["targets"])
        shown = range(max(0, current - cycles + 1), current + 1)  # oldest first
        thrown = {i: [] for i in members}  # each person's minutes, one per cycle shown
        lines = [f"**{found['name']}**: {found['minutes']} min every {every_text(found)}"]
        for cycle in shown:
            progress = await goal_progress(self.db, found, cycle, members)
            for i, minutes in progress:
                thrown[i].append(minutes)
            reached = sum(minutes >= found["minutes"] for _, minutes in progress)
            lines.append(
                f"{life_text(*goals.cycle_bounds(found, cycle, TZ))}: {reached} of {len(progress)} reached it"
                + (" (still running)" if cycle == current else "")
            )
        lines.append("**Cycles reached per person** (minutes each cycle, oldest first)")
        hits = lambda minutes: sum(m >= found["minutes"] for m in minutes)
        for i, minutes in sorted(thrown.items(), key=lambda pair: (-hits(pair[1]), -sum(pair[1]))):
            lines.append(f"<@{i}> {hits(minutes)} of {len(shown)} · {', '.join(map(str, minutes))}")
        lines.append("-# For the people the goal applies to today, whoever it applied to then.")
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
        members = await resolve_pool(channel.guild, goal["targets"])
        short = [(i, m) for i, m in await goal_progress(self.db, goal, cycle, members) if m < goal["minutes"]]
        if not short:
            return "Nobody to remind: everyone has reached it."
        end = goals.cycle_start(goal, cycle + 1, TZ)
        if (end.hour, end.minute) == (0, 0):
            final = end.date() - timedelta(days=1)
            by = f"the end of {final:%A}, {final:%b} {final.day}"
        else:
            by = moment_text(end)
        lines = [
            f"**{goal['name']}: {goal['minutes']} min by {by}.** Not there yet:",
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
            if now.astimezone(timezone.utc) - due.astimezone(timezone.utc) > REMINDER_GRACE:
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
