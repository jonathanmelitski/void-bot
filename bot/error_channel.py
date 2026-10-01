"""Posts everything the bot logs at ERROR or above to a Discord channel (ERRORS_CHANNEL_ID).

It's a handler on the root logger, so it covers every cog and discord.py itself without any of them
knowing about it. The normal console logging is unchanged.
"""

import asyncio
import logging
import sys

import discord

from . import config

MAX_QUEUED = 50  # during an error storm, drop the rest rather than flood the channel
SECONDS_BETWEEN_POSTS = 1
MAX_LENGTH = 2000 - len("```\n\n```")  # Discord's message limit, minus the code fence


def _fit(text: str) -> str:
    """Trim to one message, keeping the first line and the end of the traceback."""
    text = text.replace("```", "'''")
    if len(text) <= MAX_LENGTH:
        return text
    first = text.split("\n", 1)[0][:300] + "\n…\n"
    return first + text[-(MAX_LENGTH - len(first)):]


class ErrorChannelHandler(logging.Handler):
    def __init__(self, bot: discord.Client):
        super().__init__(level=logging.ERROR)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        self.bot = bot
        self.loop = asyncio.get_running_loop()
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=MAX_QUEUED)
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
            pass

    async def _post_queued(self):
        await self.bot.wait_until_ready()
        while True:
            text = await self.queue.get()
            try:
                channel = self.bot.get_channel(config.ERRORS_CHANNEL_ID) or await self.bot.fetch_channel(
                    config.ERRORS_CHANNEL_ID
                )
                await channel.send(f"```\n{_fit(text)}\n```", allowed_mentions=discord.AllowedMentions.none())
            except Exception as e:
                # Never log from here: an error about posting errors would loop straight back in.
                print(f"Couldn't post an error to channel {config.ERRORS_CHANNEL_ID}: {e!r}", file=sys.stderr)
            await asyncio.sleep(SECONDS_BETWEEN_POSTS)


def install(bot: discord.Client) -> ErrorChannelHandler:
    """Start posting errors. Call from inside the running event loop."""
    handler = ErrorChannelHandler(bot)
    logging.getLogger().addHandler(handler)
    return handler
