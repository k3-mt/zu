"""The attacker: the scripted fleet replays the corpus; the live attacker is
opt-in only (CI never depends on a frontier model)."""

from __future__ import annotations

import pytest

from zu_redteam.attacker import FLEET, OBJECTIVES, CraftPayload, LiveAttacker, ScriptedAttacker


def test_fleet_is_well_formed_and_objectives_are_known() -> None:
    names = {s.name for s in FLEET}
    assert {"injector", "exfiltrator", "escaper", "exhauster", "judge_breaker"} <= names
    for s in FLEET:
        assert all(o in OBJECTIVES for o in s.objectives)


def test_craft_payload_encodings() -> None:
    assert CraftPayload.encode("secret", "base64") != "secret"
    assert CraftPayload.encode("a", "homoglyph") != "a"
    assert CraftPayload.encode("plain") == "plain"


async def test_scripted_attacker_runs_the_corpus() -> None:
    attacker = ScriptedAttacker(planted_secret="ZU-PLANTED-SECRET-x")
    results = await attacker.run()
    assert len(results) == 6
    assert len(set(attacker.objectives_attempted(results))) >= 5


async def test_live_attacker_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZU_REDTEAM_LIVE", raising=False)
    with pytest.raises(RuntimeError, match="opt-in"):
        await LiveAttacker(planted_secret="x").run()
