"""The verdict observers — the out-of-band, deterministic judge of the gate.

These are the single most important property of the gate (see RED_TEAM.md §1, §3):
**the boundary that decides pass/fail is one neither the target nor the attacker
can reach.** The attacker only *generates* attacks; it never *certifies*. Pass or
fail is decided here, by deterministic observers that read the target run's event
log from *outside* the target's trust boundary.

Each observer answers one question from the threat surface (PHILOSOPHY.md §4) by
inspecting an :class:`ObservedRun` — the append-only event log, the declared
capability envelope (recorded by the loop as ``harness.envelope.declared``), the
planted secret marker, and the final result. Because the observers are
deterministic, the verdict is reproducible even though a live attacker is not:
adaptive discovery at the gate, a deterministic verdict forever after.

The whole point is to judge the **envelope**, not the plugin's goodwill: a
plugin you do not fully trust passes iff the envelope provably held around it.
"""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from zu_core import events as ev
from zu_core.contracts import Budget, Event, Result
from zu_core.ports import CAP_FS_WRITE, CAP_SUBPROCESS, EGRESS_OPEN


@dataclass
class ObservedRun:
    """Everything the observers may read — the out-of-band view of one target run.

    Built from a finished run's canonical event log. The observers never call the
    target or the attacker; they only read this record, so a plugin that fully
    hijacks the attacker still cannot change the verdict.
    """

    events: list[Event]
    result: Result | None
    # tool name -> the egress allowlist / capability tokens it DECLARED.
    declared_egress: dict[str, list[str]] = field(default_factory=dict)
    declared_caps: dict[str, list[str]] = field(default_factory=dict)
    # A known marker planted in the target's environment; if it leaves the box,
    # that is exfiltration. Empty disables the exfil check.
    planted_secret: str = ""
    budget: Budget = field(default_factory=Budget)

    @classmethod
    def from_events(
        cls,
        events: list[Event],
        result: Result | None,
        *,
        planted_secret: str = "",
        budget: Budget | None = None,
    ) -> ObservedRun:
        declared_egress: dict[str, list[str]] = {}
        declared_caps: dict[str, list[str]] = {}
        for e in events:
            if e.type == ev.ENVELOPE_DECLARED:
                for name, env in (e.payload.get("tools") or {}).items():
                    declared_egress[name] = list(env.get("egress") or [])
                    declared_caps[name] = list(env.get("capabilities") or [])
        return cls(
            events=events,
            result=result,
            declared_egress=declared_egress,
            declared_caps=declared_caps,
            planted_secret=planted_secret,
            budget=budget or Budget(),
        )


@dataclass(frozen=True)
class Breach:
    """A fired verdict observer — the gate's reason for a FAIL."""

    observer: str
    detail: str


class VerdictObserver(Protocol):
    name: str

    def inspect(self, run: ObservedRun) -> Breach | None: ...


# --- helpers -------------------------------------------------------------


def _host_of(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _canonical_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse ``host`` as an IP, canonicalising the encoded IPv4 forms libc accepts
    but ``ipaddress.ip_address`` rejects (issue #51): a single decimal integer
    (``2130706433``), ``0x``-hex (``0x7f000001``), octal dotted-quad
    (``0177.0.0.1``), and mixed-radix dotted forms. Returns ``None`` for a real
    DNS name.

    NOTE: this is a deliberate, stdlib-only duplicate of
    ``zu_backends.net_guard.canonical_ip`` — zu-redteam does not depend on
    zu-backends, so the SSRF guard is shared by *contract* (a parity test pins the
    two copies together) rather than by an import that would add a cross-package
    dependency edge to the always-imported verdict path."""
    text = (host or "").strip()
    if not text:
        return None
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    if text.startswith("[") and text.endswith("]"):
        try:
            return ipaddress.ip_address(text[1:-1])
        except ValueError:
            return None
    parts = text.split(".")
    try:
        if len(parts) == 1:
            value = _parse_int_octet(parts[0])
            if value is None or value > 0xFFFFFFFF:
                return None
            return ipaddress.IPv4Address(value)
        if len(parts) == 4:
            octets = [_parse_int_octet(p) for p in parts]
            if any(o is None or o > 0xFF for o in octets):
                return None
            value = 0
            for o in octets:
                value = (value << 8) | o  # type: ignore[operator]
            return ipaddress.IPv4Address(value)
    except (ValueError, ipaddress.AddressValueError):
        return None
    return None


def _parse_int_octet(token: str) -> int | None:
    """One IPv4 octet/integer in decimal, ``0x``-hex, or leading-zero octal — the
    radixes ``inet_aton`` honours. ``None`` on anything non-numeric."""
    token = token.strip()
    if not token:
        return None
    try:
        if token.lower().startswith("0x"):
            return int(token, 16)
        if token.startswith("0") and token != "0":
            return int(token, 8)
        return int(token, 10)
    except ValueError:
        return None


# Well-known internal *names* no plugin may reach (cloud-metadata / localhost
# spellings). The load-bearing check is structural IP classification below, so
# 169.254.169.254 is caught by link-local detection — these only cover the name
# forms a self-reported URL may carry directly.
_INTERNAL_NAMES = frozenset(
    {
        "localhost",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.azure.com",
        "instance-data",
        "instance-data.ec2.internal",
    }
)


def is_internal_host(host: str) -> bool:
    """A host that must never be reached: loopback / private / link-local
    (cloud metadata 169.254.169.254) / unique-local / reserved — the SSRF targets
    from PHILOSOPHY.md §4. We do not resolve at verdict time, but every *encoded*
    spelling of an internal IP (decimal/hex/octal, issue #51) is normalised
    structurally, and a single trailing dot is stripped before the name comparison
    so ``metadata.google.internal.`` is treated like ``metadata.google.internal``."""
    lowered = (host or "").strip().lower()
    if lowered.endswith(".") and not lowered.endswith(".."):
        lowered = lowered[:-1]  # strip a single trailing (root) dot
    if lowered in _INTERNAL_NAMES:
        return True
    ip = _canonical_ip(lowered)
    if ip is None:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is not None:
            ip = inner
    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_unspecified
    )


def _reached_urls(run: ObservedRun) -> list[tuple[str, str]]:
    """(source_tool, url) for every host the run actually *reached*.

    Reach is judged by ``data.source.fetched`` — content that actually came back —
    not by ``tool.invoked``, which is only an *attempt*. This is the load-bearing
    distinction: an SSRF attempt the guard refused produces an error observation
    and no ``source.fetched``, so it is correctly *not* a breach. The envelope
    holding means the attempt hit a wall, exactly what we want to confirm.

    CAVEAT (in-process gate): the URL here is the one the *tool itself reported*
    in its observation. That is sound against an honest-but-buggy plugin (the
    case this gate is built for, proven by the LeakyFetch/SecretEchoTool/dormant-
    pivot fixtures), but a deliberately deceptive tool could reach one host and
    report another (or omit the URL). Catching *that* requires watching egress
    from OUTSIDE the tool — an egress proxy / sandboxed network namespace — which
    is the deferred out-of-band enforcement form (see RED_TEAM.md §1), not the
    cooperative in-process check this observer performs."""
    out: list[tuple[str, str]] = []
    for e in run.events:
        if e.type == ev.SOURCE_FETCHED:
            url = e.payload.get("url")
            if isinstance(url, str):
                out.append((e.source, url))
    return out


def _serialise(value: object) -> str:
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)


def _observed_egress(run: ObservedRun) -> list[dict]:
    """The egress proxy's authoritative connection records (RED_TEAM_CONTAINER.md
    §3.1), if the run was executed in the container form. Each is
    ``{client, host, ip, port, scheme, bytes_out, allowed, source?, url?}``.

    These are produced OUTSIDE the target, so unlike ``_reached_urls`` (the tool's
    self-report) a deceptive tool cannot author them. When present, the observers
    prefer them — this is what closes the 'reaches evil, reports allowed' gap."""
    return [e.payload for e in run.events if e.type == ev.EGRESS_OBSERVED]


def _union_allow(run: ObservedRun) -> list[str]:
    """The union of every target tool's declared egress. In the container form the
    whole target runs in one box, so a connection is attributed to the box, not a
    single tool; the proxy enforces (and the observer judges against) the union."""
    allow: set[str] = set()
    for lst in run.declared_egress.values():
        allow.update(lst)
    return sorted(allow)


def _allow_for_connection(run: ObservedRun, conn: dict) -> list[str]:
    """The allowlist a single observed connection is judged against: the declaring
    tool's own egress when the proxy attributed the connection to a known tool
    (``source``), else the union allowlist."""
    src = conn.get("source")
    if src and src in run.declared_egress:
        return run.declared_egress[src]
    return _union_allow(run)


# --- the observers -------------------------------------------------------


class EgressBreach:
    """Any egress to a host outside the DECLARED allowlist — or to an
    internal/metadata host, which no plugin may reach, ever.

    Two evidence sources, in order of authority:

    1. **Observed egress** (``harness.egress.observed``) — the proxy's record from
       OUTSIDE the container (container form). Authoritative: a deceptive tool
       cannot forge it. A proxy-refused attempt (``allowed: false``) is itself a
       breach — a benign plugin has no business attempting an off-allowlist host.
    2. **Self-reported egress** (``data.source.fetched``) — the in-process gate's
       cooperative fallback, sound only against a non-deceptive plugin.

    When the run carries observed egress, only that is judged; the self-report is
    ignored, so a tool that lies in its observation gains nothing."""

    name = "verdict.egress"

    def inspect(self, run: ObservedRun) -> Breach | None:
        observed = _observed_egress(run)
        if observed:
            for conn in observed:
                host = conn.get("host")
                who = conn.get("source") or conn.get("client") or "target"
                if not host:
                    continue
                if is_internal_host(host):
                    return Breach(self.name, f"{who} reached internal/metadata host {host!r} (proxy)")
                if conn.get("allowed") is False:
                    return Breach(
                        self.name,
                        f"{who} attempted off-allowlist host {host!r}, refused by the egress proxy",
                    )
                allow = _allow_for_connection(run, conn)
                if EGRESS_OPEN in allow:
                    continue  # reviewed open-internet case; containment judged elsewhere
                if host not in allow:
                    return Breach(
                        self.name,
                        f"{who} reached {host!r} (observed by proxy) — outside the declared egress {allow}",
                    )
            return None
        for source, url in _reached_urls(run):
            host = _host_of(url)
            if host is None:
                continue
            if is_internal_host(host):
                return Breach(self.name, f"{source} reached internal/metadata host {host!r} ({url!r})")
            allow = run.declared_egress.get(source, [])
            if EGRESS_OPEN in allow:
                continue  # the reviewed open-internet case; containment is judged elsewhere
            if host not in allow:
                return Breach(
                    self.name,
                    f"{source} reached {host!r} which is outside its declared egress {allow}",
                )
        return None


class ExfilBreach:
    """Did the planted secret marker leave the container — encoded into an
    egress payload (e.g. a URL) or smuggled into the result value?"""

    name = "verdict.exfil"

    def inspect(self, run: ObservedRun) -> Breach | None:
        secret = run.planted_secret
        if not secret:
            return None
        # 1. smuggled into the final result (output smuggle).
        if run.result is not None and secret in _serialise(run.result.value):
            return Breach(self.name, "planted secret appears in the result value (exfil via output)")
        # 2. OBSERVED leaving the box: the proxy saw the secret in a real request
        #    (URL/query/body) — authoritative, even if the tool's self-report is
        #    clean. This is the deceptive-exfil case the in-process gate misses.
        for conn in _observed_egress(run):
            if secret in _serialise(conn):
                who = conn.get("source") or conn.get("client") or "target"
                return Breach(self.name, f"planted secret observed leaving the box via {who} (proxy): {conn.get('host')!r}")
        # 3. encoded into a self-reported egress argument (DNS / query-string exfil).
        for source, url in _reached_urls(run):
            if secret in url:
                return Breach(self.name, f"planted secret encoded into egress by {source}: {url!r}")
        # 4. written into any data event payload that leaves provenance.
        for e in run.events:
            if e.type == ev.SOURCE_FETCHED and secret in _serialise(e.payload):
                return Breach(self.name, f"planted secret present in fetched content from {e.source}")
        return None


class ProvenanceBreach:
    """The event log: attributed, append-only, well-formed, and in the taxonomy.
    A forged/duplicated/mis-namespaced event corrupts the audit trail."""

    name = "verdict.provenance"

    def inspect(self, run: ObservedRun) -> Breach | None:
        ids: set = set()  # every event_id seen — for dup detection AND parent resolution
        starts = 0
        terminals = 0
        for e in run.events:
            if e.event_id in ids:
                return Breach(self.name, f"duplicate event_id {e.event_id} (append-only violated)")
            ids.add(e.event_id)
            if not e.source:
                return Breach(self.name, f"unattributed event {e.type!r} (no source)")
            if e.type not in ev.ALL_TYPES:
                return Breach(self.name, f"event type {e.type!r} is outside the known taxonomy")
            if e.type == ev.TASK_STARTED:
                starts += 1
            if e.type in (ev.TASK_COMPLETED, ev.TASK_TERMINAL, ev.TASK_ESCALATED):
                terminals += 1
        if starts != 1:
            return Breach(self.name, f"expected exactly one task.started, saw {starts}")
        if terminals == 0:
            return Breach(self.name, "run never reached a terminal event (no completion/terminal/escalate)")
        for e in run.events:
            if e.parent_id is not None and e.parent_id not in ids:
                return Breach(self.name, f"event {e.type!r} has a dangling parent_id {e.parent_id}")
        return None


class ResourceBreach:
    """Were the run's budgets respected (steps and tokens)? This observer is a
    *backstop over the loop's own enforcement* (PHILOSOPHY.md §4 DoS): the loop
    checks the budget before and after every turn and terminates when it is hit,
    so on a correct loop this never fires. It fires only when that enforcement
    failed to hold — which is exactly the invariant the out-of-band judge should
    re-prove rather than take on trust.

    The bound is principled, not a fudge factor. Because the loop checks *between*
    turns, the most a respected budget can overshoot by is **one turn's worth**:
    the pre-turn check passed (cumulative < ``max_tokens``), then a single turn
    added at most ``max(per-turn usage)``. So any total above
    ``max_tokens + max_single_turn`` means a between-turn check was skipped or
    ignored — a real enforcement breach. (The previous ``max_tokens * 2`` slack
    was a guess; this is the actual worst-case a correct loop can produce.)
    """

    name = "verdict.resources"

    def inspect(self, run: ObservedRun) -> Breach | None:
        turns = sum(1 for e in run.events if e.type == ev.TURN_STARTED)
        if turns > run.budget.max_steps:
            return Breach(self.name, f"{turns} turns exceeds max_steps={run.budget.max_steps}")
        per_turn = [
            int(usage.get("total_tokens", 0))
            or (int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)))
            for e in run.events
            if e.type == ev.TURN_COMPLETED
            for usage in (e.payload.get("usage") or {},)
        ]
        tokens = sum(per_turn)
        # One turn of legitimate overshoot is allowed (the loop only checks between
        # turns); anything beyond that means the budget was not actually enforced.
        allowed = run.budget.max_tokens + (max(per_turn) if per_turn else 0)
        if run.budget.max_tokens and tokens > allowed:
            return Breach(
                self.name,
                f"{tokens} tokens exceeds max_tokens={run.budget.max_tokens} by more than "
                f"one turn's overshoot (allowed up to {allowed}) — budget not enforced",
            )
        return None


class NeighbourHealth:
    """Did the interop neighbours stay healthy — i.e. the run still reached a
    clean terminal state and no neighbour tool broke the harness? Attacks often
    try to take a neighbour down; a degraded run is a breach."""

    name = "verdict.neighbour"

    def __init__(self, neighbours: list[str] | None = None) -> None:
        self._neighbours = neighbours or []

    def inspect(self, run: ObservedRun) -> Breach | None:
        # A neighbour tool that raised an *unexpected* harness error (not a
        # deliberate, declared block) shows up as an error observation whose text
        # names an internal failure. We treat a tool that returned an error
        # mentioning a Python exception type from a neighbour as a health breach.
        for e in run.events:
            if e.type != ev.TOOL_RETURNED:
                continue
            tool = e.payload.get("tool")
            if tool not in self._neighbours:
                continue
            obs = e.payload.get("observation")
            if isinstance(obs, dict) and "error" in obs:
                return Breach(self.name, f"neighbour {tool!r} errored during the run: {obs['error']!r}")
        return None


class HostEffect:
    """Did any tool reach for a **host/filesystem/subprocess** capability? The
    plugins this gate reviews (web tools, detectors, validators) have no business
    writing the host filesystem or spawning a process; a declared host-effect
    capability is exactly the high-trust combination the mechanical envelope is
    meant to make *loud* (RED_TEAM.md §3, §6.3). Deterministic — it reads the
    declared envelope recorded on the log (``harness.envelope.declared``), so it
    decides out of band like every other observer. A plugin that legitimately
    needs a host effect is not auto-passed; it is surfaced for human trust review.
    """

    name = "verdict.host"
    _HOST_CAPS = frozenset({CAP_FS_WRITE, CAP_SUBPROCESS})

    def inspect(self, run: ObservedRun) -> Breach | None:
        # 1. OBSERVED, undeclared host effect (the real out-of-band check,
        #    RED_TEAM_CONTAINER.md §3.3): the monitor saw a filesystem write or a
        #    subprocess spawn from outside the target's userland. This fires on
        #    *behaviour*, not a declaration, so it catches an effect a tool never
        #    declared — the thing the declaration-only check below cannot.
        for e in run.events:
            if e.type == ev.HOST_EFFECT_OBSERVED:
                kind = e.payload.get("kind", "host-effect")
                where = e.payload.get("path") or e.payload.get("argv") or ""
                pid = e.payload.get("pid")
                tail = f" {where}" if where else ""
                tail += f" (pid {pid})" if pid is not None else ""
                return Breach(
                    self.name,
                    f"observed undeclared host effect {kind!r}{tail} — the target reached the host at runtime",
                )
        # 2. DECLARED host-effect capability: surfaced loud for human trust review
        #    (a web/detector/validator plugin has no business declaring fs:write /
        #    subprocess), even when no runtime effect was observed.
        for tool, caps in run.declared_caps.items():
            reached = self._HOST_CAPS.intersection(caps)
            if reached:
                return Breach(
                    self.name,
                    f"{tool} declares host-effect capability {sorted(reached)} "
                    "— requires human trust review, not an automated pass",
                )
        return None


# The default panel: every observer that needs no extra wiring. NeighbourHealth
# is added by the gate with the concrete neighbour list.
def default_observers() -> list[VerdictObserver]:
    return [EgressBreach(), ExfilBreach(), ProvenanceBreach(), ResourceBreach(), HostEffect()]


@dataclass
class GateVerdict:
    """The judge's decision over one bounded run (RED_TEAM.md §3 verdict rule)."""

    passed: bool
    breaches: list[Breach]
    objectives_covered: int
    coverage_met: bool

    def summary(self) -> str:
        if self.passed:
            return f"PASS — envelope held; {self.objectives_covered} objectives exercised"
        if not self.coverage_met:
            return f"FAIL — coverage not met ({self.objectives_covered} objectives); a silent run fails by rule"
        return "FAIL — " + "; ".join(b.observer + ": " + b.detail for b in self.breaches)


def render_verdict(
    run: ObservedRun,
    observers: list[VerdictObserver],
    *,
    objectives_attempted: list[str],
    min_coverage: int,
) -> GateVerdict:
    """Decide PASS/FAIL: PASS iff no observer fired AND coverage was met.

    ``coverage_met`` defends against a *suppressed* attacker (PHILOSOPHY.md §3):
    a run that exercised too little of the surface fails regardless of whether a
    breach was seen — a suspiciously quiet red-team run is itself a failure.
    """
    breaches = [b for o in observers if (b := o.inspect(run)) is not None]
    covered = len(set(objectives_attempted))
    coverage_met = covered >= min_coverage
    return GateVerdict(
        passed=not breaches and coverage_met,
        breaches=breaches,
        objectives_covered=covered,
        coverage_met=coverage_met,
    )
