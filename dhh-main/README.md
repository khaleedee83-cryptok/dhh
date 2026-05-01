# Telegram Moderator Bot

A production-oriented Telegram moderator bot built on `python-telegram-bot` with persistent SQLite state.

## What it does

- Join verification with a one-tap button and timed expiry
- Automatic moderation for blocked phrases, disallowed links, flood spam, duplicate spam, caps spam, emoji spam, and mention spam
- Progressive enforcement with warnings, auto-mute, and repeat-offender banning
- Manual admin controls for warn, mute, ban, approve, and trust management
- Raid mode with both manual activation and automatic trigger on burst joins
- Per-chat persistent settings and an audit trail

## Quick start

1. Create a bot with BotFather.
2. Add the bot to your group.
3. Promote it to admin with permission to:
   - delete messages
   - restrict members
   - ban users
   - read messages
4. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`.
   `ALLOWED_USER_IDS` defaults to `8151537237,7180897251,7626734289,7902074220`.
5. Install dependencies:

```bash
pip install -r requirements.txt
```

6. Start the bot:

```bash
python3 bot.py
```

## Railway

This repo is now prepared for Railway with:

- `Dockerfile`
- `railway.toml`
- `.dockerignore`

Deploy notes:

1. Push this folder to GitHub or your Railway-connected repo.
2. Create a Railway service from the repo.
3. Add `BOT_TOKEN` as an environment variable.
4. Keep `ALLOWED_USER_IDS` set to `8151537237,7180897251,7626734289,7902074220`.
5. Mount a persistent Railway volume to `/app/data`.
6. Leave `DB_PATH` as `data/moderator.db`, or point it to another mounted path.

Important:

- If you do not mount a volume, the SQLite database will be ephemeral and your warnings, trust state, verification queue, and audit log will reset on redeploy or restart.
- This bot should run as a single worker instance. Do not scale it horizontally while using SQLite.
- Railway does not need an HTTP port for this bot.

## Important commands

- `/settings`
- `/protect on|off`
- `/verify on|off`
- `/raid on [minutes]`
- `/raid off`
- `/links off|trusted|whitelist`
- `/setlog [off|chat_id]`
- `/blockword <phrase>`
- `/allowdomain <domain>`
- `/warn` by replying to a message
- `/mute [minutes] [reason]` by replying to a message
- `/ban [reason]` by replying to a message
- `/approve` by replying to a message
- `/logs`

## Link policy

- `off`: links are allowed
- `trusted`: only trusted members can post links
- `whitelist`: only configured allowed domains can be posted by regular users

Trusted members are managed with `/approve` and `/unapprove`.

## Data

The bot stores its state in SQLite at the path configured by `DB_PATH`. That includes:

- chat settings
- member warning and trust state
- recent message fingerprints for spam detection
- pending verifications
- audit log entries

## Notes

- Verification and raid mode are per chat.
- The bot expects to run continuously so timed verification expiry can be enforced.
- If your group has unusual default permissions, review how Telegram applies restored permissions after unmute or verification.
