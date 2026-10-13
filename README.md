# Telegram Dollar Bot

## Deploy on Railway with persistent PostgreSQL

1. Create a Railway project and deploy this GitHub repository as the bot service.
2. Add a **PostgreSQL** database to the same Railway project.
3. In the bot service's **Variables**, add:
   - `BOT_TOKEN`: a fresh Telegram bot token from `@BotFather`.
   - `DATABASE_URL`: a reference to the PostgreSQL service's connection URL, for example `${{Postgres.DATABASE_URL}}` (use your actual Railway database service name).
4. The app creates its `bot_state` table automatically on first startup. User records, balances, referrals, purchase/withdrawal records, counters, and admin settings are saved to PostgreSQL.
5. The start command is configured as `python main.py`. Keep only one running replica because Telegram long polling should run in a single instance.
6. Deploy, inspect Railway logs, and send `/start` to the bot. The configured admin can open the panel with `/banantak`.

## Environment variables

The database connection is read from one variable: `DATABASE_URL`. Telegram still requires the separate `BOT_TOKEN` variable to authenticate the bot. Do not put either secret in source code or commit them to GitHub.

## Data migration

On the first startup, if PostgreSQL is empty, the bot imports `data.json` from the working directory if such a legacy file exists, then persists it in PostgreSQL. After that, PostgreSQL is the source of truth; the container filesystem is not used for persistent bot data.

## Local run

```bash
python -m pip install -r requirements.txt
python main.py
``

Set `BOT_TOKEN` and `DATABASE_URL` in your environment before running.
