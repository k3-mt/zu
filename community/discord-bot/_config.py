"""Pure, dependency-free config helpers for the Zeke Discord bot.

Kept free of the ``discord`` import on purpose: the tiny bits of startup logic that
are easy to get wrong (parsing ``GUILD_ID``, deciding whether to re-sync slash
commands) live here so they can be unit-tested offline at $0 without discord.py or a
network — the community bot is not part of the zu workspace, so importing ``bot`` in a
test would pull in discord.py, which the hermetic suite does not install.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

log_prefix = "zeke.config"


def parse_guild_id(raw: str | None) -> int | None:
    """Parse ``GUILD_ID`` into an int, tolerating a missing/malformed value.

    A blank or unset value means "no guild scoping" (global commands) → ``None``.
    A non-integer value (typo, quotes, stray text) previously crashed startup with an
    unhandled ``ValueError`` from ``int(...)``; here it is reported and downgraded to
    ``None`` (fall back to global) instead of taking the process down.

    Returns ``None`` when guild scoping should be skipped, else the parsed id.
    Raises nothing.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        # Malformed GUILD_ID: don't crash the bot — skip guild-scoping, log why.
        import logging

        logging.getLogger(log_prefix).warning(
            "GUILD_ID=%r is not an integer; ignoring it and registering commands "
            "globally instead.",
            raw,
        )
        return None


def _truthy(value: str | None) -> bool:
    """Interpret a string env flag as a boolean (unset/empty → False)."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def should_sync_commands(env: Mapping[str, str] | None = None) -> bool:
    """Decide whether to (re-)sync slash commands on this startup.

    A global ``tree.sync()`` on every boot burns the (low) global command-sync rate
    limit — a restart loop can get the bot rate-limited. So the global sync is gated:

    * If ``GUILD_ID`` is set, a guild-scoped sync is instant and cheap — always sync.
    * Otherwise (global scope), sync only when explicitly opted in via
      ``ZEKE_SYNC_COMMANDS`` being truthy. Commands rarely change between restarts, so
      the default (no sync on a plain restart) keeps a crash-loop off the rate limit.

    Returns ``True`` when a sync should run this startup.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    if parse_guild_id(environ.get("GUILD_ID")) is not None:
        return True  # guild-scoped sync is instant; safe to run every time.
    return _truthy(environ.get("ZEKE_SYNC_COMMANDS"))
