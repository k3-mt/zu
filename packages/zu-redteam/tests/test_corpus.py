"""The frozen corpus must replay deterministically, and on a safe runtime every
attack's envelope must hold (no observer fires) — that is what "survives" means."""

from __future__ import annotations

from zu_redteam.corpus import CORPUS_OBJECTIVES, build_corpus
from zu_redteam.harness import run_scenario
from zu_redteam.verdict import NeighbourHealth, default_observers

SECRET = "ZU-PLANTED-SECRET-corpus"


def test_corpus_is_stable_and_covers_the_objectives() -> None:
    ids = [c.id for c in build_corpus(SECRET)]
    assert ids == [
        "injected_page", "metadata_ssrf", "output_smuggle",
        "schema_bomb", "forged_event", "injected_judge", "pdf_phone_home",
    ]
    covered = {c.objective for c in build_corpus(SECRET)}
    assert covered == set(CORPUS_OBJECTIVES)


async def test_every_corpus_attack_envelope_holds_on_safe_runtime() -> None:
    for case in build_corpus(SECRET):
        run = await run_scenario(case.scenario)
        observers = [*default_observers(), NeighbourHealth(case.scenario.neighbours)]
        breaches = [b for o in observers if (b := o.inspect(run)) is not None]
        assert not breaches, f"{case.id} unexpectedly breached: {breaches}"
        # The planted secret must never appear anywhere the attack could reach it.
        assert run.result is None or SECRET not in str(run.result.value)
