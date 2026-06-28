"""ReputationProvider reference impl — a deterministic merchant-trust scorer (#84).

When the AGENT picks the vendor, the system chooses *who gets the money*. This is
the reference :class:`zu_core.ports.ReputationProvider`: a **deterministic,
auditable** trust decision over external, **hard-to-forge** domain signals — never
the page's persuasive content, so it is injection-immune by construction.

The design principles from the issue are encoded executably:

* **Forge-resistance weighting** — a signal's weight tracks how expensive it is to
  fake. HTTPS-present / DMARC are weak positives (cheap to fake); domain age,
  third-party review depth+age, a company-registry match, archive history are
  strong (expensive to fake).
* **Asymmetry** — strong NEGATIVES, weak positives. The decisive signals are the
  hard gates; the positives are necessary-not-sufficient.
* **Hard gates (veto → REFUSE regardless of score)** — blocklist hit, no valid
  HTTPS, parked/suspended/sinkholed, or a high aggregator risk score.
* **Two axes** — the "is it malicious?" axis (``aggregator_risk``) AND the "is it a
  real shop that ships?" axis (registration, reviews, age, platform). A clean-but-
  non-fulfilling scam passes every malware check, so neither axis alone suffices.
* **Domain-level** — the verdict is keyed by registrable domain, cacheable, and
  immune to on-page manipulation.

The pure SCORING is here and fully offline/deterministic. The signal SOURCES that
fetch TLS/DNS/RDAP/blocklist facts need the network, so they are a pluggable seam
(:class:`SignalSource`); :class:`StaticSignalSource` supplies injected signals for
tests and composition, and real fetchers (Safe Browsing, RDAP age, Companies
House, Wayback) are future adapters behind the same shape — never baked into the
scorer, so the weights can be tested without a single network call.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from zu_core.ports import ReputationProvider, ReputationVerdict


@runtime_checkable
class SignalSource(Protocol):
    """A producer of hard-to-forge domain signals. ``name`` is the provenance
    label recorded per signal; ``collect`` returns ``{signal_name: value}`` for one
    registrable domain. A real source fetches (RDAP age, a blocklist lookup, a
    cert check); the scorer never fetches — it only weighs what sources return."""

    name: str

    async def collect(self, domain: str) -> dict[str, Any]: ...


class StaticSignalSource:
    """A :class:`SignalSource` over a fixed ``{domain: {signal: value}}`` map — the
    offline/test analog of the network fetchers. Unknown domains return ``{}`` (no
    signals), which the scorer treats as "nothing known" (a CAUTION, never a silent
    pass)."""

    def __init__(self, signals: dict[str, dict[str, Any]], *, name: str = "static") -> None:
        self.name = name
        self._signals = signals

    async def collect(self, domain: str) -> dict[str, Any]:
        return dict(self._signals.get(domain, {}))


# --- the scoring model — documented weights, applied deterministically --------
#
# The hard gates: a truthy value (or, for ``aggregator_risk``, a value at/above the
# refuse threshold) VETOES — band="refuse" regardless of score. Order is fixed so
# the recorded ``gate`` reason is deterministic.
_HARD_GATES: tuple[tuple[str, str], ...] = (
    ("on_blocklist", "blocklist"),  # Safe Browsing / Spamhaus / PhishTank hit
    ("https_invalid", "no_valid_https"),  # no valid TLS — table stakes for commerce
    ("parked", "parked"),  # a parked/for-sale page, not a shop
    ("suspended", "suspended"),  # registrar-suspended
    ("sinkholed", "sinkholed"),  # DNS-sinkholed (known-bad)
)

# Score contributions (base 50, clamped to 0..100). Each entry maps a signal to a
# function value -> points; the breakdown records the value AND the points applied,
# so the verdict is fully auditable. Strong (hard-to-fake) signals carry large
# magnitudes; weak (easy-to-fake) ones small ones — forge-resistance weighting.
_BASE_SCORE = 50


def _domain_age_points(days: Any) -> int:
    d = _as_int(days)
    if d is None:
        return 0
    if d >= 730:
        return 20  # 2y+ — strong, expensive to fake
    if d >= 365:
        return 12
    if d >= 180:
        return 5
    if d < 30:
        return -20  # a brand-new domain selling goods is a strong scam negative
    return 0


def _review_points(value: Any) -> int:
    # value is a {"count", "age_days"} dict — depth AND age both matter (a wall of
    # fresh reviews is cheap to fabricate; depth sustained over a year is not).
    if not isinstance(value, dict):
        return 0
    count = _as_int(value.get("count")) or 0
    age = _as_int(value.get("age_days")) or 0
    if count >= 100 and age >= 365:
        return 15
    if count >= 20:
        return 6
    return 0


def _aggregator_points(risk: Any) -> int:
    # The malicious axis below the refuse threshold still drags the score down,
    # proportionally — a 0..100 risk costs up to ~21 points.
    r = _as_int(risk)
    if r is None:
        return 0
    return -int(round(r * 0.21))


# signal -> (points function, "strong"|"weak" tag for the audit breakdown).
_CONTRIBUTORS: dict[str, Any] = {
    "domain_age_days": _domain_age_points,
    "review": _review_points,
    "company_registry_match": lambda v: 15 if v else 0,  # strong: registry-verified
    "wayback_depth": lambda v: 8 if (_as_int(v) or 0) >= 50 else (3 if (_as_int(v) or 0) >= 5 else 0),
    "established_platform": lambda v: 10 if v else 0,  # known commerce platform
    "abused_tld": lambda v: -15 if v else 0,  # strong negative (cheap, abused TLDs)
    "https_valid": lambda v: 2 if v else 0,  # WEAK positive (cheap to obtain)
    "dmarc": lambda v: 2 if v else 0,  # WEAK positive
    "spf": lambda v: 2 if v else 0,  # WEAK positive
    "aggregator_risk": _aggregator_points,
}


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    return None


def score_signals(
    signals: dict[str, Any],
    provenance: dict[str, str],
    *,
    trusted_at: int = 70,
    aggregator_refuse_at: int = 70,
    refuse_below: int | None = None,
) -> ReputationVerdict:
    """The PURE scoring core — signals in, a :class:`ReputationVerdict` out, no I/O.

    Hard gates are checked first (a veto → refuse). Otherwise the score starts at
    ``_BASE_SCORE`` and each contributing signal moves it by its documented weight;
    the result is clamped to 0..100. ``band`` is ``trusted`` at/above ``trusted_at``,
    else ``caution`` — ``refuse`` is reserved for hard gates (and an optional
    ``refuse_below`` score floor, default off, so a deployment can choose to refuse
    very-low-trust domains outright rather than route them to human caution)."""
    breakdown: dict[str, Any] = {}

    # 1) hard gates — veto first, deterministic order.
    for key, reason in _HARD_GATES:
        if signals.get(key):
            breakdown[key] = {"value": signals[key], "gate": reason}
            return ReputationVerdict(
                band="refuse", score=0, gate=reason,
                signals=breakdown, provenance=_prov_subset(provenance, breakdown),
            )
    risk = _as_int(signals.get("aggregator_risk"))
    if risk is not None and risk >= aggregator_refuse_at:
        breakdown["aggregator_risk"] = {"value": risk, "gate": "aggregator_risk"}
        return ReputationVerdict(
            band="refuse", score=0, gate="aggregator_risk",
            signals=breakdown, provenance=_prov_subset(provenance, breakdown),
        )

    # 2) weighted score from the contributing signals.
    score = _BASE_SCORE
    for name, fn in _CONTRIBUTORS.items():
        if name not in signals:
            continue
        pts = int(fn(signals[name]))
        if pts:
            score += pts
            breakdown[name] = {"value": signals[name], "weight": pts}
    score = max(0, min(100, score))

    if refuse_below is not None and score < refuse_below:
        return ReputationVerdict(
            band="refuse", score=score, gate="low_trust",
            signals=breakdown, provenance=_prov_subset(provenance, breakdown),
        )
    band = "trusted" if score >= trusted_at else "caution"
    return ReputationVerdict(
        band=band, score=score, gate=None,
        signals=breakdown, provenance=_prov_subset(provenance, breakdown),
    )


def _prov_subset(provenance: dict[str, str], breakdown: dict[str, Any]) -> dict[str, str]:
    """Record provenance only for the signals that actually moved the verdict, so
    the audit breakdown and its provenance line up exactly."""
    return {k: provenance[k] for k in breakdown if k in provenance}


class DeterministicReputationScorer:
    """The reference :class:`ReputationProvider`: gather signals from its sources,
    then apply :func:`score_signals`. Pure scoring over pluggable fetchers — so the
    weights are testable with :class:`StaticSignalSource` at ``$0`` and a real
    Safe-Browsing/RDAP/Companies-House fetcher drops in behind ``SignalSource``
    without touching the scoring model. Later sources WIN on a signal-name clash
    (so a more authoritative source can override a cheaper one), and each signal's
    provenance records which source produced it."""

    name = "deterministic"

    def __init__(
        self,
        sources: list[SignalSource],
        *,
        trusted_at: int = 70,
        aggregator_refuse_at: int = 70,
        refuse_below: int | None = None,
    ) -> None:
        self._sources = list(sources)
        self._trusted_at = trusted_at
        self._aggregator_refuse_at = aggregator_refuse_at
        self._refuse_below = refuse_below

    async def assess(self, domain: str) -> ReputationVerdict:
        signals: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        for src in self._sources:
            for name, value in (await src.collect(domain)).items():
                signals[name] = value  # later source wins
                provenance[name] = src.name
        return score_signals(
            signals, provenance,
            trusted_at=self._trusted_at,
            aggregator_refuse_at=self._aggregator_refuse_at,
            refuse_below=self._refuse_below,
        )


# Structural conformance check (no runtime cost; documents intent).
_: type[ReputationProvider] = DeterministicReputationScorer
