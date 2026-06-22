# Zu community Discord bot

A small helper bot for the Zu Discord. It answers the questions newcomers ask
repeatedly with slash commands, so maintainers don't have to:

| Command | What it does |
|---------|--------------|
| `/docs` | Links the construction-sequence guide, the flagship example, AGENTS.md, CONTRIBUTING |
| `/run`  | The 30-second offline quickstart (`uv sync` → `zu run --offline`) |
| `/gap`  | How to report a capability gap (`zu_report_gap` / new issue) — Zu's core discipline |

It's deliberately outside the `packages/*` uv workspace: the runtime core stays
SDK-free, and this bot is community infra, not part of the shipped product.

## Create the bot application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application**.
2. **Bot** tab → **Reset Token** → copy it. This is your `DISCORD_BOT_TOKEN`.
   No privileged intents are required.
3. **Installation** (or **OAuth2 → URL Generator**): scopes `bot` and
   `applications.commands`. For permissions, **Send Messages** is enough.
4. Open the generated URL and invite the bot to your server.

## Run it locally

```bash
cd community/discord-bot
cp .env.example .env          # paste your token into DISCORD_BOT_TOKEN
pip install -r requirements.txt
python bot.py
```

Set `GUILD_ID` in `.env` to your server id while developing — slash commands
then register instantly instead of taking up to an hour to propagate globally.

## Run it in Docker

```bash
docker build -t zu-discord-bot .
docker run --rm -e DISCORD_BOT_TOKEN=your-token-here zu-discord-bot
```

## Hosting

The bot is a single long-running process with no inbound ports, so anything that
keeps a container or a `python bot.py` alive works: Railway, Fly.io, a small VPS
with systemd, or a free-tier always-on worker. Set `DISCORD_BOT_TOKEN` (and
optionally `GUILD_ID`) as environment variables/secrets in your host of choice —
do not bake the token into the image.

## Extending it

Each command is a function decorated with `@client.tree.command(...)` in
`bot.py`. Add a command by writing one more decorated coroutine. Keep responses
short and link into the repo rather than duplicating docs that will drift.
