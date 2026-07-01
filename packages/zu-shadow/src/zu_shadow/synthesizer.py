"""The synthesizer — itself a Zu agent — turns a recording into an agent + rail.

Input: the REDACTED recorded log (``data.shadow.*`` events) + the captured "why"
intents + one sentence of instruction. Output: a :class:`SynthesisResult` carrying

  * a Zu **agent spec** (the ``agent.yaml`` shape: policy prompt, tools, detectors,
    validators, tier ladder, and a capability envelope whose EGRESS ALLOWLIST WRITES
    ITSELF from the recorded ``network.response`` hosts);
  * an induced **FSM** as a ``zu_core.reachability.Fsm`` (NO new type) — the same
    shape Phase-1's rail check and the Phase-4 event-log→Fsm builder consume; and
  * **invariants** as ``zu_core.invariants.Invariant`` (NO new type) — the egress
    allowlist as a ``DOMAIN_ALLOWLIST`` and the recorded outcome as an ``EVENTUALLY``
    success criterion.

The synthesizer is a Zu agent: it is *driven by a* ``ModelProvider``. The model's
job is the one genuinely model-shaped decision — writing the policy prompt and
naming the goal from the human's intent narration. Everything verifiable (the
egress set, the FSM topology, the step sequence) is DERIVED deterministically from
the log, never invented by the model. Offline it is driven by ``ScriptedProvider``
(so the whole thing is $0 and deterministic); the egress/FSM/invariants come out
identical regardless of the model, because they are induced, not generated.

The synthesizer PROPOSES. Promotion is GATED downstream by reproduced outcome
(``replay_gate``); the "why" resolutions are surfaced for REVIEW, never auto-applied.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from zu_core import events as ev
from zu_core.invariants import Invariant, InvariantKind, Predicate, PredicateKind
from zu_core.ports import ModelProvider, ModelRequest
from zu_core.reachability import Fsm, FsmEdge

# The state label for the recording's start, and the accepting (goal) state.
_INITIAL_STATE = "start"
_GOAL_STATE = "goal"


@dataclass
class SynthesisResult:
    """What the synthesizer PROPOSES — reviewed and replay-gated before promotion."""

    # The agent.yaml-shaped spec (policy prompt, tools, detectors, validators, tier,
    # capability envelope incl. the self-writing egress allowlist).
    spec: dict
    # The induced plan as a core Fsm (shared with §1 rail / §4 fsm-from-log).
    fsm: Fsm
    # The induced invariants (egress allowlist + success criterion), as core types.
    invariants: list[Invariant] = field(default_factory=list)
    # The "why" intents surfaced for REVIEW — (step_index, intent_text). Never
    # auto-promoted; a reviewer decides whether each becomes a prompt directive.
    intents_for_review: list[tuple[int, str]] = field(default_factory=list)

    @property
    def egress(self) -> list[str]:
        """The induced egress allowlist (the hosts), for convenience/printing."""
        env = self.spec.get("capability_envelope", {})
        return list(env.get("egress", []))

    def to_yaml_dict(self) -> dict:
        """The full proposal as a JSON/YAML-able dict (spec + serialized FSM +
        invariants), so a reviewer can read or persist the whole proposal."""
        return {
            **self.spec,
            "induced_fsm": _fsm_to_dict(self.fsm),
            "induced_invariants": [_invariant_to_dict(i) for i in self.invariants],
        }


def _payload(e: object) -> dict:
    p = getattr(e, "payload", None)
    return p if isinstance(p, dict) else {}


def _host_of(url: str) -> str:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return ""
    return host or ""


def _registrable(host: str) -> str:
    """A generic, PSL-free approximation of a host's registrable domain (eTLD+1): its
    last two dot-separated labels (``api.vets.example.com`` → ``example.com``). This is
    the FIRST-PARTY key — two hosts sharing it are the same site. It intentionally does
    NOT special-case multi-label public suffixes (``co.uk``): erring toward a BROADER
    first-party grouping only ever KEEPS a same-org host, never widens the allowlist to a
    third-party tracker on a different registrable domain (the failure mode F8 fixes)."""
    labels = [x for x in host.split(".") if x]
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def induce_egress(events: list[object]) -> list[str]:
    """THE EGRESS ALLOWLIST WRITES ITSELF — but SCOPED to the hosts the recorded ACTIONS
    actually needed, not every incidental subresource the page fetched (F8).

    The scoping signal is FIRST-PARTY-ness, derived structurally from the log: the hosts
    the human NAVIGATED to define the first-party registrable domain(s) of the task; a
    recorded ``network.response`` host is admitted only when it shares one of those
    registrable domains. So a page's own API/CDN subdomains (``api.``/``cdn.`` of the
    navigated site) are KEPT — the actions genuinely needed them — while a third-party
    tracker/analytics/ad host on a DIFFERENT registrable domain
    (``google-analytics.com``, a CDN on another org's domain) is DROPPED, instead of the
    allowlist absorbing every beacon and defeating its own purpose. Deterministic; derived
    from the log, never from the model. A recording with no navigation host (nothing to
    anchor first-partyness to) falls back to admitting the observed hosts, so an
    action-only recording is never left with an empty allowlist."""
    nav_hosts: set[str] = set()
    resp_hosts: set[str] = set()
    for e in events:
        t = getattr(e, "type", "")
        p = _payload(e)
        if t == ev.SHADOW_USER_NAVIGATE:
            host = _host_of(p.get("url", ""))
            if host:
                nav_hosts.add(host)
        elif t == ev.SHADOW_NETWORK_RESPONSE:
            host = p.get("host") or _host_of(p.get("url", ""))
            if host:
                resp_hosts.add(host)
    # The navigation hosts are always in-scope (the human went there deliberately).
    hosts: set[str] = set(nav_hosts)
    first_party = {_registrable(h) for h in nav_hosts}
    if first_party:
        # Admit only subresource hosts on a navigated site's registrable domain.
        hosts.update(h for h in resp_hosts if _registrable(h) in first_party)
    else:
        # No navigation to anchor first-partyness — fall back to the observed hosts so an
        # action-only recording still writes a (conservative) allowlist rather than none.
        hosts.update(resp_hosts)
    return sorted(hosts)


def induce_fsm(events: list[object]) -> Fsm:
    """Induce a linear ``Fsm`` from the recorded action sequence — one state per
    captured user action, an edge labelled by the action between consecutive states,
    ``start`` as initial and ``goal`` as the single accepting state. Aligned with the
    Phase-4 event-log→Fsm builder so the §1 reachability check consumes it unchanged.
    """
    actions = [
        e for e in events
        if getattr(e, "type", "") in (ev.SHADOW_USER_CLICK, ev.SHADOW_USER_TYPE,
                                      ev.SHADOW_USER_NAVIGATE)
    ]
    labels = _clean_step_labels([_action_label(e) for e in actions])
    states = [_INITIAL_STATE]
    edges: list[FsmEdge] = []
    prev = _INITIAL_STATE
    for i, label in enumerate(labels):
        s = f"s{i + 1}"
        states.append(s)
        edges.append(FsmEdge(src=prev, dst=s, label=label))
        prev = s
    states.append(_GOAL_STATE)
    edges.append(FsmEdge(src=prev, dst=_GOAL_STATE, label="done"))
    return Fsm(
        states=frozenset(states),
        initial=_INITIAL_STATE,
        accepting=frozenset({_GOAL_STATE}),
        edges=tuple(edges),
    )


# Baked-in prices make a target name instance-brittle ("Add to cart £46.00"); strip them so
# the step generalises ("Add to cart"). Generic shape, never a site constant.
_PRICE = re.compile(r"[£$€]\s*\d+(?:[.,]\d{2})?(?:\s*(?:GBP|USD|EUR))?", re.I)


def _clean_name(name: str) -> str:
    """Normalise a captured accessible name for the induced step: drop baked-in prices,
    collapse a multi-option dump to its first option, squeeze whitespace, cap length —
    so the FSM reads as clean, generalised steps rather than instance soup."""
    s = _PRICE.sub("", name or "")
    s = s.replace("\n", " ").split("  ")[0]  # a select's option-dump → its first option
    s = re.sub(r"\s+", " ", s).strip(" -–·|\t")
    return s[:50]


def _action_label(e: object) -> str:
    t = getattr(e, "type", "")
    p = _payload(e)
    if t == ev.SHADOW_USER_NAVIGATE:
        return "navigate"
    verb = "click" if t == ev.SHADOW_USER_CLICK else "type"
    target = p.get("target", {})
    name = _clean_name(target.get("name") or target.get("label") or target.get("role") or "")
    return f"{verb}:{name}" if name else verb


def _clean_step_labels(labels: list[str]) -> list[str]:
    """Clean a raw step sequence into a generalised path: (R1) collapse a run of identical
    consecutive steps (a widget that fires the same action twice), and (R2) drop a
    focus-click immediately followed by a type on the SAME target (focus-then-type → just
    the type). The "why" annotations live on the events and are surfaced separately, so
    pruning a redundant FSM state never loses a captured rationale."""
    out: list[str] = []
    for i, lab in enumerate(labels):
        if lab.startswith("click:") and i + 1 < len(labels):
            nxt = labels[i + 1]
            if nxt.startswith("type:") and nxt[len("type:"):] == lab[len("click:"):]:
                continue  # R2: this click just focuses the field the next step types into
        if out and out[-1] == lab:
            continue  # R1: a consecutive duplicate of the step we already kept
        out.append(lab)
    return out


def _derive_success_token(events: list[object]) -> str | None:
    """DERIVE the success signal from OBSERVED STRUCTURE, not a model string (F12).

    The recorded surface state that means "the task reached its end" is the control the
    human's LAST action operated — the terminal click/type target's cleaned accessible
    name (a real string the recording contains, e.g. "Confirm booking"). The success rail
    then asserts that same label re-appears on the live SurfaceView, so the criterion is
    grounded in the recording's structure rather than a free-text goal the model invented.
    Returns ``None`` when the recording has no named action target to key on (the caller
    then omits the outcome rail rather than inventing one)."""
    last_name = ""
    for e in events:
        t = getattr(e, "type", "")
        if t in (ev.SHADOW_USER_CLICK, ev.SHADOW_USER_TYPE):
            tgt = _payload(e).get("target", {})
            name = _clean_name(tgt.get("name") or tgt.get("label") or "")
            if name:
                last_name = name
    return last_name or None


def induce_invariants(events: list[object], egress: list[str],
                      success_token: str | None = None) -> list[Invariant]:
    """Induce rail invariants as CORE ``Invariant`` objects (no new type):

      * a ``DOMAIN_ALLOWLIST`` over the induced egress — the agent must not reach a
        host the human's session never touched (defense in depth on top of the
        capability envelope); and
      * an ``EVENTUALLY`` success criterion whose expected token is DERIVED from the
        recorded structure — the label of the human's terminal action's target
        (``_derive_success_token``), NOT a free-text goal the model invented (F12). The
        rail asserts that observed label re-appears on the live SurfaceView by the
        deadline (``require_present`` so a run that never reaches it VIOLATES rather than
        passing vacuously). When the recording carries no named terminal target, the
        outcome rail is OMITTED rather than keyed on an invented string.
    """
    invs: list[Invariant] = []
    if egress:
        invs.append(Invariant(
            name="recorded-egress-allowlist",
            kind=InvariantKind.THROUGHOUT,
            predicate=Predicate(
                kind=PredicateKind.DOMAIN_ALLOWLIST,
                params={"event_type": ev.SHADOW_NETWORK_RESPONSE, "field": "host",
                        "allow": list(egress)},
            ),
        ))
    token = success_token if success_token is not None else _derive_success_token(events)
    if token:
        invs.append(Invariant(
            name="reproduce-recorded-outcome",
            kind=InvariantKind.EVENTUALLY,
            predicate=Predicate(
                kind=PredicateKind.SURFACE_CONTAINS,
                params={"event_type": ev.SURFACE_CAPTURED, "label": token,
                        "require_present": True},
            ),
        ))
    return invs


# --- the model-shaped seam: the policy prompt + goal from the human's intent ----

_SYNTH_TOOLS = ["web_search", "http_fetch", "html_parse", "recall"]
_SYNTH_BROWSER_TOOLS = ["render_dom", "browser"]


def _build_request(instruction: str, events: list[object],
                   intents: list[str], egress: list[str]) -> ModelRequest:
    """The prompt handed to the synthesizing model: the instruction, the redacted
    action trace, the reviewed intents, and the induced egress — asking ONLY for the
    policy prompt + a one-line goal (the verifiable parts are derived, not asked)."""
    trace = [_action_label(e) for e in events
             if getattr(e, "type", "").startswith("data.shadow.user.")]
    sys = (
        "You are Zu's Shadow synthesizer. From a REDACTED recording of a human doing "
        "a task, write a production agent's policy prompt and name its goal. Do not "
        "invent any host, secret, or step the recording does not show. Respond with a "
        "JSON object: {\"policy_prompt\": str, \"goal\": str}."
    )
    user = json.dumps({
        "instruction": instruction,
        "recorded_actions": trace,
        "why_intents": intents,
        "observed_hosts": egress,
    })
    return ModelRequest(messages=[{"role": "system", "content": sys},
                                  {"role": "user", "content": user}])


def _parse_model(text: str | None, instruction: str) -> tuple[str, str]:
    """Parse the model's {policy_prompt, goal}. A non-JSON or empty reply falls back
    to the instruction itself as the prompt and 'goal' as the goal — the synthesizer
    degrades to a usable proposal rather than failing (it is reviewed anyway)."""
    if text:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return (str(obj.get("policy_prompt") or instruction),
                        str(obj.get("goal") or "goal"))
        except (ValueError, TypeError):
            pass
    return instruction, "goal"


class Synthesizer:
    """The synthesizer agent. Driven by a ``ModelProvider`` (``ScriptedProvider``
    offline). ``synthesize`` runs the one model call, then DERIVES the egress, FSM,
    and invariants from the log deterministically."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    async def synthesize(self, session, instruction: str) -> SynthesisResult:
        """Produce the agent + rail PROPOSAL from a :class:`RecordedSession`."""
        events = list(session.events)
        intents = _collect_intents(events)
        egress = induce_egress(events)

        req = _build_request(instruction, events, [t for _, t in intents], egress)
        resp = await self._provider.complete(req)
        policy_prompt, goal = _parse_model(resp.text, instruction)

        fsm = induce_fsm(events)
        # The success rail's expected token is DERIVED from the recorded structure (the
        # terminal action's target label), NOT the model-produced ``goal`` — the goal
        # remains the human-readable label in the spec/task, but the VERIFIABLE success
        # criterion must be derived-not-invented (F12).
        invariants = induce_invariants(events, egress)
        needs_browser = any(
            getattr(e, "type", "") in (ev.SHADOW_USER_CLICK, ev.SHADOW_USER_TYPE)
            for e in events
        )
        tiers: dict[int, list[str]] = {1: list(_SYNTH_TOOLS)}
        max_tier = 1
        if needs_browser:
            tiers[2] = list(_SYNTH_BROWSER_TOOLS)
            max_tier = 2

        spec = {
            "provider": {"name": "scripted"},  # a reviewer wires a real provider
            "tiers": tiers,
            "plugins": {
                "detectors": ["empty", "error", "js-shell", "embedded-widget", "bot-wall"],
                "validators": ["schema", "grounding"],
            },
            "capability_envelope": {
                # The self-writing egress allowlist — the hosts the human's session
                # actually touched, nothing more (a reviewer can narrow further).
                "egress": list(egress),
                "tier": max_tier,
            },
            "task": {
                "query": policy_prompt,
                "goal": goal,
                "max_tier": max_tier,
            },
        }
        return SynthesisResult(
            spec=spec, fsm=fsm, invariants=invariants, intents_for_review=intents
        )


def _collect_intents(events: list[object]) -> list[tuple[int, str]]:
    """The (step_index, "why" text) pairs from the recorded action events — surfaced
    for REVIEW, never auto-promoted. The text is already redacted (it was redacted at
    capture, before it reached the log)."""
    out: list[tuple[int, str]] = []
    step = 0
    for e in events:
        t = getattr(e, "type", "")
        if t in (ev.SHADOW_USER_CLICK, ev.SHADOW_USER_TYPE, ev.SHADOW_USER_NAVIGATE):
            step += 1
            intent = _payload(e).get("intent")
            if intent:
                out.append((step, str(intent)))
    return out


# --- serialization helpers (a proposal is reviewable / persistable) -------------


def _fsm_to_dict(fsm: Fsm) -> dict:
    return {
        "states": sorted(fsm.states),
        "initial": fsm.initial,
        "accepting": sorted(fsm.accepting),
        "edges": [{"src": e.src, "dst": e.dst, "label": e.label} for e in fsm.edges],
    }


def _invariant_to_dict(inv: Invariant) -> dict:
    return {
        "name": inv.name,
        "kind": inv.kind.value,
        "predicate": {"kind": inv.predicate.kind.value, "params": inv.predicate.params},
        "applies_to": inv.applies_to,
    }
