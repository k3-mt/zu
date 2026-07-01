"""Zeke — the Zu community Discord bot.

A small, dependency-light helper for the Zu server (the zebra mascot — "Zeke", distinct
from the "Zu" release/issue webhook). It greets new members and ships slash commands that
answer the questions newcomers ask over and over — where the docs are, how to run the
thing, and (the load-bearing one for this project) how to turn "Zu couldn't do X" into a
reported capability gap instead of a local hack.

Run it:

    cp .env.example .env        # then paste your bot token into .env
    pip install -r requirements.txt
    python bot.py

Config (environment):
  * DISCORD_BOT_TOKEN  — required. The bot token (Developer Portal → Bot → Reset Token).
  * GUILD_ID           — optional. A server id gives instant slash-command registration
                         during development; unset = normal (global) propagation.
  * WELCOME_CHANNEL    — optional. Channel name to greet new members in (default: general).

The new-member greeting needs the **Server Members** privileged intent — enable it at
Developer Portal → your app → Bot → Privileged Gateway Intents → Server Members Intent.
"""

from __future__ import annotations

import logging
import os

import discord
from _config import parse_guild_id, should_sync_commands
from discord import app_commands

try:  # Optional convenience: load .env if python-dotenv is installed.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("zeke")

REPO = "https://github.com/k3-mt/zu"
BRAND = 0x5865F2  # Discord blurple, matches the README badge.
WELCOME_CHANNEL = os.environ.get("WELCOME_CHANNEL", "general")


def _embed(title: str, description: str) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=BRAND)
    e.set_footer(text="Zeke · the Zu community helper")
    return e


def _channel_ref(guild: discord.Guild, name: str) -> str:
    """A clickable #channel mention if it exists, else a plain ``#name`` fallback."""
    channel = discord.utils.get(guild.text_channels, name=name)
    return channel.mention if channel else f"#{name}"


class ZekeBot(discord.Client):
    def __init__(self) -> None:
        # Default intents + members, so the bot can greet people as they join.
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        # Parse GUILD_ID defensively: a malformed value falls back to global scope
        # (a clear log line) instead of crashing startup with an unhandled ValueError.
        guild_id = parse_guild_id(os.environ.get("GUILD_ID"))
        # Gate the sync so a restart loop doesn't burn the global command rate limit.
        if not should_sync_commands():
            log.info(
                "Skipping slash-command sync this startup (no GUILD_ID and "
                "ZEKE_SYNC_COMMANDS not set). Commands rarely change between restarts; "
                "set ZEKE_SYNC_COMMANDS=1 to force a global re-sync."
            )
            return
        if guild_id is not None:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s (instant).", guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to an hour).")


client = ZekeBot()


@client.event
async def on_ready() -> None:
    log.info("Zeke is online as %s (id=%s).", client.user, getattr(client.user, "id", "?"))


@client.event
async def on_member_join(member: discord.Member) -> None:
    """Greet a new member and point them at the load-bearing channels + commands."""
    guild = member.guild
    channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL)
    if channel is None:
        return
    embed = _embed(
        f"Welcome to Zu, {member.display_name}! 🦓",
        "Glad you're here — Zu is a backend-agnostic runtime for production agents.\n\n"
        f"• {_channel_ref(guild, 'start-here')} — what Zu is, and the rules\n"
        f"• {_channel_ref(guild, 'help')} — ask anything; try `/run` and `/docs`\n"
        f"• {_channel_ref(guild, 'capability-gaps')} — hit a wall? `/gap` shows how to report it\n"
        f"• {_channel_ref(guild, 'show-and-tell')} — share what you build\n\n"
        f"The open runtime lives at <{REPO}>.",
    )
    try:
        await channel.send(content=member.mention, embed=embed)
    except discord.DiscordException as exc:  # missing perms / channel gone — never crash
        log.warning("Could not post welcome for %s: %s", member, exc)


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
