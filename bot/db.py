import json
import sqlite3
import uuid
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

# Each entry upgrades the schema by one version. Append only; never edit old entries.
MIGRATIONS = [
    """
    CREATE TABLE players (
        discord_id  INTEGER PRIMARY KEY,
        first_name  TEXT,
        last_name   TEXT,
        email       TEXT,
        penn_id     TEXT UNIQUE,
        phone       TEXT,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE throwing_sessions (
        id                 TEXT PRIMARY KEY,               -- UUID4
        occurred_at        TEXT NOT NULL,                  -- ISO 8601, UTC
        minutes            INTEGER NOT NULL CHECK (minutes > 0),
        description        TEXT,
        reported_by        INTEGER NOT NULL,               -- Discord user ID
        source_message_id  INTEGER UNIQUE,                 -- the report message; stops double-logging
        created_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE session_participants (
        session_id  TEXT NOT NULL REFERENCES throwing_sessions(id) ON DELETE CASCADE,
        discord_id  INTEGER NOT NULL,
        PRIMARY KEY (session_id, discord_id)
    );
    CREATE INDEX session_participants_by_user ON session_participants(discord_id);
    """,
    """
    CREATE TABLE report_threads (
        message_id  INTEGER PRIMARY KEY,                   -- the report being asked about
        thread_id   INTEGER NOT NULL UNIQUE,               -- the bot's thread asking about it
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE throwing_groups (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT NOT NULL,
        starts_at    TEXT NOT NULL,                        -- ISO 8601, UTC; the group's life is [starts_at, ends_at)
        ends_at      TEXT NOT NULL,
        count_solo   INTEGER NOT NULL DEFAULT 0,           -- a session with only one of its members counts
        require_all  INTEGER NOT NULL DEFAULT 0,           -- a session with several but not all members doesn't count
        created_by   INTEGER NOT NULL,                     -- Discord user ID
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE throwing_group_members (
        group_id    INTEGER NOT NULL REFERENCES throwing_groups(id) ON DELETE CASCADE,
        discord_id  INTEGER NOT NULL,
        PRIMARY KEY (group_id, discord_id)
    );
    CREATE INDEX throwing_group_members_by_user ON throwing_group_members(discord_id);
    -- Which groups each session involves: one row per session and group that had at least one member
    -- in it during the group's life. It's a view, so it can't fall out of step when a session, a
    -- group's members, its dates, or its settings are edited. `counts` applies the group's settings:
    -- a session with every member always counts, one member counts if count_solo, and several but
    -- not all count unless require_all.
    CREATE VIEW session_groups AS
    SELECT x.session_id, x.group_id, x.minutes, x.occurred_at, x.members_present, x.group_size,
           CASE
               WHEN x.members_present = x.group_size THEN 1
               WHEN x.members_present = 1 THEN x.count_solo
               ELSE NOT x.require_all
           END AS counts
    FROM (
        SELECT s.id AS session_id, g.id AS group_id, s.minutes, s.occurred_at, g.count_solo, g.require_all,
               COUNT(*) AS members_present,
               (SELECT COUNT(*) FROM throwing_group_members WHERE group_id = g.id) AS group_size
        FROM throwing_sessions s
        JOIN session_participants p ON p.session_id = s.id
        JOIN throwing_group_members m ON m.discord_id = p.discord_id
        JOIN throwing_groups g ON g.id = m.group_id AND s.occurred_at >= g.starts_at AND s.occurred_at < g.ends_at
        GROUP BY s.id, g.id
    ) x;
    """,
    """
    ALTER TABLE players ADD COLUMN nickname TEXT;          -- what teammates call them; several separated by commas
    -- Reports that were added to a session someone else had already logged. The report that created
    -- a session is throwing_sessions.source_message_id; these are the later ones.
    CREATE TABLE session_reports (
        message_id  INTEGER PRIMARY KEY,
        session_id  TEXT NOT NULL REFERENCES throwing_sessions(id) ON DELETE CASCADE,
        created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    -- Every channel /group create has made. /group commands only ever act on channels listed here.
    -- Who is in a group isn't stored: it's whoever the channel's permissions let in.
    CREATE TABLE group_channels (
        channel_id   INTEGER PRIMARY KEY,
        name         TEXT NOT NULL,                        -- as created, before any archived- prefix
        created_by   INTEGER NOT NULL,                     -- Discord user ID
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        archived_at  TEXT                                  -- ISO 8601, UTC; NULL while active
    );
    -- People let into a group for a short while (/group join). Stored so they're still removed on
    -- time if the bot restarts in between.
    CREATE TABLE group_visits (
        channel_id  INTEGER NOT NULL REFERENCES group_channels(channel_id) ON DELETE CASCADE,
        discord_id  INTEGER NOT NULL,
        expires_at  TEXT NOT NULL,                         -- ISO 8601, UTC
        PRIMARY KEY (channel_id, discord_id)
    );
    """,
    """
    -- A minutes goal that repeats: cycle 0 starts on first_day, and each cycle is cycle_days long.
    -- Nothing is stored per cycle; a person's progress is their session minutes inside the cycle.
    CREATE TABLE throwing_goals (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        name                TEXT NOT NULL,
        minutes             INTEGER NOT NULL CHECK (minutes > 0),   -- per person, per cycle
        first_day           TEXT NOT NULL,                 -- YYYY-MM-DD in the bot's time zone
        cycle_days          INTEGER NOT NULL CHECK (cycle_days > 0),
        channel_id          INTEGER NOT NULL,              -- where reminders and new groups are posted
        remind_days_before  TEXT NOT NULL DEFAULT '',      -- comma-separated; 0 is the cycle's last day. Empty: no reminders
        remind_hour         INTEGER NOT NULL DEFAULT 18,   -- 0-23, in the bot's time zone
        last_reminder_at    TEXT,                          -- ISO 8601, UTC; reminder times up to here are done
        group_config        TEXT,                          -- JSON GroupConfig; NULL: don't make groups each cycle
        groups_cycle        INTEGER NOT NULL DEFAULT -1,   -- the last cycle groups were made for
        created_by          INTEGER NOT NULL,              -- Discord user ID
        created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    -- Who a goal applies to: roles and people, looked up afresh each time, so someone given a role
    -- later is included from then on. Anyone matched by an excluded row is left out.
    CREATE TABLE throwing_goal_targets (
        goal_id    INTEGER NOT NULL REFERENCES throwing_goals(id) ON DELETE CASCADE,
        target_id  INTEGER NOT NULL,                       -- Discord role or user ID
        is_role    INTEGER NOT NULL,
        excluded   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (goal_id, target_id)
    );
    """,
]


@dataclass
class Player:
    # Details start empty for players bulk-imported from a role.
    discord_id: int
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    penn_id: str | None = None
    phone: str | None = None
    nickname: str | None = None

    @property
    def is_complete(self) -> bool:
        return all(getattr(self, c) for c in EDITABLE_COLUMNS - OPTIONAL_COLUMNS)

    @property
    def full_name(self) -> str | None:
        return " ".join(filter(None, [self.first_name, self.last_name])) or None


PLAYER_COLUMNS = [f.name for f in fields(Player)]
EDITABLE_COLUMNS = set(PLAYER_COLUMNS) - {"discord_id"}
OPTIONAL_COLUMNS = {"nickname"}  # most players don't have one


def _utc(dt: datetime) -> str:
    """One fixed format for stored times, so they compare correctly as strings."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class DuplicateError(Exception):
    """A unique field (e.g. penn_id) is already used by another player."""

    def __init__(self, field: str):
        super().__init__(field)
        self.field = field


def _duplicate_field(err: sqlite3.IntegrityError) -> str:
    # Message looks like "UNIQUE constraint failed: players.penn_id"
    return str(err).rsplit(".", 1)[-1]


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate()

    async def schema(self) -> str:
        """The CREATE TABLE and CREATE VIEW statements, as SQLite stores them."""
        async with self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ) as cur:
            return "\n\n".join(row["sql"].strip() + ";" for row in await cur.fetchall())

    async def read_only_query(self, sql: str, max_rows: int) -> tuple[list[str], list[tuple]]:
        """Run one statement on a connection that can't write. Returns the column names and up to
        max_rows + 1 rows (one extra, so the caller can tell there were more)."""
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("PRAGMA query_only = ON")
            async with conn.execute(sql) as cur:
                rows = await cur.fetchmany(max_rows + 1)
                columns = [d[0] for d in cur.description or []]
        return columns, [tuple(r) for r in rows]

    async def run_statements(self, statements: list[str], *, commit: bool) -> list[int]:
        """Run statements in one transaction and return the rows each one changed. With commit=False
        it's a dry run: everything is rolled back. Uses its own connection, so the transaction can't
        get mixed up with the bot's other writes."""
        async with aiosqlite.connect(self.path, isolation_level=None) as conn:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("BEGIN IMMEDIATE")
            try:
                counts = []
                for sql in statements:
                    cur = await conn.execute(sql)
                    counts.append(cur.rowcount)
                await conn.execute("COMMIT" if commit else "ROLLBACK")
            finally:
                if conn.in_transaction:
                    await conn.execute("ROLLBACK")
        return counts

    async def backup(self, path: str):
        """Copy the whole database to another file. Safe while the bot is running."""
        async with aiosqlite.connect(path) as target:
            await self.conn.backup(target)

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _migrate(self):
        async with self.conn.execute("PRAGMA user_version") as cur:
            (version,) = await cur.fetchone()
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            # One transaction per migration, so a failure can't leave a half-applied schema.
            await self.conn.executescript(f"BEGIN; {script} PRAGMA user_version = {i}; COMMIT;")

    @staticmethod
    def _to_player(row: aiosqlite.Row | None) -> Player | None:
        return Player(**{c: row[c] for c in PLAYER_COLUMNS}) if row else None

    async def get_player(self, discord_id: int) -> Player | None:
        async with self.conn.execute(
            f"SELECT {', '.join(PLAYER_COLUMNS)} FROM players WHERE discord_id = ?", (discord_id,)
        ) as cur:
            return self._to_player(await cur.fetchone())

    async def list_players(self) -> list[Player]:
        async with self.conn.execute(
            f"SELECT {', '.join(PLAYER_COLUMNS)} FROM players ORDER BY last_name IS NULL, last_name, first_name"
        ) as cur:
            return [self._to_player(row) for row in await cur.fetchall()]

    async def add_player(self, player: Player):
        try:
            await self.conn.execute(
                f"INSERT INTO players ({', '.join(PLAYER_COLUMNS)}) "
                f"VALUES ({', '.join('?' for _ in PLAYER_COLUMNS)})",
                tuple(getattr(player, c) for c in PLAYER_COLUMNS),
            )
            await self.conn.commit()
        except sqlite3.IntegrityError as err:
            await self.conn.rollback()
            raise DuplicateError(_duplicate_field(err)) from err

    async def import_players(self, discord_ids: list[int]) -> int:
        """Create empty records for these users, skipping existing ones. Returns how many were added."""
        before = self.conn.total_changes
        await self.conn.executemany(
            "INSERT OR IGNORE INTO players (discord_id) VALUES (?)", [(i,) for i in discord_ids]
        )
        await self.conn.commit()
        return self.conn.total_changes - before

    async def _set_fields(self, discord_id: int, changes: dict):
        unknown = set(changes) - EDITABLE_COLUMNS
        if unknown:
            raise ValueError(f"Not editable: {', '.join(sorted(unknown))}")
        if changes:
            assignments = ", ".join(f"{c} = ?" for c in changes)
            await self.conn.execute(
                f"UPDATE players SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE discord_id = ?",
                (*changes.values(), discord_id),
            )

    async def update_player(self, discord_id: int, **changes) -> Player | None:
        """Update the given fields. Returns the updated player, or None if not found."""
        try:
            await self._set_fields(discord_id, changes)
            await self.conn.commit()
        except sqlite3.IntegrityError as err:
            await self.conn.rollback()
            raise DuplicateError(_duplicate_field(err)) from err
        return await self.get_player(discord_id)

    async def upsert_players(self, rows: list[tuple[int, dict]]) -> tuple[int, int]:
        """Create or update many players in one transaction; all or nothing.
        Returns (created, updated)."""
        created = 0
        try:
            for discord_id, changes in rows:
                cur = await self.conn.execute(
                    "INSERT OR IGNORE INTO players (discord_id) VALUES (?)", (discord_id,)
                )
                created += cur.rowcount
                await self._set_fields(discord_id, changes)
            await self.conn.commit()
        except sqlite3.IntegrityError as err:
            await self.conn.rollback()
            raise DuplicateError(_duplicate_field(err)) from err
        return created, len(rows) - created

    async def clear_players(self) -> int:
        """Delete every player. Returns how many were deleted."""
        cur = await self.conn.execute("DELETE FROM players")
        await self.conn.commit()
        return cur.rowcount

    async def delete_player(self, discord_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM players WHERE discord_id = ?", (discord_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def sessions_for_messages(self, message_ids: list[int]) -> dict[int, str]:
        """Report message ID -> the session it was logged as or added to, for those already handled."""
        if not message_ids:
            return {}
        marks = ", ".join("?" for _ in message_ids)
        async with self.conn.execute(
            f"SELECT source_message_id AS message_id, id AS session_id FROM throwing_sessions WHERE source_message_id IN ({marks}) "
            f"UNION ALL SELECT message_id, session_id FROM session_reports WHERE message_id IN ({marks})",
            [*message_ids, *message_ids],
        ) as cur:
            return {row["message_id"]: row["session_id"] for row in await cur.fetchall()}

    async def report_threads(self, message_ids: list[int]) -> dict[int, int]:
        """Report message ID -> the thread asking about it, for those that have one."""
        if not message_ids:
            return {}
        async with self.conn.execute(
            "SELECT message_id, thread_id FROM report_threads "
            f"WHERE message_id IN ({', '.join('?' for _ in message_ids)})",
            message_ids,
        ) as cur:
            return {row["message_id"]: row["thread_id"] for row in await cur.fetchall()}

    async def report_for_thread(self, thread_id: int) -> int | None:
        async with self.conn.execute(
            "SELECT message_id FROM report_threads WHERE thread_id = ?", (thread_id,)
        ) as cur:
            row = await cur.fetchone()
        return row["message_id"] if row else None

    async def add_report_thread(self, message_id: int, thread_id: int):
        await self.conn.execute(
            "INSERT OR REPLACE INTO report_threads (message_id, thread_id) VALUES (?, ?)", (message_id, thread_id)
        )
        await self.conn.commit()

    async def remove_report_thread(self, message_id: int):
        await self.conn.execute("DELETE FROM report_threads WHERE message_id = ?", (message_id,))
        await self.conn.commit()

    async def log_session(
        self,
        *,
        occurred_at: datetime,
        minutes: int,
        description: str | None,
        participant_ids: list[int],
        reported_by: int,
        source_message_id: int | None,
    ) -> str:
        """Record a throwing session and its participants in one transaction. Returns the session UUID."""
        session_id = str(uuid.uuid4())
        try:
            await self.conn.execute(
                "INSERT INTO throwing_sessions "
                "(id, occurred_at, minutes, description, reported_by, source_message_id) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, _utc(occurred_at), minutes, description, reported_by, source_message_id),
            )
            await self.conn.executemany(
                "INSERT OR IGNORE INTO session_participants (session_id, discord_id) VALUES (?, ?)",
                [(session_id, d) for d in participant_ids],
            )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return session_id

    async def join_session(self, session_id: str, participant_ids: list[int], message_id: int) -> bool:
        """Add people to a session that's already logged, and record the report that added them so
        it isn't read again. False if the session no longer exists."""
        try:
            async with self.conn.execute("SELECT 1 FROM throwing_sessions WHERE id = ?", (session_id,)) as cur:
                if not await cur.fetchone():
                    return False
            await self.conn.executemany(
                "INSERT OR IGNORE INTO session_participants (session_id, discord_id) VALUES (?, ?)",
                [(session_id, d) for d in participant_ids],
            )
            await self.conn.execute(
                "INSERT OR REPLACE INTO session_reports (message_id, session_id) VALUES (?, ?)", (message_id, session_id)
            )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return True

    async def throwing_totals(
        self, start: datetime, end: datetime, discord_ids: list[int] | None = None, limit: int = 25
    ) -> list[dict]:
        """Minutes and session counts per person for sessions in [start, end), most minutes first."""
        where, params = "s.occurred_at >= ? AND s.occurred_at < ?", [_utc(start), _utc(end)]
        if discord_ids:
            where += f" AND p.discord_id IN ({', '.join('?' for _ in discord_ids)})"
            params += discord_ids
        async with self.conn.execute(
            "SELECT p.discord_id, SUM(s.minutes) AS minutes, COUNT(*) AS sessions "
            "FROM session_participants p JOIN throwing_sessions s ON s.id = p.session_id "
            f"WHERE {where} GROUP BY p.discord_id ORDER BY minutes DESC LIMIT ?",
            (*params, limit),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def list_sessions(
        self, start: datetime, end: datetime, discord_id: int | None = None, limit: int = 20
    ) -> list[dict]:
        """Sessions in [start, end), newest first, each with its participant IDs."""
        where, params = "s.occurred_at >= ? AND s.occurred_at < ?", [_utc(start), _utc(end)]
        if discord_id:
            where += " AND s.id IN (SELECT session_id FROM session_participants WHERE discord_id = ?)"
            params.append(discord_id)
        async with self.conn.execute(
            "SELECT s.id, s.occurred_at, s.minutes, s.description, s.reported_by, "
            "group_concat(p.discord_id) AS participants "
            "FROM throwing_sessions s JOIN session_participants p ON p.session_id = s.id "
            f"WHERE {where} GROUP BY s.id ORDER BY s.occurred_at DESC LIMIT ?",
            (*params, limit),
        ) as cur:
            rows = [dict(row) for row in await cur.fetchall()]
        for row in rows:
            row["participants"] = [int(i) for i in row["participants"].split(",")]
        return rows

    # ---- group channels ----

    async def add_group_channel(self, channel_id: int, name: str, created_by: int):
        await self.conn.execute(
            "INSERT OR IGNORE INTO group_channels (channel_id, name, created_by) VALUES (?, ?, ?)",
            (channel_id, name, created_by),
        )
        await self.conn.commit()

    async def list_group_channels(self, *, include_archived: bool = False) -> list[dict]:
        """Channels made by /group create, newest first."""
        where = "1" if include_archived else "archived_at IS NULL"
        async with self.conn.execute(
            f"SELECT channel_id, name, created_by, created_at, archived_at FROM group_channels WHERE {where} "
            "ORDER BY created_at DESC, channel_id DESC"
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def get_group_channel(self, channel_id: int) -> dict | None:
        async with self.conn.execute(
            "SELECT channel_id, name, created_by, created_at, archived_at FROM group_channels WHERE channel_id = ?",
            (channel_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def archive_group_channel(self, channel_id: int):
        """Mark a group archived and forget its visits."""
        await self.conn.execute(
            "UPDATE group_channels SET archived_at = ? WHERE channel_id = ?",
            (_utc(datetime.now(timezone.utc)), channel_id),
        )
        await self.conn.execute("DELETE FROM group_visits WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()

    async def delete_group_channel(self, channel_id: int):
        await self.conn.execute("DELETE FROM group_channels WHERE channel_id = ?", (channel_id,))
        await self.conn.commit()

    async def add_group_visit(self, channel_id: int, discord_id: int, expires_at: datetime):
        """Start a visit, or move the end of one that's already running."""
        await self.conn.execute(
            "INSERT OR REPLACE INTO group_visits (channel_id, discord_id, expires_at) VALUES (?, ?, ?)",
            (channel_id, discord_id, _utc(expires_at)),
        )
        await self.conn.commit()

    async def remove_group_visit(self, channel_id: int, discord_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM group_visits WHERE channel_id = ? AND discord_id = ?", (channel_id, discord_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def group_visits(self, *, channel_id: int | None = None, expired_only: bool = False) -> list[dict]:
        where, params = ["1"], []
        if channel_id is not None:
            where.append("channel_id = ?")
            params.append(channel_id)
        if expired_only:
            where.append("expires_at <= ?")
            params.append(_utc(datetime.now(timezone.utc)))
        async with self.conn.execute(
            f"SELECT channel_id, discord_id, expires_at FROM group_visits WHERE {' AND '.join(where)}", params
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    # ---- throwing groups ----

    async def create_groups(
        self,
        groups: list[tuple[str, list[int]]],
        *,
        starts_at: datetime,
        ends_at: datetime,
        count_solo: bool,
        require_all: bool,
        created_by: int,
        end_others: bool,
        goal_cycle: tuple[int, int] | None = None,
    ) -> list[int]:
        """Create (name, member IDs) groups in one transaction, optionally ending every group that's
        still running or yet to start. Returns the new group IDs. goal_cycle is (goal ID, cycle) when
        a goal is making its groups for a cycle; it's recorded in the same transaction, so a cycle
        can't get two sets."""
        try:
            if goal_cycle:
                await self.conn.execute("UPDATE throwing_goals SET groups_cycle = ? WHERE id = ?", goal_cycle[::-1])
            if end_others:
                now = _utc(datetime.now(timezone.utc))
                await self.conn.execute("UPDATE throwing_groups SET ends_at = ? WHERE ends_at > ?", (now, now))
            ids = []
            for name, members in groups:
                cur = await self.conn.execute(
                    "INSERT INTO throwing_groups (name, starts_at, ends_at, count_solo, require_all, created_by) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, _utc(starts_at), _utc(ends_at), int(count_solo), int(require_all), created_by),
                )
                ids.append(cur.lastrowid)
                await self.conn.executemany(
                    "INSERT INTO throwing_group_members (group_id, discord_id) VALUES (?, ?)",
                    [(cur.lastrowid, m) for m in members],
                )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return ids

    # ---- throwing goals ----

    async def create_goal(
        self,
        *,
        name: str,
        minutes: int,
        first_day: str,
        cycle_days: int,
        channel_id: int,
        remind_days_before: list[int],
        remind_hour: int,
        group_config: dict | None,
        targets: list[dict],
        created_by: int,
    ) -> int:
        """targets are {target_id, is_role, excluded}. Reminder times before now count as done, so a
        goal made late in a cycle doesn't send that cycle's earlier reminders at once."""
        try:
            cur = await self.conn.execute(
                "INSERT INTO throwing_goals (name, minutes, first_day, cycle_days, channel_id, remind_days_before, "
                "remind_hour, last_reminder_at, group_config, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name, minutes, first_day, cycle_days, channel_id, ",".join(map(str, remind_days_before)),
                    remind_hour, _utc(datetime.now(timezone.utc)), group_config and json.dumps(group_config), created_by,
                ),
            )
            await self.conn.executemany(
                "INSERT OR REPLACE INTO throwing_goal_targets (goal_id, target_id, is_role, excluded) VALUES (?, ?, ?, ?)",
                [(cur.lastrowid, t["target_id"], int(t["is_role"]), int(t["excluded"])) for t in targets],
            )
            await self.conn.commit()
        except Exception:
            await self.conn.rollback()
            raise
        return cur.lastrowid

    async def list_goals(self, goal_id: int | None = None) -> list[dict]:
        where, params = ("id = ?", [goal_id]) if goal_id is not None else ("1", [])
        async with self.conn.execute(f"SELECT * FROM throwing_goals WHERE {where} ORDER BY id", params) as cur:
            goals = [dict(row) for row in await cur.fetchall()]
        for goal in goals:
            goal["remind_days_before"] = [int(d) for d in goal["remind_days_before"].split(",") if d]
            goal["group_config"] = json.loads(goal["group_config"]) if goal["group_config"] else None
            async with self.conn.execute(
                "SELECT target_id, is_role, excluded FROM throwing_goal_targets WHERE goal_id = ? ORDER BY excluded, is_role DESC",
                (goal["id"],),
            ) as cur:
                goal["targets"] = [dict(row) for row in await cur.fetchall()]
        return goals

    async def get_goal(self, goal_id: int) -> dict | None:
        goals = await self.list_goals(goal_id)
        return goals[0] if goals else None

    async def mark_goal_reminded(self, goal_id: int, at: datetime):
        await self.conn.execute("UPDATE throwing_goals SET last_reminder_at = ? WHERE id = ?", (_utc(at), goal_id))
        await self.conn.commit()

    async def mark_goal_groups(self, goal_id: int, cycle: int):
        """Groups for this cycle are dealt with (used when there was nobody to make groups from)."""
        await self.conn.execute("UPDATE throwing_goals SET groups_cycle = ? WHERE id = ?", (cycle, goal_id))
        await self.conn.commit()

    async def delete_goal(self, goal_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM throwing_goals WHERE id = ?", (goal_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_groups(self, *, include_ended: bool = False, group_id: int | None = None) -> list[dict]:
        """Groups with their member IDs and the minutes and sessions that count for them, newest
        first. Without include_ended, only groups that haven't ended yet."""
        where, params = "1", []
        if group_id is not None:
            where, params = "g.id = ?", [group_id]
        elif not include_ended:
            where, params = "g.ends_at > ?", [_utc(datetime.now(timezone.utc))]
        async with self.conn.execute(
            "SELECT g.id, g.name, g.starts_at, g.ends_at, g.count_solo, g.require_all, g.created_by, "
            "(SELECT group_concat(discord_id) FROM throwing_group_members WHERE group_id = g.id) AS members, "
            "(SELECT COALESCE(SUM(minutes), 0) FROM session_groups WHERE group_id = g.id AND counts) AS minutes, "
            "(SELECT COUNT(*) FROM session_groups WHERE group_id = g.id AND counts) AS sessions "
            f"FROM throwing_groups g WHERE {where} ORDER BY g.starts_at DESC, g.id",
            params,
        ) as cur:
            rows = [dict(row) for row in await cur.fetchall()]
        for row in rows:
            row["members"] = [int(i) for i in row["members"].split(",")] if row["members"] else []
            row["count_solo"], row["require_all"] = bool(row["count_solo"]), bool(row["require_all"])
        return rows

    async def get_group(self, group_id: int) -> dict | None:
        rows = await self.list_groups(group_id=group_id)
        return rows[0] if rows else None

    async def update_group(
        self, group_id: int, *, name: str, starts_at: datetime, ends_at: datetime, count_solo: bool, require_all: bool
    ) -> bool:
        cur = await self.conn.execute(
            "UPDATE throwing_groups SET name = ?, starts_at = ?, ends_at = ?, count_solo = ?, require_all = ? WHERE id = ?",
            (name, _utc(starts_at), _utc(ends_at), int(count_solo), int(require_all), group_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def end_group(self, group_id: int) -> bool:
        """End a group now. False if it doesn't exist or has already ended."""
        now = _utc(datetime.now(timezone.utc))
        cur = await self.conn.execute(
            "UPDATE throwing_groups SET ends_at = ? WHERE id = ? AND ends_at > ?", (now, group_id, now)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def add_group_member(self, group_id: int, discord_id: int) -> bool:
        """False if they were already in it."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO throwing_group_members (group_id, discord_id) VALUES (?, ?)", (group_id, discord_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_group_member(self, group_id: int, discord_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM throwing_group_members WHERE group_id = ? AND discord_id = ?", (group_id, discord_id)
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def delete_group(self, group_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM throwing_groups WHERE id = ?", (group_id,))
        await self.conn.commit()
        return cur.rowcount > 0
