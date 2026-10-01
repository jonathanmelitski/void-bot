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

The bot's role needs the **Manage Channels** permission for this. Set `GROUP_CATEGORY_ID` in `.env` to put group channels under a category.

## Throwing sessions

Set `THROWING_CHANNEL_ID` and `ANTHROPIC_API_KEY` in `.env`. The bot does nothing in the channel until someone @mentions it there. It doesn't read or keep messages in the meantime, and it ignores mentions anywhere else.

When it's tagged:

1. It fetches the last hour of the channel (up to 200 messages) and Claude Haiku 4.5 picks out the throwing reports that aren't logged yet. Examples: "threw 45 min with" followed by an @mention or a name, or "hour and a half of hucks yesterday". The tag can be the report itself, or just a nudge to pick up earlier ones.
2. **Complete reports** are saved and the bot reacts on the report: with the server's `:void_throw_<minutes>:` emoji if there is one for exactly that many minutes (`:void_throw_45:` for 45), otherwise ✅. It posts nothing in the channel.
3. **Incomplete reports:** if the minutes are missing, or a name is ambiguous (it matches two people) or unknown, the bot starts a thread on the report and asks there, mentioning the reporter silently. A report that already has a thread doesn't get a second one.
4. **Answers:** the bot reads messages posted in those threads, without needing a tag. Once the report is complete it's logged and the thread is deleted. It asks at most 3 questions per report. Saying "cancel" drops it; that thread archives itself after an hour.

A report nobody tags the bot about within an hour isn't picked up. If something goes wrong, the bot reacts ⚠️ on the message that triggered it and logs the error.

The reporter is counted as a participant unless they say otherwise. Claude looks names up with a `find_members` tool that matches display names, nicknames, usernames and the names in the player database; it can't see emails, phone numbers or Penn IDs. `CLAUDE_MODEL` changes the model.

The bot's role needs these permissions in the channel: **View Channel**, **Read Message History**, **Add Reactions**, **Create Public Threads**, **Send Messages in Threads** and **Manage Threads** (to delete a thread once its report is logged; without it the thread is left to archive).

The emoji come from `scripts/make_emotes.py`, which draws a flying disc with the minutes next to it for 5 to 180 minutes: `pipenv run pip install pillow`, `pipenv run python scripts/make_emotes.py`, then upload the PNGs in `emotes/` under **Server Settings → Emoji**. Discord names each emoji after its file. The bot works without them.

Tables: `throwing_sessions` (UUID `id`, `occurred_at`, `minutes`, `description`, `reported_by`, `source_message_id`) and `session_participants` (`session_id`, `discord_id`). `report_threads` maps a report's message ID to the open question thread about it, and holds IDs only. There's no command for viewing totals yet.

## Error channel

Set `ERRORS_CHANNEL_ID` in `.env` and the bot posts everything it logs at error level to that channel, from any part of the bot: failed throwing scans, Claude API failures, slash-command crashes and uncaught exceptions, with the traceback. Warnings and info lines stay in `docker compose logs` only, and so does anything that goes wrong before the bot connects (a bad token, a missing variable) or while Discord is unreachable. During a burst of errors it posts about one a second and drops anything past 50 waiting.

The bot needs **View Channel** and **Send Messages** there. Keep the channel admin-only, since tracebacks can include IDs and other details.

## Adding a command

Drop a new file in `bot/cogs/` with a `Cog` class and an `async def setup(bot)` function (see `ping.py`). Every module in that folder is loaded automatically, and slash commands sync on startup.
