"""Claude tool plumbing shared by the auto-logger and the @Void Bot assistant.

Tools never run SQL that Claude wrote: they validate arguments here and call fixed queries.
Member lookups return names only, never emails, phone numbers, or Penn IDs.
"""

import functools
import json
import logging

import discord
from anthropic import beta_async_tool

log = logging.getLogger(__name__)


class ToolError(Exception):
    """A bad tool argument; the message goes back to Claude so it can correct itself."""


def logged_tool(fn):
    """Log every call, and turn ToolErrors into a message Claude can act on."""

    @functools.wraps(fn)  # keeps the name, docstring, and type hints the schema is built from
    async def wrapper(**kwargs):
        try:
            out = await fn(**kwargs)
            log.info("  tool %s(%s) -> %s", fn.__name__, kwargs, out[:300])
            return out
        except ToolError as e:
            log.info("  tool %s(%s) -> error: %s", fn.__name__, kwargs, e)
            return f"Error: {e}"

    return wrapper


def parse_id(value) -> int:
    value = str(value).strip().removeprefix("<@").removeprefix("!").removesuffix(">")
    if not value.isdigit():
        raise ToolError(f"{value!r} isn't a Discord user ID. Use find_members to look people up.")
    return int(value)


async def _names_on_file(db) -> dict[int, str]:
    return {
        p.discord_id: name
        for p in await db.list_players()
        if (name := " ".join(filter(None, [p.first_name, p.last_name])))
    }


async def display_names(guild: discord.Guild, db) -> dict[int, str]:
    """Discord ID -> "Display (First Last)" for every human member."""
    on_file = await _names_on_file(db)
    return {
        m.id: f"{m.display_name} ({on_file[m.id]})" if m.id in on_file else m.display_name
        for m in guild.members
        if not m.bot
    }


def find_members_tool(guild: discord.Guild, db):
    """A find_members tool bound to one server."""

    @beta_async_tool
    @logged_tool
    async def find_members(name: str) -> str:
        """Look up server members by name. Matches display names, nicknames, usernames, and the
        first/last names on file in the player database. Use it to turn every name into a Discord ID.

        Args:
            name: A name or part of one, e.g. "luke", "Max Power", or a username.
        """
        needle = name.strip().lstrip("@").lower()
        if not needle:
            raise ToolError("name is empty.")
        on_file = await _names_on_file(db)
        matches = []
        for m in guild.members:
            if m.bot:
                continue
            fields = [m.display_name, m.name, m.global_name, getattr(m, "nick", None), on_file.get(m.id)]
            if needle in " ".join(filter(None, fields)).lower():
                matches.append(
                    {
                        "id": str(m.id),
                        "display_name": m.display_name,
                        "username": m.name,
                        "name_on_file": on_file.get(m.id),
                    }
                )
        return json.dumps({"matches": matches[:15], "total_matches": len(matches)})

    return find_members
