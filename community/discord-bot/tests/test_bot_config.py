"""Offline ($0, no network, no discord.py) tests for the Zeke startup helpers.

Covers the two startup foot-guns fixed in the bot:

* F61 — a malformed ``GUILD_ID`` must NOT crash the bot with an unhandled ValueError.
* F65 — global slash-command sync must not run on every startup (rate-limit risk).
"""

from __future__ import annotations

from _config import parse_guild_id, should_sync_commands


class TestParseGuildId:
    def test_valid_integer(self) -> None:
        assert parse_guild_id("123456789012345678") == 123456789012345678

    def test_valid_integer_with_whitespace(self) -> None:
        assert parse_guild_id("  42  ") == 42

    def test_missing_is_none(self) -> None:
        assert parse_guild_id(None) is None

    def test_empty_is_none(self) -> None:
        assert parse_guild_id("") is None
        assert parse_guild_id("   ") is None

    def test_malformed_does_not_raise(self) -> None:
        # The old code did int(os.environ["GUILD_ID"]) → ValueError crash at startup.
        # These would all crash the old path; here they must return None, not raise.
        for bad in ("not-a-number", "123abc", '"123"', "12.5", "0x10"):
            assert parse_guild_id(bad) is None


class TestShouldSyncCommands:
    def test_no_guild_no_flag_skips_global_sync(self) -> None:
        # The restart-loop hazard: plain global scope with no opt-in → do NOT sync.
        assert should_sync_commands({}) is False

    def test_guild_scoped_always_syncs(self) -> None:
        # Guild-scoped sync is instant/cheap, so it's fine to run every startup.
        assert should_sync_commands({"GUILD_ID": "42"}) is True

    def test_malformed_guild_falls_back_and_skips(self) -> None:
        # Bad GUILD_ID degrades to global scope; without the opt-in flag → skip.
        assert should_sync_commands({"GUILD_ID": "nope"}) is False

    def test_opt_in_flag_forces_global_sync(self) -> None:
        for truthy in ("1", "true", "TRUE", "yes", "on"):
            assert should_sync_commands({"ZEKE_SYNC_COMMANDS": truthy}) is True

    def test_falsey_flag_skips(self) -> None:
        for falsey in ("0", "false", "no", "off", ""):
            assert should_sync_commands({"ZEKE_SYNC_COMMANDS": falsey}) is False
