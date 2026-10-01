import os
import sys


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


TOKEN = _required("DISCORD_TOKEN")
# Optional: sync slash commands to one guild for instant updates during development.
GUILD_ID = int(os.environ["DISCORD_GUILD_ID"]) if os.getenv("DISCORD_GUILD_ID") else None
SYNC_COMMANDS_ON_START = os.getenv("SYNC_COMMANDS_ON_START", "true").lower() != "false"
DB_PATH = os.getenv("DB_PATH", "data/players.db")
# Roles allowed to manage players (comma-separated IDs). Server administrators are always allowed.
ADMIN_ROLE_IDS = {int(i) for i in os.getenv("ADMIN_ROLE_IDS", "").split(",") if i.strip()}
# Optional: category that /group channels are created in.
GROUP_CATEGORY_ID = int(os.environ["GROUP_CATEGORY_ID"]) if os.getenv("GROUP_CATEGORY_ID") else None
# Throwing-session logging. Disabled unless both are set. The Anthropic SDK reads
# ANTHROPIC_API_KEY from the environment itself.
THROWING_CHANNEL_ID = int(os.environ["THROWING_CHANNEL_ID"]) if os.getenv("THROWING_CHANNEL_ID") else None
ANTHROPIC_API_KEY_SET = bool(os.getenv("ANTHROPIC_API_KEY"))
# Optional: channel that everything logged at ERROR or above is also posted to.
ERRORS_CHANNEL_ID = int(os.environ["ERRORS_CHANNEL_ID"]) if os.getenv("ERRORS_CHANNEL_ID") else None
# Pulling minutes and names out of a few messages is simple; the smallest model is plenty.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
# Used to interpret "yesterday", "this morning", etc. in reports.
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")
