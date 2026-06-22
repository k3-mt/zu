"""Zu community Discord bot.

A small, dependency-light helper for the Zu server. It ships a handful of slash
commands that answer the questions newcomers ask over and over — where the docs
are, how to run the thing, and (the load-bearing one for this project) how to
turn "Zu couldn't do X" into a reported capability gap instead of a local hack.

Run it:

    cp .env.example .env        # then paste your bot token into .env
    pip install -r requirements.txt
    python bot.py

The token is read from the DISCORD_BOT_TOKEN environment variable. Set GUILD_ID
to a server id during development for instant slash-command registration; leave
it unset in production for normal (global) propagation.
"""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands

try:  # Optional convenience: load .env if python-dotenv is installed.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("Zu")

REPO = "https://github.com/k3-mt/zu"
BRAND = 0x5865F2  # Discord blurple, matches the README badge.


def _embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=BRAND)


class ZuBot(discord.Client):
    def __init__(self) -> None:
        # No privileged intents needed — slash commands work with the default set.
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        guild_id = os.environ.get("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s (instant).", guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to an hour).")


client = ZuBot()


@client.event
async def on_ready() -> None:
    log.info("Logged in as %s (id=%s).", client.user, getattr(client.user, "id", "?"))


@client.tree.command(name="docs", description="Links to the Zu docs and the best starting points.")
async def docs(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=_embed(
            "🦓 Zu docs",
            "Start here:\n"
            f"• **Build an agent** — [construction sequence]({REPO}/blob/main/docs/agent-construction-sequence.md)\n"
            f"• **Flagship example** — [vet-appointment agent]({REPO}/tree/main/examples/agents/vet-appointment)\n"
            f"• **Navigate the repo** — [AGENTS.md]({REPO}/blob/main/AGENTS.md)\n"
            f"• **Contribute** — [CONTRIBUTING.md]({REPO}/blob/main/CONTRIBUTING.md)",
        )
    )


@client.tree.command(name="run", description="The 30-second 'how do I run Zu' quickstart.")
async def run(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=_embed(
            "Run Zu offline (no API keys, no network)",
            "```bash\n"
            "uv sync                 # editable install of every package\n"
            "uv run pytest           # the whole suite, hermetic\n"
            "zu init && zu capture <agent>   # one live run -> fixtures\n"
            "zu run <agent> --offline        # replay at ~$0, iterate freely\n"
            "```\n"
            f"Full guide: <{REPO}/blob/main/docs/agent-construction-sequence.md>",
        )
    )


@client.tree.command(
    name="gap",
    description="Hit a wall Zu couldn't clear? Here's how to report a capability gap.",
)
async def gap(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=_embed(
            "Report a capability gap",
            "If Zu can't do something, that's a **capability gap to fix upstream** — "
            "not something to hack around with a magic constant.\n\n"
            "• From a coding harness on the `zu mcp` server, call the **`zu_report_gap`** "
            "tool — it turns the gap into a reproducible issue.\n"
            f"• Or open one by hand: [new issue]({REPO}/issues/new/choose).\n\n"
            "Include what you asked the agent to do, what it did instead, and the run id "
            "if you have one.",
        )
    )


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and add your "
            "bot token (or export the variable)."
        )
    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
