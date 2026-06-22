# Zeke — the Zu community Discord bot

**Zeke** (the zebra mascot — distinct from the "Zu" release/issue webhook) greets new
members and answers the questions newcomers ask repeatedly, so maintainers don't have to.

| Trigger | What it does |
|---------|--------------|
| *member joins* | Posts a welcome in `#general` pointing at `#start-here`, `#help`, `#capability-gaps`, and the `/run` `/docs` `/gap` commands |
| `/docs` | Links the construction-sequence guide, the flagship example, AGENTS.md, CONTRIBUTING |
| `/run`  | The 30-second offline quickstart (`uv sync` → `zu run --offline`) |
| `/gap`  | How to report a capability gap (`zu_report_gap` / new issue) — Zu's core discipline |

It's deliberately outside the `packages/*` uv workspace: the runtime core stays
SDK-free, and this bot is community infra, not part of the shipped product.

Identity to set in the Developer Portal (General Information): name **Zeke**, the 🦓 avatar
([`assets/zeke.svg`](assets/zeke.svg), export to PNG), description "Zeke — the Zu community
helper. Try /docs, /run, /gap.", tags *Developer Tools · Open Source · AI Agents · Utility*.

## Create the bot application

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
   → **New Application**.
2. **Bot** tab → **Reset Token** → copy it. This is your `DISCORD_BOT_TOKEN`.
   Under **Privileged Gateway Intents**, enable **Server Members Intent** (needed for
   the new-member welcome; the slash commands work without it).
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
keeps `python bot.py` alive works. Set `DISCORD_BOT_TOKEN` (and optionally
`GUILD_ID`/`WELCOME_CHANNEL`) as environment/secrets in your host — never bake the
token into the image.

- **Google Cloud (Always Free) — recommended:** an `e2-micro` VM under systemd.
  One command + a token over SSH. See [`deploy/gcp/`](deploy/gcp/README.md).
- **Any VPS:** the same [`deploy/zeke.service`](deploy/zeke.service) systemd unit —
  see the self-hosting note in the GCP guide.
- **Container hosts (Fly.io/Railway/etc.):** use the `Dockerfile` here; set the token
  as a secret. (Note: Cloud Run isn't a fit — a gateway bot can't run there for free.)

## Extending it

Each command is a function decorated with `@client.tree.command(...)` in
`bot.py`. Add a command by writing one more decorated coroutine. Keep responses
short and link into the repo rather than duplicating docs that will drift.
