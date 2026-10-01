"""Logs throwing sessions reported in a channel, using Claude to read the conversation.

The bot keeps the last hour of THROWING_CHANNEL_ID in memory as a transcript, including its own
replies and any private back-and-forth, with each message marked if it's been logged. Shortly after
people stop typing, Claude reads the transcript and returns reports that haven't been logged yet.
Complete ones are logged; for incomplete ones the reporter is asked privately (DM, or a reply in the
channel if their DMs are closed), and their answer goes into the transcript for the next pass.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from .. import config
from ..member_tools import find_members_tool
from ..session_parser import FoundReport, ParseFailed, SessionParser

log = logging.getLogger(__name__)

TRANSCRIPT_TTL = timedelta(hours=1)
MAX_TRANSCRIPT_MESSAGES = 300
QUIET_SECONDS = 8  # wait for a pause in the conversation before reading it
MAX_QUESTIONS = 3
MAX_MINUTES = 24 * 60
# Statuses of messages that are done with. "handled" = it @mentioned the bot, so the assistant
# (cogs/assistant.py) answered it, including any manual logging.
CLOSED = ("logged", "gave up", "dropped", "handled")


@dataclass
class Entry:
    at: datetime
    author_id: int
    author_name: str
    content: str
    message: discord.Message | None = None  # None for private (DM) lines
    reply_to: int | None = None
    is_bot: bool = False
    status: str | None = None  # "asked: ..." or one of CLOSED

    @property
    def id(self) -> int | None:
        return self.message.id if self.message else None


@dataclass
class Asked:
    """A report we've asked its author about."""

    reporter: discord.abc.User
    questions: int = 0
    awaiting_answer: bool = False
    via_dm: bool = True


class Throwing(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.parser = SessionParser(config.CLAUDE_MODEL)
        self.tz = ZoneInfo(config.TIMEZONE)
        self.transcript: list[Entry] = []
        self.asked: dict[int, Asked] = {}  # report's first message ID -> question state
        self.lock = asyncio.Lock()
        self.pending_analysis: asyncio.Task | None = None
        self.backfilled = False

    @property
    def db(self):
        return self.bot.db

    # ---- keeping the transcript ----

    def _prune(self):
        cutoff = datetime.now(timezone.utc) - TRANSCRIPT_TTL
        self.transcript = [e for e in self.transcript if e.at >= cutoff][-MAX_TRANSCRIPT_MESSAGES:]
        live = {e.id for e in self.transcript}
        self.asked = {k: v for k, v in self.asked.items() if k in live}

    def _entry(self, message_id: int) -> Entry | None:
        return next((e for e in self.transcript if e.id == message_id), None)

    def _add_message(self, message: discord.Message, status: str | None = None):
        if self._entry(message.id):
            return
        ref = message.reference
        self.transcript.append(
            Entry(
                at=message.created_at,
                author_id=message.author.id,
                author_name=message.author.display_name,
                content=message.content or "(attachment)",
                message=message,
                reply_to=ref.message_id if ref else None,
                is_bot=message.author == self.bot.user,
                status=status,
            )
        )
        self.transcript.sort(key=lambda e: e.at)

    def _add_private(self, author: discord.abc.User, text: str):
        self.transcript.append(
            Entry(at=datetime.now(timezone.utc), author_id=author.id, author_name=author.display_name, content=text)
        )

    @commands.Cog.listener()
    async def on_ready(self):
        if self.backfilled:
            return
        self.backfilled = True
        channel = self.bot.get_channel(config.THROWING_CHANNEL_ID)
        if channel is None:
            log.warning("Throwing channel %s not found or not visible to the bot", config.THROWING_CHANNEL_ID)
            return
        perms = channel.permissions_for(channel.guild.me)
        needed = ["view_channel", "read_message_history", "send_messages", "add_reactions"]
        if missing := [p for p in needed if not getattr(perms, p)]:
            log.error(
                "Missing permissions in #%s: %s. Add the bot's role to the channel (Edit Channel -> "
                "Permissions) and allow them.",
                channel.name,
                ", ".join(missing),
            )
            if "view_channel" in missing or "read_message_history" in missing:
                return
        after = datetime.now(timezone.utc) - TRANSCRIPT_TTL
        count = 0
        try:
            async for message in channel.history(after=after, oldest_first=True, limit=MAX_TRANSCRIPT_MESSAGES):
                if message.author.bot and message.author != self.bot.user:
                    continue
                logged = await self.db.session_for_message(message.id)
                self._add_message(message, status="logged" if logged else None)
                count += 1
        except discord.Forbidden as e:
            log.error("Couldn't read #%s history: %s", channel.name, e)
            return
        log.info("Loaded %d message(s) from the last hour of the throwing channel", count)
        if count:
            self._schedule_analysis()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot and message.author != self.bot.user:
            return
        if message.guild is None:
            if message.author != self.bot.user:
                await self._handle_dm(message)
            return
        if message.channel.id != config.THROWING_CHANNEL_ID:
            return

        self._add_message(message, status="handled" if self.bot.user in message.mentions else None)
        if message.author == self.bot.user:
            return  # our own confirmations/questions are context, not a reason to re-read
        # If we asked in the channel (their DMs are closed), anything they say there may be the answer.
        # If we asked by DM, only a DM reply counts; otherwise ordinary chatter from the reporter could
        # make a still-open report look cancelled. (An answer given in the channel anyway is still in
        # the transcript, so the report gets logged once it's complete.)
        for asked in self.asked.values():
            if asked.reporter.id == message.author.id and not asked.via_dm:
                asked.awaiting_answer = False
        self._schedule_analysis()

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        entry = self._entry(payload.message_id)
        if entry and not entry.is_bot and "content" in payload.data:
            entry.content = payload.data["content"] or "(attachment)"
            self._schedule_analysis()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        self.transcript = [e for e in self.transcript if e.id != payload.message_id]

    async def _handle_dm(self, message: discord.Message):
        waiting = [a for a in self.asked.values() if a.reporter.id == message.author.id and a.via_dm]
        if not waiting:
            return
        for asked in waiting:
            asked.awaiting_answer = False
        self._add_private(message.author, f"[private] {message.author.display_name} replied to the bot: {message.content}")
        self._schedule_analysis()

    # ---- reading the transcript ----

    def _schedule_analysis(self):
        """Read the transcript once the conversation has been quiet for a few seconds."""
        if self.pending_analysis and not self.pending_analysis.done():
            self.pending_analysis.cancel()
        self.pending_analysis = asyncio.create_task(self._analyze_after_pause())

    async def _analyze_after_pause(self):
        await asyncio.sleep(QUIET_SECONDS)
        async with self.lock:
            try:
                await self._analyze()
            except Exception:
                log.exception("Error analyzing the throwing channel")

    async def _analyze(self):
        self._prune()
        if not any(not e.is_bot for e in self.transcript):
            return
        channel = self.bot.get_channel(config.THROWING_CHANNEL_ID)
        if not channel.guild.chunked:
            await channel.guild.chunk()
        try:
            reports = await self.parser.find_reports(self._prompt(), [find_members_tool(channel.guild, self.db)])
        except ParseFailed as e:
            log.error("Couldn't read the throwing channel: %s", e)
            return

        log.info("Claude found %d report(s) in %d transcript line(s)", len(reports), len(self.transcript))
        for n, report in enumerate(reports, 1):
            for message_id in report.message_ids:
                entry = self._entry(message_id)
                if entry:
                    log.info("  report %d: msg %s from %s (id=%s): %r", n, message_id, entry.author_name, entry.author_id, entry.content[:100])
                else:
                    log.info("  report %d: msg %s (not in transcript)", n, message_id)
            log.info(
                "  report %d: minutes=%s participants=%s description=%r question=%r",
                n, report.minutes, report.participant_ids, report.description, report.question,
            )

        found = set()
        for report in reports:
            if key := await self._handle_report(report, channel.guild):
                found.add(key)

        # A report we asked about that's no longer being returned, after the reporter replied,
        # was cancelled or turned out not to be a session.
        for key, asked in list(self.asked.items()):
            if key not in found and not asked.awaiting_answer:
                del self.asked[key]
                if entry := self._entry(key):
                    entry.status = "dropped"
                if asked.via_dm:
                    await self._dm(asked.reporter, "OK, I won't log that one.")

    async def _handle_report(self, report: FoundReport, guild: discord.Guild) -> int | None:
        """Log or ask about one report. Returns the report's key (first message ID) if it's still open."""
        # Only unlogged human channel messages can make up a report.
        entries = [
            e for i in dict.fromkeys(report.message_ids)
            if (e := self._entry(i)) and not e.is_bot and e.status not in CLOSED
        ]
        if not entries:
            return None
        first = min(entries, key=lambda e: e.at)
        if await self.db.session_for_message(first.id):
            first.status = "logged"
            return None

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
            return first.id
        await self._log(first, entries, report, participants)
        return None

    async def _log(self, first: Entry, entries: list[Entry], report: FoundReport, participants: list[int]):
        message = first.message
        when = self._occurred_at(report.occurred_at, message.created_at)
        session_id = await self.db.log_session(
            occurred_at=when,
            minutes=report.minutes,
            description=report.description,
            participant_ids=participants,
            reported_by=first.author_id,
            source_message_id=first.id,
        )
        for e in entries:
            e.status = "logged"
        log.info("Logged session %s: %d min, %d people, from %s", session_id, report.minutes, len(participants), message.jump_url)

        summary = f"Logged **{report.minutes} min** for {', '.join(f'<@{i}>' for i in participants)}"
        if report.description:
            summary += f": {report.description}"
        local = when.astimezone(self.tz)
        if local.date() != message.created_at.astimezone(self.tz).date():
            summary += f" ({local:%a %b} {local.day})"
        # The ✅ is the confirmation. Only reply when Claude has something to answer.
        await self._react(message, "✅")
        if report.reply:
            try:
                await message.reply(
                    report.reply[:2000], mention_author=False, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.HTTPException as e:
                log.warning("Couldn't reply to %s: %s", message.jump_url, e)

        asked = self.asked.pop(first.id, None)
        if asked and asked.via_dm:
            await self._dm(asked.reporter, f"Thanks! {summary}.")

    async def _ask(self, first: Entry, question: str):
        message = first.message
        asked = self.asked.setdefault(first.id, Asked(reporter=message.author))
        if asked.awaiting_answer:
            return  # already asked; wait for them
        if asked.questions >= MAX_QUESTIONS:
            first.status = "gave up"
            del self.asked[first.id]
            text = (
                "I still couldn't work out that throwing session, so I didn't log it. Try posting it "
                "again with @mentions and the number of minutes."
            )
            if asked.via_dm:
                await self._dm(asked.reporter, text)
            else:
                await message.reply(text, mention_author=False)
            return

        asked.questions += 1
        asked.awaiting_answer = True
        first.status = f"asked: {question}"
        try:
            await message.author.send(
                f"Quick question about your throwing report {message.jump_url}\n> {question}\n\n"
                'Just reply here, or say "cancel" to skip it.'
            )
            asked.via_dm = True
            self._add_private(
                self.bot.user, f"[private] void-bot asked {message.author.display_name} about msg {first.id}: {question}"
            )
        except discord.Forbidden:
            # DMs closed: ask in the channel. Their answer will show up in the transcript.
            asked.via_dm = False
            await message.reply(f"{question}\n-# Answer here, or say \"cancel\".")

    # ---- helpers ----

    async def _dm(self, user: discord.abc.User, text: str):
        try:
            await user.send(text)
        except discord.HTTPException as e:
            log.warning("Couldn't DM %s: %s", user, e)

    @staticmethod
    async def _react(message: discord.Message, emoji: str):
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException as e:
            log.warning("Couldn't react to %s: %s", message.jump_url, e)

    def _prompt(self) -> str:
        now = datetime.now(timezone.utc).astimezone(self.tz)
        lines = []
        for e in self.transcript:
            at = e.at.astimezone(self.tz)
            stamp = f"{at:%a %H:%M}"
            if e.message is None:
                lines.append(f"{stamp} {e.content}")
                continue
            who = "void-bot (the bot)" if e.is_bot else f"{e.author_name} (id={e.author_id})"
            reply = f", replying to msg {e.reply_to}" if e.reply_to else ""
            lines.append(f"[msg {e.id}] {stamp} {who}{reply}: {e.content}")
            if e.status:
                lines.append(f"    -> {e.status}")
        return (
            f"Now: {now.isoformat()} ({now:%A}, time zone {config.TIMEZONE})\n\n"
            "Transcript (last hour, oldest first):\n<transcript>\n" + "\n".join(lines) + "\n</transcript>"
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
    log.info("Logging throwing sessions from channel %s with %s", config.THROWING_CHANNEL_ID, config.CLAUDE_MODEL)
