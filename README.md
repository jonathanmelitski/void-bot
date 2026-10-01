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

Set `THROWING_CHANNEL_ID` and `ANTHROPIC_API_KEY` in `.env`. The channel stays a normal conversation, and the bot quietly picks out the reports. Examples: "threw 45 min with @Sam and Max", or "hour and a half of hucks with Jess yesterday".

- **The transcript:** the bot keeps the last hour of the channel in memory. It includes the bot's own replies and any private back-and-forth, and each message is marked if it's been logged, asked about, or dropped. On startup, the bot reloads the last hour from the channel's history and uses the database to see what was already logged.
- **Reading it:** after a few seconds with no new messages, Claude Haiku 4.5 reads the whole transcript and returns only the reports that haven't been logged. Because it sees the conversation, "Nice job!" under a report counts as a reaction, and "me too, 30 min" as part of the report.
- **Complete reports** are saved and the bot reacts ✅. It only posts a reply if the report asks the bot something it can answer. Questions meant for teammates don't count.
- **Incomplete reports:** if the minutes are missing, or a name is ambiguous (two Maxes) or unknown, the bot DMs the reporter a question. If their DMs are closed, it asks in the channel.
  - The answer, whether in DM or in the channel, goes into the transcript for the next pass.
  - After 3 questions without an answer that settles it, the bot gives up. Saying "cancel" drops the report.
  - Anything older than an hour falls out of the transcript.
- **Errors** are logged, and the next message in the channel triggers a retry.

The reporter is counted as a participant unless they say otherwise. Claude looks names up with the same `find_members` tool the @mention assistant uses. It matches display names, nicknames, usernames and the names in the player database. If a name matches several people or nobody, the bot asks the reporter. `CLAUDE_MODEL` changes the model.

Tables: `throwing_sessions` (UUID `id`, `occurred_at`, `minutes`, `description`, `reported_by`, `source_message_id`) and `session_participants` (`session_id`, `discord_id`).

## Asking Void Bot

@mention the bot in any channel it can read to ask about logged throwing, or to log a session. Replying to one of its answers continues the conversation.

- "how much did @Max throw this week?"
- "who threw the most in September?"
- "what sessions did I do yesterday?"
- "log 30 min for me and Sam, break throws"

Claude (`CLAUDE_MODEL`) answers using four fixed tools: find members by name, total minutes per person over a date range, list sessions, and log a session. It never writes SQL. Each tool runs one parameterized query, and every argument is validated before it reaches the database. The tools can't see emails, phone numbers or Penn IDs, and can't edit or delete sessions. Anyone can ask or log, the same as posting in the throwing channel. A manual log is tied to the message that asked for it, so one message can't log the same session twice. The throwing-channel logger skips messages that mention the bot.

## Adding a command

Drop a new file in `bot/cogs/` with a `Cog` class and an `async def setup(bot)` function (see `ping.py`). Every module in that folder is loaded automatically, and slash commands sync on startup.
