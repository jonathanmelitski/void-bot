"""Reading and writing the player spreadsheet used by /player export and /player import-csv."""

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from .db import Player
from .validation import ValidationError, clean_field

COLUMNS = ["discord_id", "discord_username", "first_name", "last_name", "email", "penn_id", "phone"]
DETAIL_COLUMNS = ["first_name", "last_name", "email", "penn_id", "phone"]

# Header spellings people are likely to use, after normalizing to snake_case.
HEADER_ALIASES = {
    "id": "discord_id",
    "user_id": "discord_id",
    "username": "discord_username",
    "discord": "discord_username",
    "discord_name": "discord_username",
    "first": "first_name",
    "firstname": "first_name",
    "last": "last_name",
    "lastname": "last_name",
    "email_address": "email",
    "pennid": "penn_id",
    "phone_number": "phone",
    "cell": "phone",
    "mobile": "phone",
}

MAX_ERRORS_SHOWN = 15


def normalize_header(header: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")
    return HEADER_ALIASES.get(key, key)


def write_csv(players: Iterable[Player], usernames: dict[int, str]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLUMNS)
    for p in players:
        writer.writerow(
            [p.discord_id, usernames.get(p.discord_id, ""), p.first_name, p.last_name, p.email, p.penn_id, p.phone]
        )
    # BOM so Excel opens it as UTF-8 (names with accents etc.).
    return buf.getvalue().encode("utf-8-sig")


@dataclass
class ParseResult:
    rows: list[tuple[int, dict]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)


def parse_csv(
    text: str,
    known_ids: set[int],
    ids_by_username: dict[str, int],
    penn_id_owners: dict[str, int],
) -> ParseResult:
    """Validate an uploaded sheet. Each row becomes (discord_id, {field: value}) with blank cells left out.

    known_ids: server members and existing players. A discord_id not in here (e.g. one a
      spreadsheet rounded) falls back to the discord_username column.
    ids_by_username: lowercase Discord username -> user ID, for current server members.
    penn_id_owners: Penn IDs already in the database -> the player who has it.
    """
    result = ParseResult()
    reader = csv.reader(io.StringIO(text))
    try:
        headers = [normalize_header(h) for h in next(reader)]
    except StopIteration:
        result.errors.append("The file is empty.")
        return result

    if not {"discord_id", "discord_username"} & set(headers):
        result.errors.append("Needs a `discord_id` or `discord_username` column to know who each row is.")
    if not set(DETAIL_COLUMNS) & set(headers):
        result.errors.append(f"No player columns found. Expected some of: {', '.join(DETAIL_COLUMNS)}.")
    if result.errors:
        return result
    result.ignored_columns = [h for h in headers if h and h not in COLUMNS]

    seen_ids: dict[int, int] = {}  # discord_id -> row number
    penn_ids_in_file: dict[str, int] = {}  # penn_id -> discord_id
    for row_num, values in enumerate(reader, start=2):
        cells = {h: v.strip() for h, v in zip(headers, values) if h in COLUMNS}
        if not any(cells.values()):
            continue

        def error(msg: str):
            result.errors.append(f"Row {row_num}: {msg}")

        discord_id = None
        raw_id = cells.get("discord_id", "")
        if raw_id.isdigit() and int(raw_id) in known_ids:
            discord_id = int(raw_id)
        username = cells.get("discord_username", "").lstrip("@").lower()
        if discord_id is None and username:
            discord_id = ids_by_username.get(username)
        if discord_id is None:
            if username:
                error(f"no server member with username `{username}`.")
            elif raw_id:
                error(f"unknown Discord ID `{raw_id}` (spreadsheets often round long IDs; add a discord_username column).")
            else:
                error("no discord_id or discord_username.")
            continue
        if discord_id in seen_ids:
            error(f"same person as row {seen_ids[discord_id]}.")
            continue
        seen_ids[discord_id] = row_num

        changes = {}
        for col in DETAIL_COLUMNS:
            if cells.get(col):
                try:
                    changes[col] = clean_field(col, cells[col])
                except ValidationError as e:
                    error(str(e))

        penn_id = changes.get("penn_id")
        if penn_id:
            owner = penn_ids_in_file.get(penn_id) or penn_id_owners.get(penn_id)
            if owner is not None and owner != discord_id:
                error(f"Penn ID {penn_id} already belongs to <@{owner}>.")
            penn_ids_in_file[penn_id] = discord_id

        result.rows.append((discord_id, changes))
    return result


def format_errors(errors: list[str]) -> str:
    shown = errors[:MAX_ERRORS_SHOWN]
    more = len(errors) - len(shown)
    return "\n".join(f"• {e}" for e in shown) + (f"\n…and {more} more." if more else "")
