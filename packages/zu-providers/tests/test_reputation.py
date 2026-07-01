"""#84 — the deterministic merchant-trust scorer, proved offline.

The verdict is computed from hard-to-forge domain signals, never page content, so
it is injection-immune by construction. These tests pin the three design
principles: hard gates veto (strong negatives), forge-resistance weighting (strong
positives are hard-to-fake), and the two-axis split (malicious vs. real-shop).
"""

from __future__ import annotations

import pytest

from zu_core.ports import CAP_NET, ReputationProvider, ReputationVerdict
from zu_providers.reputation import (
    LIVE_SIGNAL_HOSTS,
    DeterministicReputationScorer,
    LiveSignalSource,
    StaticSignalSource,
    score_signals,
)


def _score(signals: dict, **kw) -> ReputationVerdict:
    prov = dict.fromkeys(signals, "test")
    return score_signals(signals, prov, **kw)


# --- hard gates: a veto → refuse regardless of score (strong negatives) --------


def test_blocklist_is_a_hard_gate() -> None:
    v = _score({"on_blocklist": True, "domain_age_days": 5000, "company_registry_match": True})
    assert v.band == "refuse" and v.gate == "blocklist" and v.score == 0


def test_no_valid_https_is_a_hard_gate() -> None:
    v = _score({"https_invalid": True, "review": {"count": 9999, "age_days": 9999}})
    assert v.band == "refuse" and v.gate == "no_valid_https"


def test_high_aggregator_risk_is_a_hard_gate_even_for_a_real_looking_shop() -> None:
    # The two-axis point: strong "real shop" signals do NOT rescue a domain the
    # malicious axis flags — a phishing clone of a real brand still refuses.
    v = _score(
        {"aggregator_risk": 85, "domain_age_days": 4000, "company_registry_match": True,
         "established_platform": True},
        aggregator_refuse_at=70,
    )
    assert v.band == "refuse" and v.gate == "aggregator_risk"


# --- forge-resistance weighting: strong positives are hard-to-fake -------------


def test_established_shop_scores_trusted() -> None:
    v = _score(
        {
            "https_valid": True,
            "domain_age_days": 3000,            # strong +20
            "company_registry_match": True,      # strong +15
            "review": {"count": 500, "age_days": 800},  # strong +15
            "wayback_depth": 120,                # +8
            "established_platform": True,        # +10
            "aggregator_risk": 0,
        }
    )
    assert v.band == "trusted" and v.score >= 70
    # The breakdown is auditable: value + applied weight per contributing signal.
    assert v.signals["domain_age_days"]["weight"] == 20
    assert v.provenance["company_registry_match"] == "test"


def test_bare_https_only_is_caution_not_trusted() -> None:
    # A weak positive (cheap to obtain) is necessary-not-sufficient.
    v = _score({"https_valid": True})
    assert v.band == "caution" and v.score < 70


def test_clean_but_unknown_shop_is_caution_two_axis() -> None:
    # The malicious axis is clean (no risk) but the real-shop axis is empty — a
    # clean-but-non-fulfilling scam passes malware checks yet must not be trusted.
    v = _score({"https_valid": True, "aggregator_risk": 0})
    assert v.band == "caution"


def test_clean_malware_checks_but_no_real_shop_axis_is_not_trusted() -> None:
    # Passes EVERY malware/aggregator check (no blocklist, clean aggregator) and
    # carries the cheap positives a scam can fake, but the real-shop axis is empty
    # (no registry, no review depth, very young) — the two-axis requirement means
    # this must NOT reach "trusted".
    v = _score(
        {
            "on_blocklist": False,
            "aggregator_risk": 0,
            "https_valid": True,
            "dmarc": True,
            "spf": True,
            "domain_age_days": 7,
        }
    )
    assert v.band != "trusted" and v.score < 70


def test_young_abused_tld_is_dragged_down() -> None:
    v = _score({"domain_age_days": 10, "abused_tld": True, "https_valid": True})
    assert v.band == "caution" and v.score < 30
    # And a deployment may opt to refuse very-low-trust domains outright.
    v2 = _score({"domain_age_days": 10, "abused_tld": True}, refuse_below=30)
    assert v2.band == "refuse" and v2.gate == "low_trust"


# --- the provider: gather signals from pluggable sources, then score -----------


async def test_provider_assesses_via_sources_and_records_provenance() -> None:
    src = StaticSignalSource(
        {"shop.com": {"https_valid": True, "domain_age_days": 3000,
                      "company_registry_match": True, "review": {"count": 300, "age_days": 700}}},
        name="catalogue",
    )
    scorer = DeterministicReputationScorer([src])
    v = await scorer.assess("shop.com")
    assert v.band == "trusted"
    assert v.provenance["domain_age_days"] == "catalogue"


async def test_unknown_domain_is_caution_never_a_silent_pass() -> None:
    scorer = DeterministicReputationScorer([StaticSignalSource({})])
    v = await scorer.assess("never-seen.example")
    assert v.band == "caution" and v.score == 50  # neutral base, nothing known


async def test_later_source_wins_on_a_signal_clash() -> None:
    cheap = StaticSignalSource({"d.com": {"on_blocklist": False}}, name="cheap")
    authoritative = StaticSignalSource({"d.com": {"on_blocklist": True}}, name="threat-intel")
    scorer = DeterministicReputationScorer([cheap, authoritative])
    v = await scorer.assess("d.com")
    assert v.band == "refuse" and v.gate == "blocklist"
    assert v.provenance["on_blocklist"] == "threat-intel"


def test_scorer_satisfies_the_port() -> None:
    assert isinstance(DeterministicReputationScorer([]), ReputationProvider)


# --- the live source: a network-gated stub with an honest capability envelope ---


def test_live_source_declares_its_capability_envelope() -> None:
    src = LiveSignalSource()
    assert src.capabilities == frozenset({CAP_NET})
    assert src.egress == LIVE_SIGNAL_HOSTS and "*" not in src.egress


async def test_live_source_refuses_offline_unless_a_fetcher_is_injected() -> None:
    # No `fetch` ⇒ it never opens a socket; the offline suite cannot reach the net.
    with pytest.raises(RuntimeError):
        await LiveSignalSource().collect("shop.com")


async def test_live_source_composes_with_the_scorer_via_an_injected_fetch() -> None:
    async def fake_fetch(domain: str, hosts: frozenset[str]) -> dict:
        assert hosts == LIVE_SIGNAL_HOSTS  # the declared envelope is what it reaches
        return {"domain_age_days": 3000, "company_registry_match": True,
                "review": {"count": 300, "age_days": 700}}

    scorer = DeterministicReputationScorer([LiveSignalSource(fetch=fake_fetch)])
    v = await scorer.assess("shop.com")
    assert v.band == "trusted" and v.provenance["domain_age_days"] == "live"
