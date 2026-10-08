"""Claude tool plumbing shared by the throwing-session logger and /throwing query.

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


async def _players_on_file(db) -> dict:
    return {p.discord_id: p for p in await db.list_players()}


def _name_on_file(player) -> str | None:
    """First Last "Nickname", with whichever parts are on file."""
    if not player:
        return None
    return " ".join(filter(None, [player.full_name, player.nickname and f'"{player.nickname}"'])) or None


async def display_names(guild: discord.Guild, db) -> dict[int, str]:
    """Discord ID -> 'Display (First Last "Nickname")' for every human member."""
    on_file = await _players_on_file(db)
    return {
        m.id: f"{m.display_name} ({name})" if (name := _name_on_file(on_file.get(m.id))) else m.display_name
        for m in guild.members
        if not m.bot
    }


def find_members_tool(guild: discord.Guild, db):
    """A find_members tool bound to one server."""

    @beta_async_tool
    @logged_tool
    async def find_members(name: str) -> str:
        """Look up server members by name. Matches display names, server nicknames, usernames, and
        the first/last names and nicknames on file in the player database. Use it to turn every name
        into a Discord ID.

        Args:
            name: A first name, last name, full name, nickname, or username, or part of one.
        """
        needle = name.strip().lstrip("@").lower()
        if not needle:
            raise ToolError("name is empty.")
        on_file = await _players_on_file(db)
        matches = []
        for m in guild.members:
            if m.bot:
                continue
            player = on_file.get(m.id)
            fields = [
                m.display_name, m.name, m.global_name, getattr(m, "nick", None),
                player and player.full_name, player and player.nickname,
            ]
            if needle in " ".join(filter(None, fields)).lower():
                matches.append(
                    {
                        "id": str(m.id),
                        "display_name": m.display_name,
                        "username": m.name,
                        "name_on_file": player and player.full_name,
                        "nickname_on_file": player and player.nickname,
                    }
                )
        return json.dumps({"matches": matches[:15], "total_matches": len(matches)})

    return find_members
