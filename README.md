# void-bot

Discord bot (discord.py 2.x), run with Docker.

## Setup

1. Create an app at https://discord.com/developers/applications, add a bot, copy the token.
   Under **Bot → Privileged Gateway Intents**, enable **Server Members Intent** and **Message Content Intent**. The bot won't start without them.
2. Invite it with the `bot` and `applications.commands` scopes.
3. `cp .env.example .env` and fill in the values.

## Run on the VPS

```sh
docker compose up -d --build
docker compose logs -f
```

Update after changing code: `docker compose up -d --build`. The code is copied into the image when it's built, so a plain restart keeps running the old code. After changing only `.env`, `docker compose up -d` is enough.

## Automatic deploys

Every push to `main` runs `.github/workflows/deploy.yml`, which SSHes into the VPS, runs `git pull --ff-only` in the repo checkout, then `docker compose up -d --build`. It can also be run by hand from the **Actions** tab.

One-time setup:

1. On the VPS, clone the repo and create `.env` there. The checkout must be able to `git pull` without a prompt (a public repo, or a deploy key).
2. Create a key pair for deploys (`ssh-keygen -t ed25519 -f deploy_key -N ""`) and add `deploy_key.pub` to `~/.ssh/authorized_keys` for the VPS user. That user must be able to run `docker` without `sudo`.
3. Under **Settings → Secrets and variables → Actions**, add these repository secrets:

| Secret | Value |
| --- | --- |
| `VPS_HOST` | VPS hostname or IP |
| `VPS_USER` | SSH user |
| `VPS_SSH_KEY` | Contents of the private `deploy_key` file |
| `VPS_PATH` | Absolute path of the repo checkout on the VPS |
| `VPS_PORT` | Optional. SSH port, defaults to 22 |

## Local dev (Python 3.10+)

```sh
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
set -a && source .env && set +a
python -m bot
```

## Player database

Players are stored in SQLite at `/app/data/players.db`, kept in the `bot-data` Docker volume. The data survives rebuilds and restarts. Only `docker compose down -v` deletes it.

Only server administrators and members with a role listed in `ADMIN_ROLE_IDS` can run these commands. Everyone else gets a "no permission" reply. The replies are only visible to the person who ran the command.

To also hide the commands from everyone else, go to **Server Settings → Integrations → the bot**, turn off `@everyone`, and allow your admin roles. That setting only controls who sees the commands. The bot enforces `ADMIN_ROLE_IDS` either way.

| Command | What it does |
| --- | --- |
| `/player import-role role` | Create empty records for everyone with a role. Bots and people already in the database are skipped. |
| `/player export [incomplete_only]` | Download players as a CSV spreadsheet |
| `/player import-csv file` | Create or update players from a CSV |
| `/player add` | Add one member. Their details are optional. |
| `/player get` | Show one player's info |
| `/player list [incomplete_only]` | List players, or only those with missing details |
| `/player update member` | Opens a form with the player's current details. Edit the boxes and submit; emptying a box clears that field. |
| `/player nickname member [nickname]` | Set what teammates call someone, so throwing reports can use it. Separate several with commas. Leave `nickname` out to clear it. |
| `/player remove` | Delete a player's record |
| `/player clear` | Delete every player, after typing `DELETE` in a confirmation popup. The reply includes a CSV backup. |

### Bulk-filling details

1. `/player import-role` for each role you want as players.
2. `/player export` to get the spreadsheet, then fill in the columns in Excel or Google Sheets.
3. Save it as CSV and upload it with `/player import-csv`.

How the import works:
- People are matched by `discord_id`, or by `discord_username` if the ID doesn't match anyone. Spreadsheet apps round long IDs, so keep the username column.
- Blank cells leave the existing value unchanged.
- Anyone who isn't in the database yet gets a new record.
- Headers are flexible: `First Name`, `Penn ID`, `Phone Number` and similar spellings all work.
- If any row has a problem, nothing is imported and the bot lists what to fix.

If Penn IDs start with 0, format that column as text before typing them in. Otherwise the spreadsheet drops the leading zero.

Back up the database (safe while the bot is running):

```sh
docker compose exec bot python -c "import sqlite3; sqlite3.connect('data/players.db').backup(sqlite3.connect('data/backup.db'))"
docker compose cp bot:/app/data/backup.db ./players-backup.db
```

## Groups

`/group create` opens a popup where you name the group and pick up to 25 members. The bot creates a private text channel with that name that only you, the people you picked and the bot can see, then posts a welcome message mentioning everyone. Tick **Include names and phone numbers** to list each member's name and phone number from the player database next to their @. The bot tells you privately who has nothing on file. Like `/player`, only server admins and `ADMIN_ROLE_IDS` roles can use it.

The bot keeps a list of every channel `/group create` makes (the `group_channels` table), and the commands below only work on channels in that list, so they can't touch a channel the bot didn't create. Who is in a group isn't stored: it's the people named in the channel's permissions.

| Command | What it does |
| --- | --- |
| `/group list [include_archived]` | The bot's groups, with their members and anyone visiting |
| `/group add group member` | Add someone. The bot says so in the channel, since Discord doesn't tell people when they're let in. |
| `/group remove group member` | Remove someone |
| `/group join group [minutes]` | Let yourself in for 5 minutes (or up to 60), for example to post a message. The bot takes you out when the time is up, even if it was restarted in between. Running it again moves the end time. |
| `/group archive group` | Rename the channel to `archived-<name>`, move it to the archive category, and stop new messages. Members can still read it. |
| `/group adopt channel` | Put a channel the bot made before it kept a list into the list. Only works if the channel still has the "Group created by" topic the bot gave it. |

The `group` option autocompletes from the bot's list as you type, so you can pick a group you can't see. Archived groups can't be changed or joined.

Archived channels go to `GROUP_ARCHIVE_CATEGORY_ID` if it's set. Otherwise the bot creates a private **Archived groups** category. A Discord category holds 50 channels, so when one is full the bot makes **Archived groups 2**, and so on.

The bot's role needs **Manage Channels** and **Manage Roles** for these (Discord requires the second to change who can see a channel). Set `GROUP_CATEGORY_ID` in `.env` to put group channels under a category.

## Throwing sessions

Set `THROWING_CHANNEL_ID` and `ANTHROPIC_API_KEY` in `.env`. The bot does nothing in the channel until someone @mentions it there. It doesn't read or keep messages in the meantime, and it ignores mentions anywhere else.

When it's tagged:

1. It fetches the last hour of the channel (up to 200 messages) and Claude Haiku 4.5 picks out the throwing reports that aren't logged yet. Examples: "threw 45 min with" followed by an @mention or a name, or "hour and a half of hucks yesterday". The tag can be the report itself, or just a nudge to pick up earlier ones.
2. **Complete reports** are saved and the bot reacts on the report with the `:void_throw_<minutes>:` emoji, or ✅ if that emoji hasn't been uploaded. It posts nothing in the channel. Minutes are rounded to the nearest unit there's an emoji for, and the rounded number is what's saved: every 5 minutes up to 60, every 10 up to 120, every 15 up to 180. So 42 is logged as 40, 65 as 70, and anything over 180 as 180.
3. **Incomplete reports:** if the minutes are missing, or a name is ambiguous (it matches two people) or unknown, the bot starts a thread on the report and asks there, mentioning the reporter silently. A report that already has a thread doesn't get a second one.
4. **Answers:** the bot reads messages posted in those threads, without needing a tag. Once the report is complete it's logged and the thread is deleted. It asks at most 3 questions per report. Saying "cancel" drops it; that thread archives itself after an hour.

**Solo throwing.** A report that names nobody else ("threw 45") is a solo session for the reporter. The bot doesn't ask who they threw with.

**People the bot can't place.** It only knows people in the server, so a name can belong to someone it will never find. It asks who they mean, at most twice. If the reporter says the person isn't in the server, or two questions don't settle it, the session is logged without that person: solo, if nobody else was named.

**The same session reported twice.** Before logging a complete report, the bot looks for a session logged within 12 hours of it that either includes someone the reporter says they threw with, or that somebody else logged the reporter in. If there is one, it asks in a thread whether this is the same session. On yes, the report's people are added to that session, which keeps its original minutes and time, and nothing new is logged. On no, it's logged as its own session. Replying "I was there too" to a report that's already logged adds you to it without a question. Your own earlier sessions never trigger the question, so throwing twice in a day is fine. A report isn't logged until the question is answered.

A report nobody tags the bot about within an hour isn't picked up. If something goes wrong, the bot reacts ⚠️ on the message that triggered it and logs the error.

The reporter is counted as a participant unless they say otherwise. Claude looks names up with a `find_members` tool that matches display names, server nicknames, usernames, and the names and nicknames in the player database (`/player nickname`); it can't see emails, phone numbers or Penn IDs. `CLAUDE_MODEL` changes the model.

The bot's role needs these permissions in the channel: **View Channel**, **Read Message History**, **Add Reactions**, **Create Public Threads**, **Send Messages in Threads** and **Manage Threads** (to delete a thread once its report is logged; without it the thread is left to archive).

The emoji come from `scripts/make_emotes.py`, which draws a flying disc with the minutes next to it for 5 to 180 minutes: `pipenv run pip install pillow`, `pipenv run python scripts/make_emotes.py`, then upload the PNGs in `emotes/` either to the bot's application (Developer Portal → your app → **Emojis**) or to the server (**Server Settings → Emoji**). Discord names each emoji after its file. The bot looks in the server first, then the application, and reads the application's list once at first use, so restart it after uploading more. It works without them.

Tables: `throwing_sessions` (UUID `id`, `occurred_at`, `minutes`, `description`, `reported_by`, `source_message_id`) and `session_participants` (`session_id`, `discord_id`). `session_reports` maps a later report's message ID to the session it was added to. `report_threads` maps a report's message ID to the open question thread about it, and holds IDs only.

### Asking about throwing

`/throwing query question` answers a plain-English question about the logged sessions, for example "who has under 100 minutes this week?" or "how many sessions do" two named people "have this week compared to each other?". Like `/player`, only server admins and `ADMIN_ROLE_IDS` roles can use it, and the answer is only visible to the person who asked. It needs `ANTHROPIC_API_KEY`, but not `THROWING_CHANNEL_ID`.

- Claude (`CLAUDE_MODEL`) answers using four read-only tools: find members by name, total minutes and sessions per person over a date range, list sessions, and list throwing groups with their minutes. It never writes SQL, each tool runs one fixed query, and nothing can add, change or delete a session.
- "Everyone" means the roster: people in the player database who are still in the server, including those with nothing logged, plus anyone else with logged minutes. Run `/player import-role` first, or people with zero minutes can't show up.
- Weeks start on Monday. A question with no time range is answered for all time.
- The tools can't see emails, phone numbers or Penn IDs.

### Throwing groups

A throwing group is a set of members with a life: a start date and an end date. `/throwing-mgr` manages them. Like `/player`, only server admins and `ADMIN_ROLE_IDS` roles can use it, and it doesn't need `ANTHROPIC_API_KEY`.

`/throwing-mgr creategroups` opens a form:

- **Pool:** roles and/or individual people. Everyone with a picked role is included; bots are left out.
- **Exclude:** roles and/or individual people to leave out, even if they're in the pool. Optional.
- **Group size**.
- **Dates:** the first and last day, both included, as `YYYY-MM-DD to YYYY-MM-DD`. They share one box because a Discord form holds five fields at most.
- **Allow solo groups:** if one person is left over, they get a group of their own. Otherwise they join another group, making it one bigger. Two or more left over always form a smaller group.
- **Group minutes count solo throwing** and **Group minutes require all members:** see the rules below.
- **End all other groups:** every group that's running or yet to start ends when you confirm.

You then get a preview only you can see, with **Confirm**, **Reshuffle** and **Cancel**. Confirm saves the groups and posts them in the channel you ran the command in, mentioning everyone silently.

| Command | What it does |
| --- | --- |
| `/throwing-mgr list [include_ended]` | Groups with their dates, members and minutes |
| `/throwing-mgr show group` | One group, with its rules |
| `/throwing-mgr edit group` | Change the name, dates or the two minutes settings in a form |
| `/throwing-mgr add-member group member` | Add someone |
| `/throwing-mgr remove-member group member` | Remove someone |
| `/throwing-mgr end group` | End it now; it keeps its minutes |
| `/throwing-mgr delete group` | Delete it. Logged sessions aren't touched |

The `group` option autocompletes as you type an ID, a group name or a member's name.

**What counts towards a group's minutes.** A session is looked at for a group if it happened during the group's life and at least one member took part. Its minutes count once for the group, however many members were there:

- every member took part: always counts;
- two or more members, but not all: counts unless **require all members** is on;
- exactly one member, alone or with people outside the group: counts only if **count solo throwing** is on.

Nothing is stored when a session is logged. The `session_groups` view works this out from the sessions, the groups and their settings each time it's read (`session_id`, `group_id`, `members_present`, `group_size`, and `counts`), so editing a group, its members or a session can never leave it out of date. `/throwing query` and superadmin requests can both see groups. Tables: `throwing_groups` and `throwing_group_members`.

### Throwing goals

A goal is a number of minutes each person in a pool should throw every cycle, repeating until it's deleted. `/throwing-mgr goal` manages them, with the same permissions as the rest of `/throwing-mgr`.

`/throwing-mgr goal create` is two forms, because a Discord form holds five fields. Nothing is saved until the second one is submitted.

1. **Who and how much:** Name, Minutes (per person, per cycle), Pool and Exclude (roles and/or people, as in `creategroups`), and the Channel that reminders and new groups are posted in.
2. **Schedule and groups**, opened by the button the first form leaves:
   - **First cycle starts:** a date and time, like `2026-10-04 20:00` or `10/4 8pm`. A date alone means midnight. It can be in the past.
   - **Repeats every:** any number of hours, days, weeks or months: `1 week`, `3 days`, `12 hours`, `1 month`.
   - **Reminders:** how long before each cycle ends to ping people who are short, like `2d, 6h` (days, hours or minutes). Blank for none.
   - **Group size** and **Group settings:** fill in a size to have new random throwing groups made from the pool at the start of every cycle. Leave it blank for none. These are the same fields `creategroups` uses.

| Command | What it does |
| --- | --- |
| `/throwing-mgr goal list` | The goals, with their current cycle, pool, reminders and group settings |
| `/throwing-mgr goal progress goal [cycles_ago]` | Everyone's minutes in a cycle and who reached it. The current cycle by default; `cycles_ago: 1` is the one before |
| `/throwing-mgr goal history goal [cycles]` | The last few cycles (8 by default): how many people reached it each time, and each person's minutes per cycle |
| `/throwing-mgr goal remind goal` | Send the reminder now |
| `/throwing-mgr goal delete goal` | Delete it. Sessions and the groups it made aren't touched |

How it works:

- **Progress** is a person's total minutes from every session they took part in during the cycle, alone or with anyone. Group rules don't come into it.
- **Goals count retroactively.** Nothing is stored per cycle: progress is always read from the logged sessions. So a goal whose first cycle starts in the past has every cycle since then, and `progress` and `history` show them. Sessions logged late ("threw yesterday") land in the cycle they happened in. Starting in the past doesn't send old reminders or make groups for cycles that are over.
- **The pool is looked up each time**, so someone given a pool role later is included from then on, and someone who loses it or leaves drops out. Past cycles are shown for today's pool; who was in it back then isn't recorded.
- **Reminders** ping, in the goal's channel, everyone who is short, with their minutes so far. If everyone has reached it, nothing is posted. If the bot was down at a reminder time, it's sent when the bot comes back if that's within 3 hours, and skipped otherwise.
- **Groups** made for a cycle live for exactly that cycle, are posted silently in the goal's channel, and show up in `/throwing-mgr list` like any others. A goal created part-way through a cycle makes that cycle's groups straight away.
- The bot checks goals once a minute. It needs **View Channel** and **Send Messages** in the goal's channel.

**Daylight saving.** Times are on the clock in `TIMEZONE`. A cycle that repeats in days, weeks or months starts at the same time of day all year: one that starts Sunday at 20:00 starts every Sunday at 20:00, and the week the clocks change is an hour shorter or longer. A reminder given in days works the same way (`1d` before a 20:00 end is 20:00 the day before). Cycles and reminders given in hours or minutes are real elapsed time, so a `12 hours` cycle shifts by an hour on the clock when it changes. A start time that doesn't exist on the night the clocks go forward (02:30) happens an hour later that one night; one that happens twice when they go back (01:30) uses the first. A monthly goal that starts on the 31st uses the last day of shorter months and goes back to the 31st after.

A goal can't be edited: delete it and make it again, which loses nothing, since progress comes from the sessions. Tables: `throwing_goals` and `throwing_goal_targets`; a goal's group settings are stored as JSON in `throwing_goals.group_config`.

## Superadmin requests

Set `SUPERADMIN_ROLE_ID` and `SUPERADMIN_MANAGEMENT_CHANNEL_ID` in `.env` (and `ANTHROPIC_API_KEY`). Someone with that role can then @mention the bot in that channel and ask for a change to the database in plain English, such as purging players who have left the server, or changing a session's minutes and removing someone from it. Server administrators without the role can't use it.

1. Claude (`SUPERADMIN_MODEL`, default `claude-opus-5-5`) looks at the database and writes the SQL. It can run any `SELECT`, on every table and column. It can also see who is currently in the server.
2. The bot replies with what will change, the SQL, how many rows each statement would change (from a dry run that is rolled back), and **Confirm** and **Cancel** buttons.
3. Nothing changes until a superadmin presses Confirm. Any superadmin can press it, not only the one who asked. Each plan runs at most once: the first press removes the buttons, and they expire after 10 minutes or when the bot restarts.
4. On Confirm the bot copies the database to `data/backups/before-superadmin-<time>.db` (the newest 10 are kept), then runs the statements in one transaction. If any statement fails, none apply.

If the request is unclear, the bot asks instead of offering buttons; reply to its message to answer. The only limit on the SQL is that it must be `INSERT`, `UPDATE` or `DELETE`: it can change or delete any rows, but not the tables themselves. Requests, the SQL and who confirmed are written to the log.

To undo a run, stop the bot and copy the backup over the database:

```sh
docker compose stop
docker compose run --rm --entrypoint sh bot -c 'cp data/backups/<file>.db data/players.db && rm -f data/players.db-wal data/players.db-shm'
docker compose up -d
```

The bot needs **View Channel**, **Send Messages** and **Read Message History** in the management channel. Keep the channel private to superadmins: plans and answers can include anything in the database.

## Error channel

Set `ERRORS_CHANNEL_ID` in `.env` and the bot posts everything it logs at error level to that channel, from any part of the bot: failed throwing scans, Claude API failures, slash-command crashes and uncaught exceptions, with the traceback. Warnings and info lines stay in `docker compose logs` only, and so does anything that goes wrong before the bot connects (a bad token, a missing variable) or while Discord is unreachable. During a burst of errors it posts about one a second and drops anything past 50 waiting.

The bot needs **View Channel** and **Send Messages** there. Keep the channel admin-only, since tracebacks can include IDs and other details.

## Log channel

Set `LOGS_CHANNEL_ID` in `.env` and the bot posts every line it logs to that channel, in the same format as `docker compose logs`: info, warnings and errors, from the bot and from discord.py. Lines are batched into one message about every second. Errors still go to the error channel as well, if that's set.

It only carries what goes through Python's logging once the bot is connected. Startup lines logged before then are held and posted on connect, but anything printed straight to the console (a crash before logging starts, a missing variable, a failure to post to these channels) stays in `docker compose logs` only. If the bot logs faster than it can post, it keeps up to 1000 lines waiting, drops the rest, and says how many it dropped.

The bot needs **View Channel** and **Send Messages** there. Keep the channel private to the people who run the bot: the log includes members' names and IDs, `/throwing query` questions, name lookups, and superadmin requests with their SQL, which can contain emails, phone numbers and Penn IDs.

## Adding a command

Drop a new file in `bot/cogs/` with a `Cog` class and an `async def setup(bot)` function (see `groups.py` for a small one). Every module in that folder is loaded automatically, and slash commands sync on startup.
