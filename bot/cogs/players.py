import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import player_csv
from ..checks import admin_only
from ..db import DuplicateError, Player
from ..validation import ValidationError, clean_field, clean_fields, format_phone

MISSING = "—"

log = logging.getLogger(__name__)


def player_embed(player: Player) -> discord.Embed:
    embed = discord.Embed(title=player.full_name or "(no name yet)", color=discord.Color.blurple())
    embed.add_field(name="Discord", value=f"<@{player.discord_id}>")
    embed.add_field(name="Nickname", value=player.nickname or MISSING)
    embed.add_field(name="Penn ID", value=player.penn_id or MISSING)
    embed.add_field(name="Email", value=player.email or MISSING, inline=False)
    embed.add_field(name="Phone", value=format_phone(player.phone) or MISSING, inline=False)
    return embed


FIELD_INPUTS = {
    # field: (label, description, max_length)
    "first_name": ("First name", None, 100),
    "last_name": ("Last name", None, 100),
    "email": ("Email", None, 254),
    "penn_id": ("Penn ID", "8 digits", 20),
    "phone": ("Phone", "10–15 digits; formatting is ignored", 30),
}


class RetryView(discord.ui.View):
    """A "Fix it" button that reopens a modal, so a failed submit doesn't lose what was typed."""

    def __init__(self, make_modal):
        super().__init__(timeout=600)
        self.make_modal = make_modal

    @discord.ui.button(label="Fix it", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(self.make_modal())


class EditPlayerModal(discord.ui.Modal):
    """All five fields, prefilled. What's in the boxes on submit is saved; an emptied box clears that field."""

    def __init__(self, db, discord_id: int, display_name: str, values: dict[str, str | None]):
        super().__init__(title=f"Edit {display_name}"[:45])
        self.db = db
        self.discord_id = discord_id
        self.display_name = display_name
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for field, (label, description, max_length) in FIELD_INPUTS.items():
            text_input = discord.ui.TextInput(default=values.get(field) or None, required=False, max_length=max_length)
            self.inputs[field] = text_input
            self.add_item(discord.ui.Label(text=label, description=description, component=text_input))

    async def on_submit(self, interaction: discord.Interaction):
        raw = {field: text_input.value.strip() for field, text_input in self.inputs.items()}
        retry = RetryView(lambda: EditPlayerModal(self.db, self.discord_id, self.display_name, raw))

        changes, errors = {}, []
        for field, value in raw.items():
            if not value:
                changes[field] = None
                continue
            try:
                changes[field] = clean_field(field, value)
            except ValidationError as e:
                errors.append(str(e))
        if errors:
            await interaction.response.send_message(
                "Not saved:\n" + "\n".join(f"• {e}" for e in errors), view=retry, ephemeral=True
            )
            return

        current = await self.db.get_player(self.discord_id)
        if not current:
            await interaction.response.send_message(
                f"<@{self.discord_id}> is no longer in the database.", ephemeral=True
            )
            return
        changes = {field: value for field, value in changes.items() if getattr(current, field) != value}
        if not changes:
            await interaction.response.send_message("Nothing changed.", ephemeral=True)
            return

        try:
            player = await self.db.update_player(self.discord_id, **changes)
        except DuplicateError as e:
            await interaction.response.send_message(
                f"Not saved: another player already has that {e.field.replace('_', ' ')}.", view=retry, ephemeral=True
            )
            return
        await interaction.response.send_message("Player updated.", embed=player_embed(player), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Error updating player %s", self.discord_id, exc_info=error)
        msg = "Something went wrong saving the player."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class ClearPlayersModal(discord.ui.Modal, title="Wipe the player database?"):
    confirmation = discord.ui.Label(
        text="Type DELETE to confirm",
        component=discord.ui.TextInput(placeholder="DELETE", max_length=20),
    )

    def __init__(self, cog: "Players", player_count: int):
        super().__init__()
        self.cog = cog
        # Insert the warning above the confirmation box.
        self.remove_item(self.confirmation)
        self.add_item(
            discord.ui.TextDisplay(
                f"⚠️ **This is destructive.** It permanently deletes all **{player_count}** player "
                "records: names, emails, Penn IDs and phone numbers. It can't be undone.\n\n"
                "You'll get a CSV backup of what was deleted, which `/player import-csv` can restore."
            )
        )
        self.add_item(self.confirmation)

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirmation.component.value.strip().upper() != "DELETE":
            await interaction.response.send_message("Confirmation didn't match. Nothing was deleted.", ephemeral=True)
            return

        players = await self.cog.db.list_players()
        backup = self.cog._players_file(interaction.guild, players)
        deleted = await self.cog.db.clear_players()
        log.warning("%s cleared the player database (%d players)", interaction.user, deleted)
        await interaction.response.send_message(
            f"Deleted **{deleted}** player(s). Backup attached.", file=backup, ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Error clearing players", exc_info=error)
        msg = "Something went wrong. Check the bot logs before retrying."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


@app_commands.guild_only()
class Players(commands.GroupCog, group_name="player", group_description="Manage the player database."):
    def __init__(self, bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.db

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await admin_only(interaction)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        original = getattr(error, "original", error)
        if isinstance(original, ValidationError):
            msg = str(original)
        elif isinstance(original, DuplicateError):
            msg = f"Another player already has that {original.field.replace('_', ' ')}. Nothing was saved."
        else:
            return  # fall through to the bot-wide handler
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        interaction.extras["handled"] = True

    async def _ensure_members_cached(self, guild: discord.Guild):
        if not guild.chunked:
            await guild.chunk()

    @app_commands.command(name="import-role", description="Bulk-add everyone with a role. Existing players are left untouched.")
    async def import_role(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._ensure_members_cached(interaction.guild)

        members = [m for m in role.members if not m.bot]
        added = await self.db.import_players([m.id for m in members])
        await interaction.followup.send(
            f"Imported {role.mention}: **{added}** new player(s), "
            f"{len(members) - added} already in the database.\n"
            "To fill in their details in bulk, run `/player export`, fill in the spreadsheet, "
            "and upload it with `/player import-csv`.",
            ephemeral=True,
        )

    @app_commands.command(description="Add a single player. Details are optional and can be filled in later.")
    @app_commands.describe(penn_id="8-digit Penn ID", nickname="What teammates call them; separate several with commas")
    async def add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        penn_id: str | None = None,
        phone: str | None = None,
        nickname: str | None = None,
    ):
        if await self.db.get_player(member.id):
            await interaction.response.send_message(
                f"{member.mention} is already in the database. Use `/player update` instead.", ephemeral=True
            )
            return

        player = Player(member.id, **clean_fields(
            first_name=first_name, last_name=last_name, email=email, penn_id=penn_id, phone=phone, nickname=nickname
        ))
        await self.db.add_player(player)
        await interaction.response.send_message("Player added.", embed=player_embed(player), ephemeral=True)

    @app_commands.command(description="Show a player's info.")
    async def get(self, interaction: discord.Interaction, member: discord.User):
        player = await self.db.get_player(member.id)
        if not player:
            await interaction.response.send_message(f"{member.mention} isn't in the database.", ephemeral=True)
            return
        await interaction.response.send_message(embed=player_embed(player), ephemeral=True)

    # Named list_ so it doesn't shadow the built-in `list` in the class body.
    @app_commands.command(name="list", description="List players.")
    @app_commands.describe(incomplete_only="Only show players with missing details")
    async def list_(self, interaction: discord.Interaction, incomplete_only: bool = False):
        players = await self.db.list_players()
        if incomplete_only:
            players = [p for p in players if not p.is_complete]
        if not players:
            await interaction.response.send_message(
                "Every player's details are filled in." if incomplete_only else "No players yet.", ephemeral=True
            )
            return

        lines = [
            " · ".join(
                [
                    f"**{p.last_name or MISSING}, {p.first_name or MISSING}**" + (f' "{p.nickname}"' if p.nickname else ""),
                    f"<@{p.discord_id}>",
                    p.penn_id or MISSING,
                    p.email or MISSING,
                    format_phone(p.phone) or MISSING,
                ]
            )
            for p in players
        ]
        description = "\n".join(lines)
        title = f"{'Incomplete players' if incomplete_only else 'Players'} ({len(players)})"
        if len(description) <= 4096:
            embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Too long for one embed: send the spreadsheet instead.
        await interaction.response.send_message(
            f"{len(players)} players (attached as CSV).",
            file=self._players_file(interaction.guild, players),
            ephemeral=True,
        )

    def _players_file(self, guild: discord.Guild, players: list[Player]) -> discord.File:
        usernames = {p.discord_id: m.name for p in players if (m := guild.get_member(p.discord_id))}
        return discord.File(io.BytesIO(player_csv.write_csv(players, usernames)), filename="players.csv")

    @app_commands.command(description="Download players as a spreadsheet to fill in and re-upload with /player import-csv.")
    @app_commands.describe(incomplete_only="Only include players with missing details")
    async def export(self, interaction: discord.Interaction, incomplete_only: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._ensure_members_cached(interaction.guild)
        players = await self.db.list_players()
        if incomplete_only:
            players = [p for p in players if not p.is_complete]
        if not players:
            await interaction.followup.send("No players to export.", ephemeral=True)
            return
        await interaction.followup.send(
            f"{len(players)} player(s). Fill in the blanks and upload with `/player import-csv`. "
            "Blank cells are left unchanged on import.",
            file=self._players_file(interaction.guild, players),
            ephemeral=True,
        )

    @app_commands.command(name="import-csv", description="Create or update players from a spreadsheet (CSV).")
    @app_commands.describe(file="CSV with discord_id and/or discord_username plus player columns")
    async def import_csv(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if file.size > 2_000_000:
            await interaction.followup.send("That file is too big (max 2 MB).", ephemeral=True)
            return
        try:
            text = (await file.read()).decode("utf-8-sig")
        except UnicodeDecodeError:
            await interaction.followup.send("Couldn't read that file. Save it as **CSV UTF-8** and try again.", ephemeral=True)
            return

        guild = interaction.guild
        await self._ensure_members_cached(guild)
        existing = await self.db.list_players()
        humans = [m for m in guild.members if not m.bot]
        result = player_csv.parse_csv(
            text,
            known_ids={m.id for m in humans} | {p.discord_id for p in existing},
            ids_by_username={m.name.lower(): m.id for m in humans},
            penn_id_owners={p.penn_id: p.discord_id for p in existing if p.penn_id},
        )
        if result.errors:
            await interaction.followup.send(
                "Nothing was imported. Fix these and upload again:\n" + player_csv.format_errors(result.errors),
                ephemeral=True,
            )
            return
        if not result.rows:
            await interaction.followup.send("No rows found in that file.", ephemeral=True)
            return

        created, updated = await self.db.upsert_players(result.rows)
        msg = f"Imported {len(result.rows)} row(s): **{created}** new player(s), **{updated}** updated."
        if result.ignored_columns:
            msg += f"\nIgnored unrecognized columns: {', '.join(f'`{c}`' for c in result.ignored_columns)}"
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(description="Edit a player's info in a form.")
    async def update(self, interaction: discord.Interaction, member: discord.User):
        player = await self.db.get_player(member.id)
        if not player:
            await interaction.response.send_message(
                f"{member.mention} isn't in the database. Add them with `/player add` first.", ephemeral=True
            )
            return
        values = {field: getattr(player, field) for field in FIELD_INPUTS}
        await interaction.response.send_modal(EditPlayerModal(self.db, member.id, member.display_name, values))

    # Its own command because the edit form is full: a Discord form holds five fields at most.
    @app_commands.command(description="Set or clear a player's nickname, so throwing reports can use it.")
    @app_commands.describe(nickname="What teammates call them; separate several with commas. Leave out to clear it.")
    async def nickname(
        self, interaction: discord.Interaction, member: discord.User, nickname: app_commands.Range[str, 1, 100] | None = None
    ):
        if not await self.db.get_player(member.id):
            await interaction.response.send_message(
                f"{member.mention} isn't in the database. Add them with `/player add` first.", ephemeral=True
            )
            return
        player = await self.db.update_player(member.id, nickname=clean_field("nickname", nickname) if nickname else None)
        await interaction.response.send_message(
            "Nickname saved." if nickname else "Nickname cleared.", embed=player_embed(player), ephemeral=True
        )

    @app_commands.command(description="Remove a player from the database.")
    async def remove(self, interaction: discord.Interaction, member: discord.User):
        if await self.db.delete_player(member.id):
            await interaction.response.send_message(f"Removed {member.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"{member.mention} isn't in the database.", ephemeral=True)

    @app_commands.command(description="Delete ALL players. Asks for confirmation first.")
    async def clear(self, interaction: discord.Interaction):
        count = len(await self.db.list_players())
        if not count:
            await interaction.response.send_message("The player database is already empty.", ephemeral=True)
            return
        await interaction.response.send_modal(ClearPlayersModal(self, count))


async def setup(bot: commands.Bot):
    await bot.add_cog(Players(bot))
