import asyncio
import logging
import pkgutil
import signal

import discord
from discord import app_commands
from discord.ext import commands

from . import cogs, config
from .db import Database

log = logging.getLogger("void-bot")


class VoidBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Needed to see who has a role (for /player import). Must also be enabled in the
        # Developer Portal: Bot -> Privileged Gateway Intents -> Server Members Intent.
        intents.members = True
        # Needed to read throwing reports when the bot is tagged. Also a privileged intent: enable
        # Bot -> Privileged Gateway Intents -> Message Content Intent.
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = Database(config.DB_PATH)

    async def setup_hook(self):
        await self.db.connect()
        log.info("Opened database at %s", config.DB_PATH)
        log.info("Admin roles: %s", sorted(config.ADMIN_ROLE_IDS) or "none (server admins only)")

        # Load every module in bot/cogs as an extension.
        for module in pkgutil.iter_modules(cogs.__path__):
            await self.load_extension(f"{cogs.__name__}.{module.name}")
            log.info("Loaded cog %s", module.name)

        if config.SYNC_COMMANDS_ON_START:
            if config.GUILD_ID:
                guild = discord.Object(id=config.GUILD_ID)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d command(s) to guild %s", len(synced), config.GUILD_ID)
            else:
                synced = await self.tree.sync()
                log.info("Synced %d command(s) globally", len(synced))

        self.tree.on_error = self.on_app_command_error

    async def close(self):
        await super().close()
        await self.db.close()

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message):
        # All commands are slash commands. Skip discord.py's text-command parsing, which would
        # otherwise treat "@Void Bot when ..." as a call to a command named "when" and log
        # CommandNotFound. Cog listeners (the throwing logger) still get every message.
        pass

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if interaction.extras.get("handled"):
            return  # a cog already replied
        if isinstance(error, app_commands.CheckFailure):
            msg = "You don't have permission to use this command."
        else:
            log.error("Error in /%s", interaction.command and interaction.command.qualified_name, exc_info=error)
            msg = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def main():
    discord.utils.setup_logging()
    bot = VoidBot()

    # Close cleanly on `docker stop` (SIGTERM) and Ctrl+C.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))

    async with bot:
        await bot.start(config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
