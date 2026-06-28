"""The six extension points, as structural Protocols.

A plugin author implements the *shape* of a port without importing or
subclassing a Zu base class. The core depends only on these shapes, never on
a concrete adapter — which is what makes every adapter replaceable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, Field

from .content import Action, Observation
from .contracts import Event, Result
from .surface import SurfaceView

if TYPE_CHECKING:
    # ``Invariant`` lives in invariants.py, which imports MonitorState/Verdict/
    # RunContext FROM this module — importing it eagerly here would be a cycle.
    # The Pattern Protocol only needs it for typing (annotations are strings
    # under ``from __future__ import annotations``), so it is a type-only import.
    from .invariants import Invariant

# --- interface versioning (MLR §6) ---------------------------------------
#
# Each plugin port carries an interface MAJOR version. A plugin declares the
# major it was built against via a ``__zu_interface__`` class/instance attribute
# (an int); absent ⇒ 1, the original contract. The registry refuses a plugin
# whose declared major differs from the runtime's major for that port — the
# structural Protocol changed incompatibly, so loading it would fail in
# confusing ways. Bump a port's number here when its Protocol changes in a
# backward-incompatible way (e.g. a renamed/removed required member); a plugin
# built for the old contract is then refused with a clear error rather than
# half-working. Minor/compatible additions do NOT bump the major.
INTERFACE_VERSION: dict[str, int] = {
    "providers": 1,
    "tools": 1,
    "detectors": 1,
    "validators": 1,
    "backends": 1,
    "sinks": 1,
    "policies": 1,
    "triggers": 1,
    # Discovery/trust ports (the ModelProvider siblings for "find the site" and
    # "who is safe to transact with"; port shapes in this module, impls are plugins):
    "retrieval_providers": 1,  # RetrievalProvider — typed vendor/product discovery (#81)
    "reputation_providers": 1,  # ReputationProvider — computed merchant trust (#84)
    # Security-conformance ports (the port shapes live in this module; the
    # implementations are plugins, several in sibling packages):
    "gates": 1,  # InvocationGate — the pre-execution gate (ZU-CORE-2)
    "channels": 1,  # Channel — a harness-owned external channel (ZU-NET-2)
    "workload_identity": 1,  # WorkloadIdentity — attestable identity (ZU-NET-4)
    "egress_enforcement": 1,  # EgressEnforcement — pluggable default-deny (ZU-NET-1)
    "replay_arbiters": 1,  # ReplayArbiter — replay-divergence decision (ZU-RAIL-3)
    "monitors": 1,  # Monitor — stateful history-aware automaton over the log (ZU-RAIL-5)
    "patterns": 1,  # Pattern — recognize a surface archetype + emit rail invariants (§5)
    # CredentialBroker — the SCOPED, time-boxed, revocable, harness-held, fully-
    # audited capability to USE an instrument (a card, a vault, an inbox, an OAuth
    # grant) WITHOUT the policy ever holding the secret (§8: generalises inference-
    # credential containment to ALL credentials/instruments). The policy holds an
    # opaque capability handle; the broker uses the secret harness-side.
    "credential_brokers": 1,
}

# The attribute a plugin sets to declare the interface major it targets.
INTERFACE_ATTR = "__zu_interface__"

# --- model provider (the any-model seam) ---------------------------------


class Finish(str, Enum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"


class ToolCall(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)


class Capabilities(BaseModel):
    native_tools: bool = True
    vision: bool = False
    max_context: int = 128_000


class ModelRequest(BaseModel):
    messages: list[dict]
    tools: list[dict] = Field(default_factory=list)
    params: dict = Field(default_factory=dict)


class ModelResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish: Finish = Finish.STOP
    usage: dict = Field(default_factory=dict)


@runtime_checkable
class ModelProvider(Protocol):
    capabilities: Capabilities

    # The model id this provider calls, recorded into harness.turn.completed so
    # cost is attributable per model. None for the fake provider (no real model).
    # A read-only property so an adapter's concrete ``str`` attribute satisfies
    # it (covariant) without every implementer widening its own type.
    @property
    def model(self) -> str | None: ...

    async def complete(self, req: ModelRequest) -> ModelResponse: ...


# --- the generalised policy port (Engineering Design §9.2) ----------------
#
# ``ModelProvider.complete`` is the LLM-shaped seam: messages in, text + tool
# calls out. ``Policy`` is the *generalisation* of the decision-maker — typed
# observation in, typed action out — so a world-model controller or an embodied
# policy plugs into the SAME seam an LLM does, and the harness, bus, detectors,
# validation, escalation, and envelope are unchanged. An LLM is bridged onto it
# by an adapter (``zu_providers.llm_policy.LlmPolicy``) that turns the
# observation + tools into a ``ModelRequest``, calls ``complete``, and maps the
# response back to an ``Action``. A world model implements ``act`` directly.
#
# The interpreter loop is not rewritten to use this — it remains the LLM path.
# ``Policy`` is the forward-compatible port: "that single generalisation lets the
# decision-maker change without touching the runtime."


class ToolSpec(BaseModel):
    """What the policy is told it may do — a tool's name, description, and
    JSON-schema, decoupled from the concrete :class:`Tool` instance.

    The field is ``json_schema`` (not ``schema``) so it does not shadow
    ``BaseModel.schema``; it holds the same dict a :class:`Tool` exposes as
    ``.schema``.
    """

    name: str
    description: str = ""
    json_schema: dict = Field(default_factory=dict)


@runtime_checkable
class Policy(Protocol):
    capabilities: Capabilities

    @property
    def model(self) -> str | None: ...

    async def act(self, observation: Observation, tools: list[ToolSpec]) -> Action: ...


# --- the retrieval provider — the discovery sibling of ModelProvider (#81) -
#
# ``ModelProvider`` is the "act on a NAMED site" seam. ``RetrievalProvider`` is its
# missing sibling for "FIND the site": the user says *"buy a dog collar"* with no
# vendor, so discovery must return **typed candidates**, not raw HTML. That typing
# is the whole point — Zu's content-free discipline says the planning model never
# ingests untrusted prose, so discovery preserves it: a provider returns
# ``Candidate`` records (facts: title, url, domain, price, …), and shortlisting/
# ranking happens over a schema, never over persuasive page content. Backed by a
# structured shopping/search API or, as a fallback, the existing ``web_search``
# tool reduced to typed records. Egress is declared per provider (fail-closed, like
# every other capability), so discovery cannot reach outside its allowlist.


class RetrievalQuery(BaseModel):
    """The discovery request — the spec normalised to query terms + typed
    constraints. ``constraints`` is a free dict (``price_max``, ``ships_to``,
    ``in_stock``, …) a provider applies as far as its backend supports, so a new
    constraint needs no shape change here."""

    text: str
    constraints: dict = Field(default_factory=dict)
    limit: int = 20


class Candidate(BaseModel):
    """One typed discovery result — FACTS, never instructions.

    A candidate is the content-free unit the ranker scores: the planning model
    never has to read a product page to shortlist. ``price`` is in MINOR units
    (cents/pence) so money is an int, never a parsed-prose float. ``source`` is the
    provenance — which provider/feed produced it — so a verdict is auditable back
    to where the fact came from. Frozen + hashable so a shortlist round-trips on
    the event log and dedupes by identity."""

    model_config = {"frozen": True}

    title: str
    url: str
    domain: str
    price: int | None = None  # minor units (cents/pence); never a parsed-prose float
    currency: str | None = None
    in_stock: bool | None = None
    image: str | None = None
    source: str = ""  # which provider/feed produced it (provenance)


@runtime_checkable
class RetrievalProvider(Protocol):
    name: str

    async def search(self, query: RetrievalQuery) -> list[Candidate]: ...


# --- the reputation provider — computed, auditable merchant trust (#84) -----
#
# When the AGENT (not the user) picks the vendor, the system chooses *who gets the
# money* — a new attack surface (scam / SEO-poisoned / non-fulfilling shops). This
# is the seam for a **deterministic, auditable merchant-trust score** computed from
# external, **hard-to-forge** domain signals — NEVER from the page's persuasive
# content, so it is inherently injection-immune. It generalises the *static*
# domain-allowlist into a *computed* trust decision.
#
# Design principles a reference impl carries (see the deterministic scorer):
#   * Forge-resistance weighting — weight a signal by how expensive it is to fake
#     (HTTPS-present is weak; domain age + third-party review depth are strong).
#   * Asymmetry — strong NEGATIVES, weak positives (most good signals are
#     necessary-not-sufficient; most decisive signals are negatives).
#   * Hard gates (veto → REFUSE regardless of score): on a reputable blocklist, no
#     valid HTTPS, suspended/parked/sinkholed, or a high aggregator risk score.
#   * Two axes — a "malicious?" axis (threat-intel aggregators) AND a
#     "real shop that ships?" axis (registration, review depth+age, domain age);
#     a clean-but-non-fulfilling scam passes every malware check, so neither
#     axis alone suffices.
#   * Domain-level, not page-level — cacheable per registrable domain, immune to
#     on-page manipulation.


class ReputationVerdict(BaseModel):
    """The auditable trust decision for one registrable domain.

    ``band`` is the actionable outcome; ``score`` (0..100, documented weights) is
    the underlying number. ``gate`` is the veto reason when ``band == "refuse"``
    (``None`` otherwise) — a hard gate refuses regardless of score. ``signals`` is
    the per-signal value + weight (the auditable breakdown), and ``provenance``
    records which source produced each signal, so the whole verdict is replayable
    and contestable, never a black box."""

    model_config = {"frozen": True}

    band: str  # "trusted" | "caution" | "refuse"
    score: int  # 0..100
    gate: str | None = None  # the veto reason if band == "refuse"
    signals: dict = Field(default_factory=dict)  # per-signal value + weight
    provenance: dict = Field(default_factory=dict)  # which source produced each signal


@runtime_checkable
class ReputationProvider(Protocol):
    name: str

    async def assess(self, domain: str) -> ReputationVerdict: ...


# --- the capability envelope ---------------------------------------------
#
# The security thesis (see PHILOSOPHY.md §5–6): a plugin **declares** the
# capabilities it needs and the hosts it reaches, so its blast radius is visible
# in its own code and a runtime/sandbox can bound it mechanically. The
# declaration is the *what*; a SandboxBackend is the *enforcement*. These tokens
# are the machine-readable contract the gate's verdict observers compare actual
# behaviour against — without them, "least privilege" is prose, not a contract.

CAP_NET = "net"  # opens outbound network connections
CAP_SANDBOX = "sandbox"  # provisions/runs a sandbox (e.g. a tier-2 container)
CAP_FS_READ = "fs:read"  # reads the host filesystem
CAP_FS_WRITE = "fs:write"  # writes the host filesystem
CAP_SUBPROCESS = "subprocess"  # spawns a subprocess

# The egress allowlist sentinel for a plugin that legitimately needs the open
# internet (a general web-fetch tool, by definition). It is *not* a default:
# declaring it is the high-trust request PHILOSOPHY.md §6 says earns review.
# Any other egress entry is a specific host the plugin is allowed to reach;
# an empty ``egress`` means the plugin reaches nothing.
EGRESS_OPEN = "*"


def declared_envelope(plugin: Any) -> dict[str, Any]:
    """The capability envelope a plugin declares, read defensively.

    A plugin that omits ``capabilities``/``egress`` is treated as least
    privilege (no capabilities, no egress) — the safe default — so the
    declaration is opt-in for plugins and never crashes the loop on an older
    plugin that predates the fields. Returns plain sorted lists so the value is
    JSON-serialisable straight into the event log.
    """
    caps = getattr(plugin, "capabilities", None) or ()
    egress = getattr(plugin, "egress", None) or ()
    return {"capabilities": sorted(caps), "egress": sorted(egress)}


# --- in-loop ports -------------------------------------------------------


class Scope(str, Enum):
    PER_OBSERVATION = "per_observation"
    PER_TURN = "per_turn"
    ON_FINAL = "on_final"


class Severity(str, Enum):
    WARN = "warn"
    RETRY = "retry"
    ESCALATE = "escalate"
    # Block THIS one invocation (ZU-CORE-2): unlike TERMINAL (which ends the whole
    # run), DENY refuses a single tool call — the tool never executes, the model
    # gets an error observation and may try something else. Ranks above ESCALATE,
    # below TERMINAL in the loop's verdict ranking.
    DENY = "deny"
    TERMINAL = "terminal"


class Verdict(BaseModel):
    severity: Severity
    detector: str
    detail: str | None = None
    # Discriminates the *kind* of escalation (ZU-CD-1/2). ``None`` (the default)
    # keeps the existing behaviour — an ESCALATE climbs the capability tier.
    # ``"human"`` requests a human-in-the-loop pause on the specific invocation:
    # the run suspends and emits the literal invocation for approval. Backward
    # compatible: every existing detector/validator leaves this ``None``.
    kind: str | None = None


class RunContext(BaseModel):
    """A read-only view of the run, fleshed out as the loop is built.

    Detectors and validators read the run's event log through this context —
    how grounding confirms a value appears in retrieved content, and how
    history-dependent detectors ask their questions.
    """

    model_config = {"arbitrary_types_allowed": True}

    spec: Any
    # Populated by the loop (build step 4); kept Any so the core stays small.
    observation: Any = None
    # The run's event log as a *read-only* sequence: the loop hands plugins a
    # window that reflects the log as it grows but cannot be mutated through this
    # context (see ``loop._EventsView``). Typed ``Sequence`` to make that
    # read-only contract explicit rather than convention.
    events: Sequence = Field(default_factory=list)
    # --- security seams the gate/validators read at decision time --------------
    # Run-level taint (ZU-CD-3): True once this run ingested hostile input.
    tainted: bool = False
    # Run mode (ZU-RAIL-2): "execute" (default) or "explore"; a gate/tool reads it
    # to disarm in exploration. The loop also enforces it mechanically.
    mode: str = "execute"
    # Quarantined run-mode (#83): a tool-less, egress-free reader for processing
    # UNTRUSTED content. When True the loop offers the policy an EMPTY tool set and
    # refuses any tool call as a hard error — so prompt injection in the content is
    # STRUCTURALLY downgraded from a control-flow attack ("make the agent *do*
    # something") to a data-integrity one ("make it *believe* something"). A
    # tool-call attempt is itself a high-signal event (the content tried to act):
    # the loop surfaces ``harness.quarantine.escape_attempt`` and raises taint. This
    # is the "quarantined reader / privileged planner" (dual-LLM) pattern made a
    # provable mode, composing the domain-allowlist / declarative policies / content
    # fencing into one contract rather than reassembled per consumer.
    quarantined: bool = False
    # Durable per-grant state handle (ZU-CD-4): a ``GrantStore`` (kept ``Any`` so
    # the contract stays a thin value object, matching ``spec``/``observation``).
    grants: Any = None
    # The ``ToolCall`` currently under pre-execution check (ZU-CORE-2); ``None``
    # outside an InvocationGate ``check``.
    invocation: Any = None
    # The idempotency key minted for the invocation in flight (ZU-CORE-4); a tool
    # reads it to dedupe a retried side effect. ``None`` outside a tool call.
    idempotency_key: str | None = None
    # The blessed step annotations for the invocation in flight (ZU-RAIL-4):
    # ``{"consequence", "destination"}`` carried from the replayed rail step, so a
    # gate can gate divergence/instruments by the rail's content-free consequence
    # class without reading hostile content. ``None`` outside a replayed call.
    annotations: dict | None = None
    # Consume-once execution ledger (ZU-CD-6): an ``ExecutionLedger`` a tool/gate
    # can ``claim(key)`` against to make a side effect idempotent across instances
    # (kept ``Any`` like ``grants``). The loop itself claims before re-executing a
    # human-approved invocation on resume, so a double-resume can't double-execute.
    execution: Any = None


@runtime_checkable
class Tool(Protocol):
    name: str
    schema: dict
    prompt_fragment: str
    # The escalation ladder: a tool is only offered to the model once the run
    # has climbed to its tier. Tier 1 is the cheap default (http_fetch); a
    # higher tier (a browser via render_dom) is unlocked by a detector ESCALATE.
    # The loop reads it defensively (``getattr(tool, "tier", 1)``) so a tool
    # that omits it is treated as tier 1.
    tier: int

    # The capability envelope (see CAP_* / EGRESS_OPEN above). ``capabilities``
    # is the least-privilege set of capability tokens the tool needs; ``egress``
    # is its host allowlist (``{EGRESS_OPEN}`` for the reviewed open-internet
    # case, empty for none). Both are read defensively via ``declared_envelope``,
    # so a tool that omits them is treated as needing nothing — the safe default.
    capabilities: frozenset[str]
    egress: frozenset[str]

    async def __call__(self, ctx: RunContext, **kwargs: Any) -> dict: ...


@runtime_checkable
class Detector(Protocol):
    name: str
    scope: Scope

    def inspect(self, ctx: RunContext) -> Verdict | None: ...


@runtime_checkable
class Validator(Protocol):
    name: str

    def check(self, result: Result, ctx: RunContext) -> Verdict | None: ...


# --- the stateful, history-aware Monitor (§1.7 deterministic rail) ---------
#
# A ``Detector`` judges a SINGLE observation/turn/final (``Scope``). A ``Monitor``
# is its stateful generalisation: it folds the WHOLE event history via
# ``ctx.events`` (the read-only ``_EventsView``) and returns the current state of
# a deterministic automaton over that stream — the temporal-property checker a
# Detector cannot be. It is PURE: a function of the event history, no model, no
# I/O (the deterministic machinery DISPOSES). That purity keeps it LTL-compilable
# later — an LTL→Monitor compiler emits an object satisfying THIS SAME shape with
# no caller change.
#
# The verdict vocabulary is deliberately policy-NEUTRAL (OK/WARN/VIOLATION), kept
# separate from ``Severity``: the Monitor→Severity bridge lives in the loop, not
# in the port, so the automaton stays a pure property and the runtime owns the
# escalation semantics (VIOLATION→TERMINAL, WARN→record-and-continue in v1).
# ``evaluate`` returns ``None`` when inert (mirrors ``Detector.inspect``).


class MonitorState(str, Enum):
    OK = "ok"
    WARN = "warn"
    VIOLATION = "violation"


class MonitorVerdict(BaseModel):
    monitor: str
    state: MonitorState
    detail: str | None = None
    # The ctx.events index the verdict was decided at (the folded step); optional.
    step: int | None = None


@runtime_checkable
class Monitor(Protocol):
    name: str

    def evaluate(self, ctx: RunContext) -> MonitorVerdict | None: ...


# --- the pre-execution gate (ZU-CORE-2) ----------------------------------
#
# Detectors and validators are *post-hoc*: they judge an observation that already
# exists (i.e. the tool already ran) or the final result. An ``InvocationGate``
# is the missing *pre-execution* seam — the harness runs every registered gate
# inside ``_invoke`` BEFORE the tool body executes, against the literal
# ``ToolCall``. This is what lets a consumer enforce ``invocation ⊆ grant ⊆
# consent`` + limits *beneath* the policy, on every call, before the side effect
# fires. The gate cannot be bypassed or disabled: the loop always iterates the
# gate list and the model only produces the ``ToolCall`` that is the gate's
# input, never its control. ``check`` returns ``None`` (allow — the inert
# default, exactly like a detector that does not fire), a ``DENY`` verdict (block
# this call, no side effect), or an ``ESCALATE`` verdict (route to the tier-climb
# or, with ``kind="human"``, the human-approval pause).


@runtime_checkable
class InvocationGate(Protocol):
    name: str

    def check(self, call: ToolCall, ctx: RunContext) -> Verdict | None: ...


# --- durable per-grant state for the gate/validators (ZU-CD-4) ------------
#
# Cumulative limits ("$X/hour", "N transactions/window") need state that
# persists across invocations, not just the single call. ``GrantStore`` is a
# deliberately tiny keyed get/put scoped by a ``grant_id`` the consumer supplies
# — NOT a database (no query, no iteration, no transactions); that poverty is
# what keeps it from bloating the core. The in-memory default
# (``zu_core.grants.InMemoryGrantStore``) is a cache over ``harness.grant.updated``
# events (the log stays the source of truth, so resume rebuilds counters); a
# durable backing (SQL/Redis) is a plugin the harness injects.
#
# CONCURRENCY — read this before building a cap on ``get``/``put``: that pair is
# NOT atomic. Under concurrent execution (tasks sharing a grant, a retried side
# effect, a multi-worker deployment) two invocations can each read the same
# spent-so-far, both pass an under-cap check, and both proceed — a lost update
# that silently overshoots the cap (a real over-spend for a money grant). Enforce
# cumulative limits with ``incr_if_below`` instead, which backends implement
# atomically (a SQL ``UPDATE ... WHERE val+delta<=ceiling``, a Redis Lua/WATCH);
# the in-memory default does it under a lock. ``get``/``put`` remain fine for
# single-writer / serial-replay state.


@runtime_checkable
class GrantStore(Protocol):
    def get(self, grant_id: str, key: str, default: Any = None) -> Any: ...

    def put(self, grant_id: str, key: str, value: Any) -> None: ...

    def incr_if_below(
        self, grant_id: str, key: str, delta: Any, ceiling: Any, default: Any = 0
    ) -> bool:
        """Atomic check-and-increment: if ``current + delta <= ceiling`` commit the
        new value and return ``True``, else leave it unchanged and return ``False``.
        The concurrency-safe primitive for cumulative caps (``get``/``put`` is
        TOCTOU-racy — see the note above). A committed increment journals like
        ``put`` so the event log stays the source of truth."""
        ...


# --- consume-once / idempotent execution for human approvals (ZU-CD-6) ----
#
# A human approval (ZU-CD-1/2) authorises exactly ONE irreversible side effect, and
# that "once" must survive across component/process lifetimes: a fresh runner that
# resumes the same resolved approval MUST NOT execute the side effect again. The
# footgun is keeping the "already done" flag per-instance — a new instance silently
# resets it and double-executes. ``ExecutionLedger`` is the durable, atomic
# consume-once primitive the loop consults before re-executing a human-approved
# invocation on resume; the in-memory default (``zu_core.ledger.InMemoryExecutionLedger``)
# is a cache over ``harness.execution.claimed`` events (the log stays source of
# truth, so resume rebuilds the claimed set); a durable backing (SQL
# ``INSERT ... ON CONFLICT DO NOTHING``, Redis ``SET NX``) is a plugin the harness
# injects. A consumer's own tool/gate may ``claim`` against it too (via
# ``ctx.execution``) to make any side effect idempotent on its idempotency key.


@runtime_checkable
class ExecutionLedger(Protocol):
    def claim(self, key: str) -> bool:
        """Atomically claim ``key`` for execution: ``True`` for the first caller
        (proceed), ``False`` for every later caller — a replay/resume/retry —
        (already executed, refuse). A first claim journals so the log stays the
        source of truth and a resumed run sees the key as taken."""
        ...


# --- the replay-divergence arbiter (ZU-RAIL-3) ---------------------------
#
# When a recorded rail (``Track``) is replayed and a step's live observation
# diverges from the recorded path, *something* must decide what to do. Zu's
# built-in divergence handling is coarse and one-directional: a hard challenge
# hands the frontier to the MODEL (tier climb). A consumer running delegated
# action needs more — to surface BOTH the recorded step and the live observation
# to its own decision component and to escalate a *consequential* drift to a
# HUMAN, not the model. ``ReplayArbiter`` is that seam: the loop calls ``decide``
# per replayed step with the recorded ``step``, the live ``observation``, and the
# run ``ctx`` (which carries the step's consequence/destination annotations and
# the taint/mode flags), and HONOURS the returned outcome — including pausing for
# a human. The arbiter holds the *policy* (the structural-diff metric, the novelty
# test, the thresholds, patch-validation) — deliberately downstream, because that
# judgment is domain-specific and gameable and must iterate outside the trusted
# core. With no arbiter registered the loop's existing behaviour is unchanged.


class ReplayDecision(str, Enum):
    CONTINUE = "continue"  # the drift is within the rail; keep replaying
    HANDOFF = "handoff"  # hand the frontier to the model (Zu's existing default)
    ESCALATE = "escalate"  # pause for a HUMAN to approve this exact step
    STOP = "stop"  # abort the run (terminal)


@runtime_checkable
class ReplayArbiter(Protocol):
    name: str

    def decide(self, step: Any, observation: Any, ctx: RunContext) -> ReplayDecision: ...


# --- infrastructure ports ------------------------------------------------


@runtime_checkable
class SandboxBackend(Protocol):
    """Provisions and execs inside an isolated environment (a container/microVM).

    ``launch`` takes a free-form spec dict so an adapter can grow new isolation
    knobs without changing this shape. The red-team container form
    (RED_TEAM_CONTAINER.md) reads, in addition to the existing
    ``image``/``network``/cap-drop keys:

      * ``network: "isolated"`` — attach to a network with no default route, so
        the egress proxy is the *only* path off-box (default-DROP is the real
        enforcement; the proxy env below is only a convenience);
      * ``proxy: {host, port}`` — the egress proxy to route through (sets
        HTTP(S)_PROXY in the container);
      * ``ca_cert`` — a per-run MITM CA to trust *inside the container only*
        (P2, for HTTPS payload inspection);
      * ``seccomp`` / ``audit`` — a syscall profile / fs-audit toggle for the
        host-effect monitor (P3).

    An adapter ignores keys it does not implement, so a spec is forward-compatible
    across phases."""

    async def launch(self, spec: dict) -> Any: ...

    async def exec(self, sandbox: Any, call: ToolCall) -> dict: ...

    async def destroy(self, sandbox: Any) -> None: ...


@runtime_checkable
class BrowserSessionHandle(Protocol):
    """A live, stateful session inside a sandbox: send a command, get a response,
    and close it. The state persists between ``send`` calls — what lets a tool
    drive a multi-step flow incrementally instead of one-shot."""

    async def send(self, cmd: dict) -> dict: ...

    async def close(self) -> None: ...


@runtime_checkable
class SessionBackend(Protocol):
    """A SandboxBackend that can also open a PERSISTENT session — a long-lived
    process inside a kept-alive sandbox, holding state across many commands (e.g. a
    browser the model drives open→act→read→close). Separate from
    :class:`SandboxBackend` so a one-shot adapter need not implement it."""

    async def open_session(self, spec: dict) -> BrowserSessionHandle: ...


@runtime_checkable
class EgressProxy(Protocol):
    """The control-plane egress proxy: the target container's sole route off-box,
    and the *authoritative* record of where it actually went (RED_TEAM_CONTAINER.md
    §3.1). It is out of band — the target routes through it but cannot read its
    log or config — so its connection record is a fact the judged cannot author.

    ``launch`` starts the proxy for one run against a host allowlist (the union of
    the target tools' declared ``egress``; ``EGRESS_OPEN`` permits any host) and
    returns an opaque handle carrying its ``{host, port}``. ``connections``
    returns the JSONL connection log as dicts — ``{client, host, ip, port,
    scheme, bytes_out, allowed}`` — one per CONNECT/request, including refused
    (``allowed: false``) attempts. ``close`` tears it down."""

    async def launch(self, spec: dict) -> Any: ...

    def connections(self, handle: Any) -> list[dict]: ...

    async def close(self, handle: Any) -> None: ...


@runtime_checkable
class HostEffectMonitor(Protocol):
    """The control-plane host-effect monitor: the out-of-band record of what the
    target did to the filesystem / process table from *outside* its userland
    (RED_TEAM_CONTAINER.md §3.3, P3). Like the egress proxy, it observes the
    target rather than trusting it, so an undeclared fs-write or subprocess is a
    fact the plugin cannot suppress.

    ``collect`` is called after the run, before teardown (it needs the live
    sandbox), and returns host-effect facts as ``{kind, path|argv, pid?}`` dicts —
    e.g. ``{"kind": "fs:write", "path": "/etc/cron.d/x"}``. The default Docker
    implementation reads the container's filesystem diff; a seccomp/audit source
    can feed subprocess/syscall facts through the same shape later."""

    async def collect(self, sandbox: Any, backend: Any) -> list[dict]: ...


# --- the generalised harness-owned channel (ZU-NET-2) --------------------
#
# Zu already owns one external channel: inference. The model key is held inside
# the provider adapter (resolved from env at call time), never placed in the
# model's context; the policy emits a typed ``ModelRequest`` and gets a
# ``ModelResponse`` but cannot read or reconfigure the channel. ``Channel``
# generalises exactly that ownership to an *arbitrary* typed external endpoint —
# the credential broker being the motivating case. The harness/operator
# constructs the channel with an env-named secret; the policy emits a typed
# ``ChannelRequest`` (a verb + args) and gets a ``ChannelResponse``, and can
# neither read the credential nor change the channel's destination. ``endpoint``
# is an opaque label for the log, NEVER the credential. ``ModelProvider`` is the
# special case kept as its own hot-path port; ``Channel`` is the general seam for
# non-inference harness-owned endpoints. An out-of-process broker is a
# ``Channel`` whose secret lives in a separate process (see ``zu_core.rpc``).


class ChannelRequest(BaseModel):
    op: str  # the endpoint verb, e.g. "mint" | "exchange" | "introspect"
    args: dict = Field(default_factory=dict)


class ChannelResponse(BaseModel):
    ok: bool = True
    data: dict = Field(default_factory=dict)
    error: str | None = None


@runtime_checkable
class Channel(Protocol):
    endpoint: str  # opaque label for the log; never the credential

    async def call(self, req: ChannelRequest) -> ChannelResponse: ...


# --- the credential broker — scoped, time-boxed, revocable, audited USE of an
#     instrument WITHOUT the policy ever holding the secret (§8) ---------------
#
# THE THESIS. Two things, and conflating them is the trap:
#   * the INSTRUMENT (a card via an issuer, a vault/KMS, an inbox, an OAuth
#     grant) — it EXISTS, or a THIRD PARTY issues it; Zu integrates it, never
#     becomes it;
#   * the CONTAINMENT problem — how the agent USES the instrument without ever
#     holding the secret, exceeding scope, overspending, or being hijacked.
# Zu builds the CONTAINMENT / ACCESS layer, NEVER the instrument. The reference
# instrument here is FAKE; a real issuer is a FUTURE pluggable adapter behind the
# ``Instrument`` seam below — never in zu-core (which imports nothing but pydantic).
#
# THE ONE PRIMITIVE. A scoped, time-boxed, revocable, harness-held, fully-audited
# capability to USE an instrument, where the policy only ever gets "a door already
# locked behind it", NEVER the secret. The policy holds an opaque capability
# HANDLE (``Grant.id``); the broker holds the secret (behind the ``Instrument``)
# and uses it harness-side; every use lands on the hash-chained audit log bound to
# the consent that justified it.
#
# This is the SAME ownership ``ModelProvider``/``Channel`` already have for the
# inference key, generalised to an arbitrary instrument: the harness/operator
# constructs the broker with (a reference to) an ``Instrument``; the policy emits
# a typed ``UseRequest`` (a verb + args + a consent_ref) and gets a ``UseOutcome``
# (an outcome dict — a charge id, never the PAN/token), and can neither read the
# secret nor exceed scope/limit/TTL/revocation. For the strongest boundary the
# broker is wrapped as a ``Channel`` (op="use") and run out-of-process via
# ``zu_core.rpc`` + ``zu_backends.oop_launcher`` — then the secret lives in a
# separate process/uid (ZU-CORE-3 / ZU-NET-3 / ZU-EXT-4) and a harness compromise
# yields the socket, not the secret.


class Consent(BaseModel):
    """Who authorized a capability + for what + the proof/authority (audit-bound
    on EVERY use, so "acted-within-granted-authority" is provable from the log).

    ``authority`` is the proof/justification — an approval_id, a signed-token ref,
    a parent grant — not the secret. ``Consent`` is a pure authority object: it
    grants the right to USE an instrument, it is not a balance/ledger/settlement
    record (containment, never issuance)."""

    model_config = {"frozen": True}

    consent_id: str
    by: str  # the principal who authorized (a human, a parent grant)
    authority: str  # the proof/justification (an approval_id, a signed-token ref)
    purpose: str = ""  # what it was authorized for (audit-readable)


class CapScope(BaseModel):
    """The allowed operations + constraints a capability is bounded to. The policy
    cannot exceed this — the broker refuses (and logs) any use outside it.

    ``payees`` is an allowlist of permitted recipients (``None`` ⇒ the op-set is
    the only constraint). ``requires_human_over`` is the per-use amount above which
    a use is HIGH-CONSEQUENCE and routes to the human-in-the-loop pause BEFORE the
    instrument operation (the existing ``Verdict(kind="human")`` path), never
    silently through."""

    model_config = {"frozen": True}

    operations: frozenset[str] = frozenset()  # allowed ops, e.g. {"charge"}
    payees: frozenset[str] | None = None  # recipient allowlist; None ⇒ op-set only
    requires_human_over: float | None = None  # per-use amount → HITL above this


class Grant(BaseModel):
    """A SCOPED, time-boxed, revocable capability to USE an instrument — the ONE
    primitive. ``id`` is the OPAQUE capability handle the policy holds; everything
    authority-bearing (the ``instrument_ref``, the secret behind it) stays
    harness-side. The model is frozen: an issued capability is immutable; revoke is
    state the broker holds, and cumulative spend accrues in a ``GrantStore`` keyed
    by ``id`` — not by mutating the Grant.

    REUSES ``GrantStore.incr_if_below`` for the cumulative cap (the atomic
    check-and-increment that closes the TOCTOU race two concurrent uses would
    otherwise drive through an under-cap check)."""

    model_config = {"frozen": True}

    id: str = Field(default_factory=lambda: uuid4().hex)  # the OPAQUE handle
    instrument_ref: str  # which Instrument (an opaque label); NEVER the secret
    scope: CapScope
    per_use_limit: float | None = None  # max single-use amount
    cumulative_limit: float | None = None  # max spend over the window (incr_if_below)
    cumulative_key: str = "spent"  # the GrantStore key the cumulative cap accrues under
    ttl_s: int | None = None  # seconds from created_at; None ⇒ no expiry
    consent: Consent  # the authorizing consent (audit-bound on every use)
    # Whether every USE must NAME a matching consent (the default — consent is
    # PRESENCE-enforced, not just mismatch-checked: a use with no consent_ref is
    # refused ``no_consent``). A grant may opt OUT explicitly (a back-office/batch
    # grant whose single issuing consent covers all uses) by setting this False.
    requires_consent: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked: bool = False  # revoke() flips this in the broker's grant table

    def expired(self, now: datetime) -> bool:
        """True once ``now`` is past ``created_at + ttl_s`` (``ttl_s is None`` ⇒
        never expires). Time is supplied by the caller so the check is pure and
        testable; the broker passes ``datetime.now(UTC)``."""
        return self.ttl_s is not None and (now - self.created_at).total_seconds() > self.ttl_s


class UseRequest(BaseModel):
    """The policy's request to USE a capability — PURE DATA, carries NO secret.

    ``capability_id`` is the opaque handle the policy holds; ``operation``/``args``
    are the verb and its parameters (e.g. ``{"amount": 1200, "payee": "acct_42"}``).
    ``consent_ref`` names which approval/consent authorizes THIS use (the audit
    binding). ``idempotency_key`` dedupes a retried side effect (reusing the
    ZU-CORE-4 key shape). There is no field on this type that could carry a secret
    — the boundary is mechanical, not asked-nicely."""

    capability_id: str
    operation: str  # e.g. "charge" | "issue_token" | "send"
    args: dict = Field(default_factory=dict)
    consent_ref: str | None = None  # which consent authorizes THIS use (audit binding)
    idempotency_key: str | None = None  # dedupe a retried side effect


class UseOutcome(BaseModel):
    """The OUTCOME of a use — a charge id, a status, never the PAN/token.

    On allow, ``outcome`` is the instrument's returned outcome dict
    (``{"charge_id": ..., "status": "captured"}``) — the secret never appears here.
    On refusal, ``ok`` is False and ``refused`` is the machine code
    (``scope`` | ``per_use`` | ``cumulative`` | ``expired`` | ``revoked`` |
    ``no_consent`` | ``requires_human``); the instrument was NOT touched."""

    ok: bool = True
    outcome: dict = Field(default_factory=dict)  # {"charge_id": ...}; NEVER a secret
    refused: str | None = None
    detail: str | None = None


@runtime_checkable
class Instrument(Protocol):
    """The pluggable issuer/vault seam (a FUTURE real issuer is an adapter here,
    not this build). The Instrument ALONE holds the secret; the broker calls
    ``perform``; the secret NEVER crosses back to the broker or the policy.

    ``ref`` is an opaque label for the log (e.g. ``"card:fake-001"``), NEVER the
    secret. ``perform`` does the real operation USING the secret it alone holds and
    returns only the OUTCOME (a charge id, a derived token used internally) — the
    one place a secret is touched, and it stays behind this boundary."""

    ref: str

    async def perform(self, operation: str, args: dict) -> dict: ...


@runtime_checkable
class CredentialBroker(Protocol):
    """The harness-side broker: holds a reference to the secret (via an
    ``Instrument``) and exposes ONLY scoped capabilities. The policy NEVER receives
    a secret — it holds an opaque ``capability_id`` and gets a ``UseOutcome``.

    ``grant`` registers a :class:`Grant` and returns its opaque id (the handle).
    ``use`` checks the grant (scope, per-use + cumulative limits via
    ``incr_if_below``, TTL/expiry, NOT revoked, the authorizing consent) and IF
    allowed performs the instrument operation USING the secret INTERNALLY, records
    the use as an audit event bound to the grant + consent, and returns only the
    outcome; it refuses (and logs a defense.blocked-style event) on any scope/limit/
    TTL/revocation failure. ``revoke`` flips a grant revoked so subsequent use is
    refused. ``name`` lets it register/version like every other port."""

    name: str

    def grant(self, grant: Grant) -> str: ...

    async def use(self, req: UseRequest) -> UseOutcome: ...

    def revoke(self, grant_id: str) -> None: ...


# --- workload identity (ZU-NET-4) ----------------------------------------
#
# The harness presents an attestable identity on a channel; the peer verifies it;
# the verified peer principal is recorded per action (under ``payload["ctx"]
# ["peer"]``, the ZU-AUDIT-3 convention). Workload identity is a *precondition*
# for authorization, never a substitute for it — the consumer's grant remains the
# authority. The mechanism is pluggable: a static-mTLS reference impl now, SPIFFE
# later, each a plugin. ``IdentityProof.proof`` is opaque and scheme-specific and
# MUST NOT carry a private key. ``proof`` MAY carry an attestation measurement
# (ZU-NET-5, SHOULD); a verifier degrades to identity-only when absent.


class IdentityProof(BaseModel):
    scheme: str  # "static-mtls" | "spiffe" | ...
    principal: str  # e.g. "spiffe://zu/agent/vet" or a cert subject
    proof: dict = Field(default_factory=dict)  # opaque; no private key


@runtime_checkable
class WorkloadIdentity(Protocol):
    scheme: str

    def present(self) -> IdentityProof: ...

    def verify(self, proof: IdentityProof) -> str | None: ...


# --- pluggable egress enforcement (ZU-NET-1) -----------------------------
#
# Distinct from ``EgressProxy`` (which *observes* and *allows*): this is the
# network policy that *prevents bypass* — the default-DROP that makes the proxy
# the only path off-box. Making it a port means the *mechanism* (Docker internal
# network, nftables, WireGuard) is interchangeable without writing a whole new
# SandboxBackend. ``apply`` installs the policy for one run against ``spec``
# ({"allowlist", "dns": "pin"|"deny"|[hosts], "proxy": {...}}) and returns a
# handle; ``revoke`` tears it down. Gating DNS is part of the contract — the
# embedded resolver is a covert egress channel L3 routing alone won't catch.


@runtime_checkable
class EgressEnforcement(Protocol):
    async def apply(self, spec: dict) -> Any: ...

    async def revoke(self, handle: Any) -> None: ...


@runtime_checkable
class EventSink(Protocol):
    """The canonical event store — the single source of truth for a run.

    ``append`` is idempotent on ``event_id`` (re-appending the same event is a
    no-op), so a retried publish never duplicates a record. It returns the stored
    event — the canonical store links it into the per-trace hash chain (ZU-AUDIT-1)
    and returns the *linked* copy so the bus fans that out to shippers; a sink
    that does not link may return ``None`` and the bus keeps the input event. A
    filter value of ``None`` matches ``IS NULL`` (e.g. ``{"parent_id": None}``
    selects roots).

    Reads come in two shapes so a large log never has to be materialised at
    once: ``query`` for a bounded window (always pass ``limit`` for big logs),
    and ``stream`` for memory-safe iteration over the whole result via keyset
    pagination.
    """

    async def append(self, event: Any) -> Event | None: ...

    async def query(
        self, flt: dict | None = None, *, limit: int | None = None, after_seq: int = 0
    ) -> list: ...

    def stream(
        self, flt: dict | None = None, *, batch_size: int = 500
    ) -> AsyncIterator[Any]: ...

    async def count(self, flt: dict | None = None) -> int: ...


# --- trigger — the inbound mirror of EventSink (Engineering Design §4.4) ---
#
# EventSink emits events *out*; a Trigger listens for events *in* and starts a
# run. Email, webhook, queue (SQS/Kafka/PubSub), schedule, an object-storage
# write, or another agent's event — each is a Trigger plugin, discovered and
# configured exactly like a Tool (the ``zu.triggers`` group). A trigger carries
# UNTRUSTED input, which is exactly why the capability envelope matters: the
# payload is attacker-controlled, so it is typed and recorded as such and never
# treated as authoritative.


class TriggerEvent(BaseModel):
    """What woke the agent — typed, and put on the log at run start.

    ``payload`` is UNTRUSTED: it is whatever an external party sent (an email
    body, a webhook JSON, a queue message). It is data to act on under the
    envelope, never an instruction the harness obeys.
    """

    source: str  # 'email' | 'webhook' | 'queue' | 'schedule' | 'object-store' | ...
    payload: dict = Field(default_factory=dict)
    # Tag a source as HOSTILE (ZU-CD-3). The core loop has no TriggerEvent
    # ingress — the caller bridges a trigger to a run — so this is a typed tag the
    # bridge maps onto ``TaskSpec.tainted``. All trigger payloads are untrusted by
    # contract regardless; ``hostile`` marks the stronger "treat as adversarial,
    # force high-consequence actions to escalate" stance for that source.
    hostile: bool = False


@runtime_checkable
class Trigger(Protocol):
    # The source label this trigger stamps onto every event it yields.
    source: str

    def listen(self) -> Iterator[TriggerEvent]: ...


# --- the pattern port — the policy-prior / move-ordering layer (§5) --------
#
# A UI is a state space; the Action Surface is the move generator (legal moves).
# A ``Pattern`` is a POLICY PRIOR over that surface — the AlphaZero-shape move
# ordering, NOT a Deep-Blue brute-force enumerator. It RECOGNIZES a situation
# (login form, cookie banner, search box, paginated list, …) over a core
# ``SurfaceView`` and PROPOSES the canonical interaction, with success criteria
# and known failure modes attached. It is READ-ONLY: it recognizes and emits
# declarative invariants; it NEVER calls a tool and NEVER decides the task action
# (that is the policy/search). A recognized pattern is a PRIOR TO BE CONFIRMED BY
# OBSERVATION, never ground truth — its success criteria compile (via
# ``zu_core.invariants.compile_spec``) to Monitors the rail VERIFIES, and a
# behaviour mismatch fires a detector (ZU-RAIL-9), it is not trusted blindly.
#
# The contract takes the CORE ``SurfaceView`` (zu_core.surface), never zu-tools'
# ``Surface`` — zu-core cannot import zu-tools. zu-tools projects its ``Surface``
# onto ``SurfaceView`` through a thin one-way adapter; a pattern speaks only the
# core type, so zu-patterns depends only on zu-core.


class PatternStep(BaseModel):
    """One canonical interaction step the prior PROPOSES — as HANDLES/role
    predicates, never selectors and never a site-specific magic constant.

    ``op`` is a generic verb (``fill`` | ``click`` | ``select`` | ``submit`` |
    ``expect``). ``role``/``label_hint`` are predicates the recognizer DERIVED
    from the surface (a substring/normalized match it bound), used to re-select
    the affordance at run time. The step is a proposal; the policy or guided
    search decides whether to follow it. A pattern never executes it.
    """

    model_config = {"frozen": True}

    op: str
    role: str | None = None
    label_hint: str | None = None
    note: str = ""


class RecognitionResult(BaseModel):
    """What a pattern's ``recognize`` returns when it fires — archetype, a
    confidence in ``[0, 1]``, the affordance handles it bound, and the proposed
    interaction script. ``None`` from ``recognize`` means no match (fall through
    to the model + safe search)."""

    model_config = {"frozen": True}

    archetype: str
    confidence: float
    matched_handles: tuple[str, ...] = ()
    script: tuple[PatternStep, ...] = ()
    detail: str | None = None
    # The control's DECLARED OUTCOME — generic, content-free tokens describing the
    # surface acting on it produces ("subscribed", "basket"/"order", "signed in").
    # The bridge from "what a control is" to "does it advance the goal" (outcome
    # inference, #69): zu_patterns.goal_progress scores a goal's tokens against
    # THESE, so a control is off-path because of its outcome, not its name. A
    # pattern that declares no outcome scores as UNKNOWN (never off-path).
    outcome: tuple[str, ...] = ()
    # Whether the outcome is TERMINAL (a dead-end side-quest — newsletter, spin-to-
    # win, survey: engaging it only wastes a step or springs an anti-bot wall) vs
    # NAVIGATIONAL (a legitimate MEANS to the goal — search, login, pagination:
    # off-path by outcome, but must not be avoided). #71: lets a consumer safely
    # AVOID terminal side-quests during navigation while still USING navigational
    # tools. Default False — most controls are a means, not a dead end.
    terminal: bool = False


@runtime_checkable
class Pattern(Protocol):
    name: str
    archetype: str

    # Cheap/deterministic classification over the CORE SurfaceView. Returns a
    # RecognitionResult (archetype + confidence + script + matched handles) or
    # None (no match). It ENUMERATES/CLASSIFIES; it MUST NOT decide the task
    # action (that is the policy's job).
    def recognize(self, surface: SurfaceView) -> RecognitionResult | None: ...

    # Success criteria as declarative Invariants the rail VERIFIES (§1 reuse):
    # the predicted "done" state. Pure data — compiled by zu_core.invariants.
    def success_invariants(self, result: RecognitionResult) -> list[Invariant]: ...

    # Known failure modes as declarative Invariants whose breach is a detector
    # firing (the pattern was a wrong prior — caught, never silently obeyed).
    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]: ...
