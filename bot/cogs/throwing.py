"""Logs throwing sessions reported in a channel, but only when someone tags the bot there.

Nothing is read or kept between tags. When the bot is @mentioned in THROWING_CHANNEL_ID it fetches
the last hour of that channel, and Claude picks out the reports that haven't been logged. Complete
ones are logged and get a reaction. For an incomplete one the bot starts a thread on the report and
asks there; messages in those threads are read as they arrive, and once the report is complete it's
logged and the thread is deleted. The bot never posts in the channel itself and never
DMs anyone.
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
MAX_MINUTES = 24 * 60
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
    thread: discord.Thread | None = None
    replies: list[discord.Message] = field(default_factory=list)  # the thread's messages


class Throwing(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.parser = SessionParser(config.CLAUDE_MODEL)
        self.tz = ZoneInfo(config.TIMEZONE)
        self.lock = asyncio.Lock()

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
        logged = await self.db.logged_message_ids(ids)
        threads = await self.db.report_threads(ids)
        items = {}
        for message in messages:
            if message.author.bot or message.type not in TEXT_TYPES:
                continue
            item = Item(message, logged=message.id in logged)
            if not item.logged and message.id in threads:
                await self._load_thread(item, threads[message.id])
            items[message.id] = item
        if all(item.logged for item in items.values()):
            return

        if not guild.chunked:
            await guild.chunk()
        reports = await self.parser.find_reports(self._prompt(items), [find_members_tool(guild, self.db)])
        log.info("Claude found %d report(s) in %d message(s)", len(reports), len(items))
        for report in reports:
            await self._handle_report(report, items, guild)

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

    async def _handle_report(self, report: FoundReport, items: dict[int, Item], guild: discord.Guild):
        """Log one report, or ask about it."""
        found = [item for i in dict.fromkeys(report.message_ids) if (item := items.get(i)) and not item.logged]
        if not found:
            return
        first = min(found, key=lambda item: item.message.created_at)
        if await self.db.session_for_message(first.message.id):
            first.logged = True
            return

        participants = [
            i for i in dict.fromkeys(report.participant_ids) if (m := guild.get_member(i)) and not m.bot
        ]
        if dropped := set(report.participant_ids) - set(participants):
            log.warning("Dropped participant IDs that aren't members of the server: %s", dropped)

        question = report.question
        if not question:
            # Claude says it's complete; double-check the essentials ourselves.
            if not report.minutes or not 0 < report.minutes <= MAX_MINUTES:
                question = "How many minutes did you throw for?"
            elif not participants:
                question = "Who did you throw with?"

        if question:
            await self._ask(first, question)
        else:
            await self._log(first, report, participants)

    async def _log(self, item: Item, report: FoundReport, participants: list[int]):
        message = item.message
        session_id = await self.db.log_session(
            occurred_at=self._occurred_at(report.occurred_at, message.created_at),
            minutes=report.minutes,
            description=report.description,
            participant_ids=participants,
            reported_by=message.author.id,
            source_message_id=message.id,
        )
        item.logged = True
        log.info("Logged session %s: %d min, %d people, from %s", session_id, report.minutes, len(participants), message.jump_url)
        await self._confirm(message, report.minutes)
        if item.thread:
            try:
                await item.thread.delete()
            except discord.HTTPException as e:
                log.warning("Couldn't delete the thread about %s (needs Manage Threads): %s", message.jump_url, e)
            await self.db.remove_report_thread(message.id)

    async def _ask(self, item: Item, question: str):
        """Ask the reporter in a thread on their report."""
        message, thread = item.message, item.thread
        if thread is None:
            try:
                thread = await message.create_thread(
                    name=f"{message.author.display_name}'s throwing session"[:100], auto_archive_duration=60
                )
            except discord.HTTPException as e:  # e.g. someone already started their own thread on it
                log.warning("Couldn't start a thread on %s: %s", message.jump_url, e)
                return
            await self.db.add_report_thread(message.id, thread.id)
            item.thread = thread
        elif item.replies and item.replies[-1].author == self.bot.user:
            return  # already asked; wait for them
        elif sum(m.author == self.bot.user for m in item.replies) >= MAX_QUESTIONS:
            log.info("Giving up on %s after %d questions", message.jump_url, MAX_QUESTIONS)
            return

        try:
            # Mentioning the reporter adds them to the thread; silent, so it doesn't notify them.
            sent = await thread.send(
                f"<@{message.author.id}> {question}"[:2000],
                silent=True,
                allowed_mentions=discord.AllowedMentions(users=[message.author]),
            )
        except discord.HTTPException as e:
            log.warning("Couldn't ask in the thread about %s: %s", message.jump_url, e)
            return
        item.replies.append(sent)

    # ---- helpers ----

    async def _confirm(self, message: discord.Message, minutes: int):
        """The only confirmation: the server's void_throw_<minutes> emote (scripts/make_emotes.py) if it
        has one for exactly that many minutes, otherwise a ✅."""
        emote = discord.utils.get(message.guild.emojis, name=f"void_throw_{minutes}")
        if emote and emote.is_usable():
            try:
                await message.add_reaction(emote)
                return
            except discord.HTTPException as e:
                log.warning("Couldn't react with :%s: on %s: %s", emote.name, message.jump_url, e)
        await self._react(message, "✅")

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

    def _prompt(self, items: dict[int, Item]) -> str:
        now = datetime.now(timezone.utc).astimezone(self.tz)
        lines = []
        for item in items.values():
            ref = item.message.reference
            reply = f" (replying to msg {ref.message_id})" if ref and ref.message_id else ""
            lines.append(f"[msg {item.message.id}]{reply} {self._line(item.message)}")
            if item.logged:
                lines.append("    -> logged")
            lines += [f"    thread> {self._line(m)}" for m in item.replies]
        return (
            f"Now: {now.isoformat()} ({now:%A}, time zone {config.TIMEZONE})\n\n"
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
