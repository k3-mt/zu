"""The frozen corpus must replay deterministically, and on a safe runtime every
attack's envelope must hold (no observer fires) — that is what "survives" means."""

from __future__ import annotations

from zu_redteam.corpus import (
    CORPUS_OBJECTIVES,
    build_corpus,
    injection_reach_cases,
)
from zu_redteam.harness import run_scenario
from zu_redteam.verdict import (
    InjectionReachBreach,
    NeighbourHealth,
    default_observers,
)

SECRET = "ZU-PLANTED-SECRET-corpus"


def test_corpus_is_stable_and_covers_the_objectives() -> None:
    ids = [c.id for c in build_corpus(SECRET)]
    assert ids == [
        "injected_page", "confused_deputy", "metadata_ssrf", "output_smuggle",
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


# --- issue #3: per-host containment for open-egress tools ---------------------


async def test_injection_reach_breaches_even_under_open_egress() -> None:
    """The injection-reach dual (issue #3): a deputy that OBEYS an injected page and
    reaches the attacker sink under an OPEN-egress tool must FAIL — ``EgressBreach``
    waves the open-egress reach through, so only ``InjectionReachBreach`` catches it.
    This is the case the old gate mis-scored as 'contained'. It lives OUTSIDE
    build_corpus (it must breach), so the always-contained corpus stays green."""
    for case in injection_reach_cases(SECRET):
        run = await run_scenario(case.scenario)
        breach = InjectionReachBreach().inspect(run)
        assert breach is not None, f"{case.id} must breach — an injection-steered reach"
        assert "sink named by untrusted fetched content" in breach.detail
        assert "evil.example" in breach.detail


async def test_injected_page_stays_contained_without_reaching_the_sink() -> None:
    """The always-contained ``injected_page`` case must NOT fire InjectionReachBreach:
    the deputy reads the page as data and never reaches the injected sink, so no
    observer fires (proving the observer is not a blanket false-positive on any run
    that merely FETCHES a page naming a URL)."""
    case = next(c for c in build_corpus(SECRET) if c.id == "injected_page")
    run = await run_scenario(case.scenario)
    assert InjectionReachBreach().inspect(run) is None
