"""Posts what the bot logs to Discord channels: errors to ERRORS_CHANNEL_ID, and everything that
reaches the console to LOGS_CHANNEL_ID.

Each is a handler on the root logger, so it covers every cog and discord.py itself without any of
them knowing about it. The normal console logging is unchanged.
"""

import asyncio
import logging
import sys

import discord

from . import config

MAX_QUEUED = 50  # during an error storm, drop the rest rather than flood the channel
MAX_QUEUED_LOG_LINES = 1000
SECONDS_BETWEEN_POSTS = 1
# The console's format (discord.utils.setup_logging), so the channel reads like `docker compose logs`.
CONSOLE_FORMAT = logging.Formatter("[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{")
MAX_LENGTH = 2000 - len("```\n\n```")  # Discord's message limit, minus the code fence


def _fit(text: str) -> str:
    """Trim to one message, keeping the first line and the end of the traceback."""
    text = text.replace("```", "'''")
    if len(text) <= MAX_LENGTH:
        return text
    first = text.split("\n", 1)[0][:300] + "\n…\n"
    return first + text[-(MAX_LENGTH - len(first)):]


class ChannelHandler(logging.Handler):
    """Posts log records to one channel, at most a message a second. With batch, the lines waiting
    are put in one message where they fit, which is what keeps up with info-level logging."""

    def __init__(
        self, bot: discord.Client, channel_id: int, *, level: int, formatter: logging.Formatter,
        max_queued: int, batch: bool,
    ):
        super().__init__(level=level)
        self.setFormatter(formatter)
        self.bot = bot
        self.channel_id = channel_id
        self.batch = batch
        self.dropped = 0  # lines lost to a full queue since the last post
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queued)
        self.task = self.loop.create_task(self._post_queued())

    def emit(self, record: logging.LogRecord):
        # Only queue here; logging can be called from anywhere and must not block or do I/O.
        try:
            self.loop.call_soon_threadsafe(self._enqueue, self.format(record))
        except Exception:
            self.handleError(record)

    def _enqueue(self, text: str):
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.dropped += 1

    def _next_message(self, first: str) -> str:
        """The next message to post: one record, or with batch as many waiting ones as fit."""
        text = _fit(first)
        if self.batch:
            if self.dropped:
                text = f"… {self.dropped} line(s) dropped: logging faster than I can post\n{text}"[:MAX_LENGTH]
                self.dropped = 0
            while not self.queue.empty() and len(text) + 1 + len(self.queue._queue[0]) <= MAX_LENGTH:
                text += "\n" + _fit(self.queue.get_nowait())
        return text

    async def _post_queued(self):
        await self.bot.wait_until_ready()
        while True:
            text = self._next_message(await self.queue.get())
            try:
                channel = self.bot.get_channel(self.channel_id) or await self.bot.fetch_channel(self.channel_id)
                await channel.send(f"```\n{text}\n```", allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:
                # Never log from here: a log line about posting log lines would loop straight back in.
                print(f"Couldn't post to log channel {self.channel_id}: {e!r}", file=sys.stderr)
            await asyncio.sleep(SECONDS_BETWEEN_POSTS)


def install(bot: discord.Client) -> list[ChannelHandler]:
    """Start posting to whichever of the two channels are set. Call from inside the running event loop."""
    handlers = []
    if config.ERRORS_CHANNEL_ID:
        handlers.append(ChannelHandler(
            bot, config.ERRORS_CHANNEL_ID, level=logging.ERROR, max_queued=MAX_QUEUED, batch=False,
            formatter=logging.Formatter("%(levelname)s %(name)s: %(message)s"),
        ))
    if config.LOGS_CHANNEL_ID:
        # NOTSET: everything the root logger lets through, which is exactly what the console gets.
        handlers.append(ChannelHandler(
            bot, config.LOGS_CHANNEL_ID, level=logging.NOTSET, max_queued=MAX_QUEUED_LOG_LINES, batch=True,
            formatter=CONSOLE_FORMAT,
        ))
    for handler in handlers:
        logging.getLogger().addHandler(handler)
    return handlers
