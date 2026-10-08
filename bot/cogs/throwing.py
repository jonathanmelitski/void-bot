"""Logs throwing sessions reported in a channel, but only when someone tags the bot there.

Nothing is read or kept between tags. When the bot is @mentioned in THROWING_CHANNEL_ID it fetches
the last hour of that channel, and Claude picks out the reports that haven't been logged. Complete
ones are logged and get a reaction. For an incomplete one the bot starts a thread on the report and
asks there; messages in those threads are read as they arrive, and once the report is complete it's
logged and the thread is deleted. The bot never posts in the channel itself and never
DMs anyone.

A report that names nobody else is a solo session. A name the bot can't place gets at most
MAX_NAME_QUESTIONS questions, then the session is logged without that person. A report that looks
like a session someone else already logged isn't logged twice: the bot asks whether it's the same
one, and if so adds the reporter's people to it.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from .. import config
from ..member_tools import find_members_tool
from ..session_parser import FoundReport, ParseFailed, SessionParser

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=1)
MAX_MESSAGES = 200
MAX_THREAD_MESSAGES = 50
MAX_QUESTIONS = 3
MAX_NAME_QUESTIONS = 2  # after this many, a person we can't identify is left out of the session
# A report within this long of a logged session with the same people might be that session.
SAME_SESSION_WINDOW = timedelta(hours=12)
# Logged sessions shown to Claude: from this long before the oldest message being read.
SESSION_LOOKBACK = timedelta(hours=36)
MAX_SESSIONS_SHOWN = 100
MAX_SESSIONS_ASKED = 3
MAX_MINUTES = 24 * 60
# Sessions are logged as the nearest of these, one per void_throw_<minutes> emote. Keep in step with
# MINUTES in scripts/make_emotes.py.
LOGGED_MINUTES = [*range(5, 61, 5), *range(70, 121, 10), *range(135, 181, 15)]
TEXT_TYPES = (discord.MessageType.default, discord.MessageType.reply)
NEEDED_PERMISSIONS = [
    "view_channel", "read_message_history", "add_reactions", "create_public_threads", "send_messages_in_threads",
    "manage_threads",  # to delete a question thread once its report is logged
]


@dataclass
class Item:
    """A message in the throwing channel, with the bot's question thread about it if there is one."""

    message: discord.Message
    logged: bool = False
    session_id: str | None = None  # the session it was logged as or added to
    thread: discord.Thread | None = None
    replies: list[discord.Message] = field(default_factory=list)  # the thread's messages


class Throwing(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.parser = SessionParser(config.CLAUDE_MODEL)
        self.tz = ZoneInfo(config.TIMEZONE)
        self.lock = asyncio.Lock()
        self.app_emotes: dict[str, discord.Emoji] | None = None  # by name; loaded on first use

    @property
    def db(self):
        return self.bot.db

    @commands.Cog.listener()
    async def on_ready(self):
        channel = self.bot.get_channel(config.THROWING_CHANNEL_ID)
        if channel is None:
            log.warning("Throwing channel %s not found or not visible to the bot", config.THROWING_CHANNEL_ID)
            return
        perms = channel.permissions_for(channel.guild.me)
        if missing := [p for p in NEEDED_PERMISSIONS if not getattr(perms, p)]:
            log.error(
                "Missing permissions in #%s: %s. Add the bot's role to the channel (Edit Channel -> "
                "Permissions) and allow them.",
                channel.name,
                ", ".join(missing),
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # The only two things the bot acts on. Every other message is ignored without being read.
        if message.author.bot or message.guild is None:
            return
        channel = message.channel
        if channel.id == config.THROWING_CHANNEL_ID:
            if self.bot.user in message.mentions:
                await self._run(message, lambda: self._scan_channel(channel))
        elif (
            isinstance(channel, discord.Thread)
            and channel.parent_id == config.THROWING_CHANNEL_ID
            and channel.owner_id == self.bot.user.id
        ):
            # The database says which report each of the bot's threads is about.
            if report_id := await self.db.report_for_thread(channel.id):
                await self._run(message, lambda: self._scan_thread(report_id))

    async def _run(self, trigger: discord.Message, scan):
        async with self.lock:
            try:
                await scan()
            except ParseFailed as e:
                log.error("Couldn't read the throwing channel: %s", e)
                await self._react(trigger, "⚠️")
            except Exception:
                log.exception("Error handling %s", trigger.jump_url)
                await self._react(trigger, "⚠️")

    # ---- reading the channel ----

    async def _scan_channel(self, channel: discord.TextChannel):
        """Tagged in the channel: go through the last hour."""
        after = datetime.now(timezone.utc) - WINDOW
        messages = [m async for m in channel.history(after=after, limit=MAX_MESSAGES, oldest_first=False)]
        await self._process(channel.guild, messages[::-1])

    async def _scan_thread(self, report_id: int):
        """Someone wrote in one of the bot's question threads: go through that one report, however old it is."""
        channel = self.bot.get_channel(config.THROWING_CHANNEL_ID)
        try:
            report = await channel.fetch_message(report_id)
        except discord.NotFound:
            return
        await self._process(channel.guild, [report])

    async def _process(self, guild: discord.Guild, messages: list[discord.Message]):
        ids = [m.id for m in messages]
        logged = await self.db.sessions_for_messages(ids)
        threads = await self.db.report_threads(ids)
        items = {}
        for message in messages:
            if message.author.bot or message.type not in TEXT_TYPES:
                continue
            item = Item(message, logged=message.id in logged, session_id=logged.get(message.id))
            if not item.logged and message.id in threads:
                await self._load_thread(item, threads[message.id])
            items[message.id] = item
        if all(item.logged for item in items.values()):
            return

        if not guild.chunked:
            await guild.chunk()
        sessions = await self._nearby_sessions(items)
        reports = await self.parser.find_reports(
            self._prompt(items, sessions, guild), [find_members_tool(guild, self.db)]
        )
        log.info("Claude found %d report(s) in %d message(s)", len(reports), len(items))
        for report in reports:
            await self._handle_report(report, items, sessions, guild)

    async def _nearby_sessions(self, items: dict[int, Item]) -> dict[str, dict]:
        """Sessions logged around the time of these messages, oldest first, under the short labels
        (s1, s2, ...) Claude knows them by. The labels only mean something within one pass."""
        if not items:
            return {}
        sent = [item.message.created_at for item in items.values()]
        rows = await self.db.list_sessions(
            min(sent) - SESSION_LOOKBACK, max(sent) + SAME_SESSION_WINDOW, limit=MAX_SESSIONS_SHOWN
        )
        return {f"s{n}": row for n, row in enumerate(rows[::-1], start=1)}

    async def _load_thread(self, item: Item, thread_id: int):
        message = item.message
        try:
            thread = message.guild.get_thread(thread_id) or await message.guild.fetch_channel(thread_id)
            replies = [m async for m in thread.history(limit=MAX_THREAD_MESSAGES, oldest_first=False)]
        except discord.NotFound:
            await self.db.remove_report_thread(message.id)  # someone deleted it; ask again in a new one
            return
        except discord.HTTPException as e:
            log.warning("Couldn't read the thread about %s: %s", message.jump_url, e)
            return
        item.thread = thread
        item.replies = [
            m for m in replies[::-1]
            if m.type in TEXT_TYPES and (not m.author.bot or m.author == self.bot.user)
        ]

    async def _handle_report(
        self, report: FoundReport, items: dict[int, Item], sessions: dict[str, dict], guild: discord.Guild
    ):
        """Log one report, add it to a session that's already logged, or ask about it."""
        found = [item for i in dict.fromkeys(report.message_ids) if (item := items.get(i)) and not item.logged]
        if not found:
            return
        first = min(found, key=lambda item: item.message.created_at)
        if await self.db.sessions_for_messages([first.message.id]):
            first.logged = True
            return

        participants = [
            i for i in dict.fromkeys(report.participant_ids) if (m := guild.get_member(i)) and not m.bot
        ]
        if dropped := set(report.participant_ids) - set(participants):
            log.warning("Dropped participant IDs that aren't members of the server: %s", dropped)

        reporter = first.message.author.id
        asked = sum(m.author == self.bot.user for m in first.replies)
        awaiting = bool(first.replies) and first.replies[-1].author == self.bot.user
        has_minutes = bool(report.minutes) and 0 < report.minutes <= MAX_MINUTES
        existing = sessions.get(report.existing_session or "")  # the reporter said it's this one

        question = report.question
        if question and asked >= MAX_NAME_QUESTIONS and not awaiting and participants and (has_minutes or existing):
            # The minutes are settled, so only a name can still be open, and we've asked enough. They
            # may not be in the server at all: go ahead without them.
            log.info("Logging %s without the people it couldn't identify", first.message.jump_url)
            question = None
        if not question and not existing:
            # Claude says it's complete; double-check the essentials ourselves.
            if not has_minutes:
                question = "How many minutes did you throw for?"
            elif not participants:
                question = "Who was throwing?"
        if question:
            await self._ask(first, question)
            return

        if existing:
            await self._join(first, existing, participants)
            return
        when = self._occurred_at(report.occurred_at, first.message.created_at)
        if report.existing_session != "new":
            candidates = self._same_session_candidates(sessions, reporter, participants, when)
            if candidates and await self._ask(first, self._same_session_question(candidates)):
                return  # out of questions means it's logged as its own session
        await self._log(first, report, participants, when, sessions)

    def _same_session_candidates(
        self, sessions: dict[str, dict], reporter: int, participants: list[int], when: datetime
    ) -> list[dict]:
        """Logged sessions this report might be a second account of: around the same time, and either
        with someone the reporter says they threw with, or one somebody else logged the reporter in.
        The reporter's own earlier sessions don't count, so throwing twice in a day isn't questioned."""
        others = set(participants) - {reporter}
        candidates = []
        for session in sessions.values():
            if abs(datetime.fromisoformat(session["occurred_at"]) - when) > SAME_SESSION_WINDOW:
                continue
            there = set(session["participants"])
            if there & others or (reporter in there and session["reported_by"] != reporter):
                candidates.append(session)
        return candidates[-MAX_SESSIONS_ASKED:]

    def _same_session_question(self, candidates: list[dict]) -> str:
        def describe(session: dict) -> str:
            at = datetime.fromisoformat(session["occurred_at"]).astimezone(self.tz)
            who = ", ".join(f"<@{i}>" for i in session["participants"])
            return f"{who}, {session['minutes']} min, {at:%a} {at.hour % 12 or 12}:{at:%M %p}"

        if len(candidates) == 1:
            return f"Already logged: {describe(candidates[0])}. Is this the same session, or a separate one?"
        listed = "\n".join(f"• {describe(c)}" for c in candidates)
        return f"Already logged:\n{listed}\nIs this one of those sessions, or a separate one?"

    async def _log(
        self, item: Item, report: FoundReport, participants: list[int], when: datetime, sessions: dict[str, dict]
    ):
        message = item.message
        # Round to the nearest unit (halfway goes up): 42 is logged as 40, and anything past 180 as 180.
        minutes = min(LOGGED_MINUTES, key=lambda unit: (abs(unit - report.minutes), -unit))
        session_id = await self.db.log_session(
            occurred_at=when,
            minutes=minutes,
            description=report.description,
            participant_ids=participants,
            reported_by=message.author.id,
            source_message_id=message.id,
        )
        item.logged = True
        # So a later report in this same pass can be recognized as this session.
        sessions[f"s{len(sessions) + 1}"] = {
            "id": session_id, "occurred_at": when.isoformat(), "minutes": minutes,
            "participants": participants, "reported_by": message.author.id,
        }
        log.info(
            "Logged session %s: %d min (reported %d), %d people, from %s",
            session_id, minutes, report.minutes, len(participants), message.jump_url,
        )
        await self._confirm(message, minutes)
        await self._close_thread(item)

    async def _join(self, item: Item, session: dict, participants: list[int]):
        """The report is a session that's already logged: add its people to that one. The session
        keeps the minutes and time it was logged with."""
        message = item.message
        if not await self.db.join_session(session["id"], participants, message.id):
            log.warning("Session %s is gone; %s will be read again", session["id"], message.jump_url)
            return
        added = [i for i in participants if i not in session["participants"]]
        session["participants"] = [*session["participants"], *added]
        item.logged = True
        log.info("Added %d people to session %s, from %s", len(added), session["id"], message.jump_url)
        await self._confirm(message, session["minutes"])
        await self._close_thread(item)

    async def _close_thread(self, item: Item):
        if not item.thread:
            return
        message = item.message
        try:
            await item.thread.delete()
        except discord.HTTPException as e:
            log.warning("Couldn't delete the thread about %s (needs Manage Threads): %s", message.jump_url, e)
        await self.db.remove_report_thread(message.id)

    async def _ask(self, item: Item, question: str) -> bool:
        """Ask the reporter in a thread on their report. False if it has had all its questions
        already, so nothing was asked and no answer is coming."""
        message, thread = item.message, item.thread
        if thread is None:
            try:
                thread = await message.create_thread(
                    name=f"{message.author.display_name}'s throwing session"[:100], auto_archive_duration=60
                )
            except discord.HTTPException as e:  # e.g. someone already started their own thread on it
                log.warning("Couldn't start a thread on %s: %s", message.jump_url, e)
                return True
            await self.db.add_report_thread(message.id, thread.id)
            item.thread = thread
        elif item.replies and item.replies[-1].author == self.bot.user:
            return True  # already asked; wait for them
        elif sum(m.author == self.bot.user for m in item.replies) >= MAX_QUESTIONS:
            log.info("Giving up on %s after %d questions", message.jump_url, MAX_QUESTIONS)
            return False

        try:
            # Mentioning the reporter adds them to the thread; silent, so it doesn't notify them.
            sent = await thread.send(
                f"<@{message.author.id}> {question}"[:2000],
                silent=True,
                allowed_mentions=discord.AllowedMentions(users=[message.author]),
            )
        except discord.HTTPException as e:
            log.warning("Couldn't ask in the thread about %s: %s", message.jump_url, e)
            return True
        item.replies.append(sent)
        return True

    # ---- helpers ----

    async def _confirm(self, message: discord.Message, minutes: int):
        """The only confirmation: the void_throw_<minutes> emote (scripts/make_emotes.py), or a ✅ if
        it hasn't been uploaded."""
        emote = await self._emote(message.guild, f"void_throw_{minutes}")
        if emote:
            try:
                await message.add_reaction(emote)
                return
            except discord.HTTPException as e:
                log.warning("Couldn't react with :%s: on %s: %s", emote.name, message.jump_url, e)
        await self._react(message, "✅")

    async def _emote(self, guild: discord.Guild, name: str) -> discord.Emoji | None:
        """An emoji by name: one uploaded to the server, or one uploaded to the bot's application
        (Developer Portal -> Emojis). Application emoji are fetched once and kept until restart."""
        emote = discord.utils.get(guild.emojis, name=name)
        if emote and emote.is_usable():
            return emote
        if self.app_emotes is None:
            try:
                self.app_emotes = {e.name: e for e in await self.bot.fetch_application_emojis()}
            except discord.HTTPException as e:
                log.warning("Couldn't fetch the application's emoji: %s", e)
                return None
        return self.app_emotes.get(name)

    @staticmethod
    async def _react(message: discord.Message, emoji: str):
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException as e:
            log.warning("Couldn't react to %s: %s", message.jump_url, e)

    def _line(self, message: discord.Message) -> str:
        at = message.created_at.astimezone(self.tz)
        if message.author == self.bot.user:
            who = "void-bot (the bot)"
        else:
            who = f"{message.author.display_name} (id={message.author.id})"
        content = re.sub(rf"<@!?{self.bot.user.id}>", "@void-bot", message.content) or "(attachment)"
        return f"{at:%a %H:%M} {who}: {content}"

    def _prompt(self, items: dict[int, Item], sessions: dict[str, dict], guild: discord.Guild) -> str:
        now = datetime.now(timezone.utc).astimezone(self.tz)
        labels = {session["id"]: label for label, session in sessions.items()}

        def who(discord_id: int) -> str:
            member = guild.get_member(discord_id)
            return f"{member.display_name if member else 'unknown'} (id={discord_id})"

        logged = []
        for label, session in sessions.items():
            at = datetime.fromisoformat(session["occurred_at"]).astimezone(self.tz)
            logged.append(
                f"[{label}] {at:%a %H:%M}, {session['minutes']} min, "
                f"{', '.join(who(i) for i in session['participants'])}; reported by {who(session['reported_by'])}"
            )
        lines = []
        for item in items.values():
            ref = item.message.reference
            reply = f" (replying to msg {ref.message_id})" if ref and ref.message_id else ""
            lines.append(f"[msg {item.message.id}]{reply} {self._line(item.message)}")
            if item.logged:
                label = labels.get(item.session_id)
                lines.append(f"    -> logged as {label}" if label else "    -> logged")
            lines += [f"    thread> {self._line(m)}" for m in item.replies]
        return (
            f"Now: {now.isoformat()} ({now:%A}, time zone {config.TIMEZONE})\n\n"
            "Logged sessions (oldest first):\n<sessions>\n" + ("\n".join(logged) or "(none)") + "\n</sessions>\n\n"
            "Transcript (oldest first):\n<transcript>\n" + "\n".join(lines) + "\n</transcript>"
        )

    def _occurred_at(self, raw: str | None, sent: datetime) -> datetime:
        """When the session happened, in UTC. Falls back to the message's send time."""
        when = None
        if raw:
            try:
                when = datetime.fromisoformat(raw)
            except ValueError:
                log.warning("Ignoring unparseable occurred_at %r", raw)
        if when and when.tzinfo is None:
            when = when.replace(tzinfo=self.tz)
        if when and when > sent + timedelta(minutes=5):
            log.warning("Ignoring occurred_at in the future: %s", when)
            when = None
        return (when or sent).astimezone(timezone.utc)


async def setup(bot: commands.Bot):
    if not config.THROWING_CHANNEL_ID:
        log.info("Throwing-session logging off: THROWING_CHANNEL_ID not set")
        return
    if not config.ANTHROPIC_API_KEY_SET:
        log.warning("Throwing-session logging off: THROWING_CHANNEL_ID is set but ANTHROPIC_API_KEY isn't")
        return
    await bot.add_cog(Throwing(bot))
    log.info("Logging throwing sessions when tagged in channel %s, with %s", config.THROWING_CHANNEL_ID, config.CLAUDE_MODEL)
