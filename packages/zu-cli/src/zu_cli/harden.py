"""Stage 5 — chaos hardening. Score how brittle a captured path is, offline and free.

Two honest, $0 signals over a ``fixtures/capture.json`` bundle (the keystone's output):

* **Static brittleness audit** — read the captured moves and flag single points of
  failure WITHOUT a run: a click/fill/select that targets one selector with no ``near``
  fallback, and a grounded value that appears exactly once across the fixtures (no
  redundancy, so one wording change loses it). This is the doc's "surface brittle
  single-selector steps", computed structurally.

* **Perturbation replay** — generate variant bundles and re-run them through the offline
  keystone (``offline.replay_offline``), modelled on ``zu-redteam``'s out-of-band
  verdict pattern (inspect a finished run from outside its trust boundary). The
  **resilience score** is the fraction of *value-preserving* perturbations the path
  still succeeds on (cosmetic page noise it should absorb). *Value-corrupting*
  perturbations are run too, as a control: they MUST fail, proving grounding is actually
  load-bearing and the score is not a rubber stamp.

What this does NOT measure: adaptive recovery (re-pathfinding around a renamed selector
or an injected interstitial). The replay drives the *captured* moves — a frozen model —
so a perturbation that needs a NEW decision can't be absorbed offline. Measuring that
needs a live model and is the next increment (a live hardening lane); it is deliberately
out of this $0 scope, not silently conflated with what is measured here.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .offline import Bundle, replay_offline

# The fixture observation fields that carry page content (what grounding reads).
_TEXT_FIELDS = ("text", "html", "content")
# The action verbs that target a single element — brittle without a `near` fallback.
_TARGETING = ("click", "fill", "select")


# --- findings + report -------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One static brittleness finding — a single point of failure in the captured path."""

    kind: str   # "single-selector" | "single-occurrence"
    where: str
    detail: str


@dataclass
class VariantResult:
    """One perturbation replay: did the path still succeed, and was that the expectation?"""

    name: str
    expect_pass: bool
    passed: bool

    @property
    def ok(self) -> bool:
        # A value-preserving variant should pass; a value-corrupting one should fail.
        return self.passed == self.expect_pass


@dataclass
class HardenReport:
    findings: list[Finding] = field(default_factory=list)
    variants: list[VariantResult] = field(default_factory=list)

    @property
    def resilience(self) -> float:
        """Fraction of value-preserving perturbations the path still succeeds on
        (1.0 = absorbed every cosmetic change; <1.0 = brittle to page noise)."""
        benign = [v for v in self.variants if v.expect_pass]
        return sum(v.passed for v in benign) / len(benign) if benign else 1.0

    @property
    def grounding_load_bearing(self) -> bool:
        """Every value-corrupting variant failed — so the score reflects real grounding,
        not a path that succeeds regardless of what the page says."""
        return all(not v.passed for v in self.variants if not v.expect_pass)


# --- static audit ------------------------------------------------------------


def _value_strings(value: Any) -> list[str]:
    """Flatten a result value into the scalar strings grounding must find on the page."""
    out: list[str] = []
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_value_strings(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_value_strings(v))
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        s = str(value).strip()
        if s:
            out.append(s)
    return out


def _final_value(bundle: Bundle) -> Any:
    """The captured answer — the last text move, parsed as JSON when it is JSON."""
    import json

    for move in reversed(bundle.moves):
        if "text" in move and move.get("text"):
            try:
                return json.loads(move["text"])
            except (ValueError, TypeError):
                return move["text"]
    return None


def grounded_values(bundle: Bundle) -> list[str]:
    """The distinct scalar strings the captured answer commits to — the values a generic
    agent must DERIVE from the page (and so must never hardcode). Order-preserving and
    de-duplicated. Reused by the brittleness audit and the anti-hardcode guardrail."""
    return list(dict.fromkeys(_value_strings(_final_value(bundle))))


def audit_brittleness(bundle: Bundle) -> list[Finding]:
    """Flag single points of failure in the captured path, no run required."""
    findings: list[Finding] = []

    # 1. Single-selector steps: a targeting action with no `near` fallback.
    step = 0
    for move in bundle.moves:
        if move.get("tool") not in ("browser", "render_dom"):
            continue
        for action in move.get("args", {}).get("actions", []) or []:
            if not isinstance(action, dict):
                continue
            verb = next((v for v in _TARGETING if v in action), None)
            if verb and "near" not in action:
                findings.append(Finding(
                    kind="single-selector",
                    where=f"{move['tool']} move #{step + 1}",
                    detail=f"{verb} {action[verb]!r} has no `near` fallback — one renamed "
                           "selector breaks this step; add an alternate locator.",
                ))
        step += 1

    # 2. Single-occurrence grounded values: present in exactly one fixture observation.
    haystacks = [str(obs.get(f, "")) for obss in bundle.observations.values()
                 for obs in obss for f in _TEXT_FIELDS]
    for s in grounded_values(bundle):
        hits = sum(s in h for h in haystacks)
        if hits == 1:
            findings.append(Finding(
                kind="single-occurrence",
                where=f"value {s!r}",
                detail="appears in only one fixture observation — a single wording change "
                       "there loses the grounding for this value.",
            ))
    return findings


# --- perturbation variants ---------------------------------------------------


def _map_observations(bundle: Bundle, fn: Any) -> Bundle:
    """A deep copy of ``bundle`` with ``fn`` applied to every content string field of
    every observation. Moves are untouched — only the 'page' changes."""
    variant = copy.deepcopy(bundle)
    for obss in variant.observations.values():
        for obs in obss:
            for f in _TEXT_FIELDS:
                if isinstance(obs.get(f), str):
                    obs[f] = fn(obs[f])
    return variant


def perturb_variants(bundle: Bundle) -> list[tuple[str, Bundle, bool]]:
    """Generate ``(name, variant, expect_pass)`` perturbations that keep the observation
    SEQUENCE aligned (so the frozen captured moves still apply) and vary only the page
    text — the honest offline subset. Value-preserving variants wrap the page in benign
    noise (a resilient path absorbs them); value-corrupting variants delete a grounded
    value (the path MUST then fail)."""
    variants: list[tuple[str, Bundle, bool]] = []

    # Value-preserving: cosmetic noise around the existing content (values intact).
    variants.append((
        "banner-prefix",
        _map_observations(bundle, lambda t: "[Cookie notice — we value your privacy] " + t),
        True,
    ))
    variants.append((
        "promo-suffix",
        _map_observations(bundle, lambda t: t + " — Limited-time offer, shop now!"),
        True,
    ))

    # Value-corrupting: remove each grounded value from every observation (a control —
    # these must fail, or grounding is not gating).
    for s in dict.fromkeys(_value_strings(_final_value(bundle))):
        variants.append((
            f"drop-value:{s}",
            _map_observations(bundle, lambda t, s=s: t.replace(s, "[removed]")),
            False,
        ))
    return variants


# --- the harden run ----------------------------------------------------------


async def harden(spec: Any, cfg: Any, bundle: Bundle) -> HardenReport:
    """Audit the captured path statically, then replay every perturbation offline and
    score resilience. Pure $0: no model, no network."""
    from zu_core.contracts import Status

    findings = audit_brittleness(bundle)
    results: list[VariantResult] = []
    for name, variant, expect_pass in perturb_variants(bundle):
        result, _events = await replay_offline(spec, cfg, variant)
        results.append(VariantResult(
            name=name, expect_pass=expect_pass, passed=result.status is Status.SUCCESS))
    return HardenReport(findings=findings, variants=results)
