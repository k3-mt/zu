"""The six extension points, as structural Protocols.

A plugin author implements the *shape* of a port without importing or
subclassing a Zu base class. The core depends only on these shapes, never on
a concrete adapter — which is what makes every adapter replaceable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
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
    # The connected-surface family — web Action Surfaces bound to an external CDP
    # target a HOST owns, reusing the AX-tree reduction (shadow/frame flattened)
    # instead of a hand-rolled DOM walk (#93/#94/#95). Port shapes below.
    "connected_surfaces": 1,  # ConnectedSurface — perceive/act over an external CDP target (#93)
    "consent_resolvers": 1,  # ConsentResolver — deterministic cookie/consent dismissal (#94)
    "selection_satisfiers": 1,  # SelectionSatisfier — satisfy required variant selects (#95)
    "checkout_proceeders": 1,  # CheckoutProceeder — advance add-to-cart -> checkout, short of commit (#117)
    "cart_adders": 1,  # CartAdder — deterministic product -> cart, recognise/click/verify (#122)
    "funnel_phase_classifiers": 1,  # FunnelPhaseClassifier — where a page sits in the funnel (#121)
    # The interaction-primitive family (#125): ONE closed vocabulary of generic,
    # self-locating, verified moves (dismiss/search/choose_one/advance/commit_stop)
    # the vertical resolvers above reduce to, plus the composition layer that drives
    # them. Port shapes below; reference impls are plugins in zu-tools.
    "interaction_primitives": 1,  # InteractionPrimitive — one generic verified move (#125)
    "primitive_runtimes": 1,  # PrimitiveRuntime — dispatch {kind,hint} over the family (#125)
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
    image: str | None = None  # the PRIMARY/thumbnail (== images[0] when a gallery is present)
    images: tuple[str, ...] = ()  # the full product image gallery, in page order — URLs are
    # facts (same category as ``price``/``image``), so this keeps the content-free guarantee:
    # the planning model still never ingests page content to use them. A tuple stays hashable,
    # so a candidate carrying a gallery still round-trips on the event log and dedupes by identity.
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

    band: Literal["trusted", "caution", "refuse"]
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

# Documented open aliases (C1). Some ``RunContext`` fields must stay open — the
# core is policy-agnostic and cannot name the concrete type without importing a
# plugin/policy. Rather than a bare ``Any`` (which says nothing), these aliases
# NAME the intent at the seam: ``spec`` is a ``TaskSpec``-shaped object,
# ``observation`` is a policy-shaped value (typically a ``dict``). They are
# ``Any`` so the seam stays open; the alias is the documentation.
TaskSpecLike = Any  # a TaskSpec-shaped object (zu_core.contracts.TaskSpec in the loop)
ObservationLike = Any  # a policy-shaped observation value (usually a dict)


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

    Typing discipline (C1): fields whose shape is genuinely knowable are typed
    to a port Protocol (``grants: GrantStore``, ``execution: ExecutionLedger``,
    ``invocation: ToolCall``) or a concrete contract, so a plugin gets real
    completion/type-checking through the ctx. The two fields that MUST stay open
    — ``spec`` and ``observation`` — are narrowed to the documented aliases
    ``TaskSpecLike`` / ``ObservationLike`` (both ``= Any``): the loop passes a
    :class:`~zu_core.contracts.TaskSpec` as ``spec`` and a policy-shaped
    observation (usually a ``dict``) as ``observation``, but neither type is
    imported into every port shape, so the alias documents the intent while
    keeping the seam policy-agnostic. ``arbitrary_types_allowed`` lets the
    Protocol-typed fields hold a concrete plugin instance without validation.
    """

    model_config = {"arbitrary_types_allowed": True}

    # ``spec`` is a TaskSpec-shaped object (``zu_core.contracts.TaskSpec`` in the
    # loop); left open (aliased ``TaskSpecLike``) so a caller can pass any object
    # exposing the fields a detector reads (``query``/``budget``/…).
    spec: TaskSpecLike
    # The current observation under a checkpoint — a policy-shaped value (usually
    # the tool's ``dict`` observation). Open (aliased ``ObservationLike``) because
    # the observation currency is policy-defined; populated by the loop per
    # checkpoint and reset to ``None`` outside one.
    observation: ObservationLike = None
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
    # Durable per-grant state handle (ZU-CD-4): the run's ``GrantStore`` (C1 —
    # Protocol-typed, so a gate/validator reading ``ctx.grants`` gets the real
    # get/put/incr_if_below surface). ``None`` when the loop is between checkpoints
    # that need it; the loop always seats a store (the in-memory default) per run.
    grants: GrantStore | None = None
    # The ``ToolCall`` currently under pre-execution check (ZU-CORE-2); ``None``
    # outside an InvocationGate ``check`` (C1 — typed to the concrete contract).
    invocation: ToolCall | None = None
    # The idempotency key minted for the invocation in flight (ZU-CORE-4); a tool
    # reads it to dedupe a retried side effect. ``None`` outside a tool call.
    idempotency_key: str | None = None
    # The blessed step annotations for the invocation in flight (ZU-RAIL-4):
    # ``{"consequence", "destination"}`` carried from the replayed rail step, so a
    # gate can gate divergence/instruments by the rail's content-free consequence
    # class without reading hostile content. ``None`` outside a replayed call.
    annotations: dict | None = None
    # Consume-once execution ledger (ZU-CD-6): the run's ``ExecutionLedger`` a
    # tool/gate can ``claim(key)`` against to make a side effect idempotent across
    # instances (C1 — Protocol-typed, mirroring ``grants``). The loop itself claims
    # before re-executing a human-approved invocation on resume, so a double-resume
    # can't double-execute.
    execution: ExecutionLedger | None = None


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

    # Whether this tool's output is UNTRUSTED external content (#77). A tool with
    # open egress (``EGRESS_OPEN`` in ``egress``) reaches the internet and is
    # treated as untrusted automatically; this OPTIONAL, default-False flag lets a
    # no-egress tool that nonetheless ingests untrusted bytes (e.g. it reads a
    # user-supplied file or a message from an untrusted channel) opt in. When set,
    # the loop fences the tool's content with boundary markers + a "this is DATA,
    # not instructions" notice in the MODEL-FACING copy only (the logged copy is
    # untouched). Read defensively (``getattr(tool, "untrusted", False)``) so a
    # tool that omits it is treated as trusted — the behaviour-preserving default.
    untrusted: bool

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


# ``RunContext`` (defined far above) types ``grants``/``execution`` to the
# ``GrantStore``/``ExecutionLedger`` Protocols declared HERE — forward references
# under ``from __future__ import annotations`` (C1). Rebuild it now that both
# names exist in the module namespace, so pydantic resolves the annotations
# eagerly rather than deferring to first use.
RunContext.model_rebuild()


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


# --- the connected-surface family (web action surfaces over an external CDP
#     target) — #93/#94/#95 --------------------------------------------------
#
# The Action Surface (``zu_tools.action_surface``) already reduces a page to
# content-free, handle-addressed affordances from the CDP accessibility tree —
# and that tree flattens OPEN shadow roots and child frames for free (a plain
# ``document.querySelectorAll`` does NOT cross shadow boundaries, so controls
# inside web components / CMP widgets are invisible to a hand-rolled walk). But
# that reducer + its act-by-handle were welded to Zu's own ``SessionBackend``.
# These three ports expose the same reduction + a deterministic act over a
# browser target a HOST already owns (reached over an external CDP endpoint), so
# a downstream host reuses Zu's shadow/frame-piercing reduction + blind detector
# instead of re-implementing an enumerator that goes blind at every boundary.


class SurfaceAction(BaseModel):
    """One act on a :class:`ConnectedSurface`, addressed by an opaque handle
    (``a1``, ``a2`` … — the same handle the reducer assigned; the caller never
    sees or supplies a selector, §11.3).

    ``kind`` is a free string — ``"click"`` / ``"type"`` / ``"select"`` / ``"submit"``
    are the shipped verbs, but a producer may add one without a core edit. ``text``
    carries the payload for ``type`` (the string to enter) and ``select`` (the
    option label to choose; ``None`` means "the first VALID option", the
    deterministic default a :class:`SelectionSatisfier` drives). ``submit`` presses
    Enter on the field (a search-on-enter box with no visible submit button)."""

    model_config = {"frozen": True}

    handle: str
    kind: str  # "click" | "type" | "select" | "submit"
    text: str | None = None


@runtime_checkable
class ConnectedSurface(Protocol):
    """An Action Surface bound to an ALREADY-RUNNING browser target — a CDP
    endpoint the HOST owns (e.g. a sandboxed Chromium reached via
    ``connect_over_cdp``), not a session Zu launched.

    ``perceive()`` returns a :class:`~zu_core.surface.SurfaceView` flattened
    across OPEN shadow roots AND child frames (the accessibility tree already
    does this, more completely than a ``querySelectorAll`` walk — which does not
    cross shadow boundaries). ``act()`` resolves an opaque handle to its element
    ACROSS those boundaries (a backend DOM-node id is global to the target, which
    is exactly what "resolve across a shadow/frame boundary" needs) and performs
    the verb, returning the RE-PERCEIVED view so a caller can confirm the effect.
    Lets a host reuse Zu's reduction + blind/unlabeled detectors instead of
    re-implementing a shadow-piercing walk. CLOSED shadow roots stay unreachable
    to any page script (a browser security boundary) — only a pixel/vision
    producer could see those, which is what the ``blind`` signal escalates to."""

    async def perceive(self) -> SurfaceView: ...

    async def act(self, action: SurfaceAction) -> SurfaceView: ...


class ConsentControl(BaseModel):
    """The one control a :class:`ConsentResolver` picks out of a consent banner.

    ``kind`` is ``"accept"`` (the control that CLEARS the banner) or
    ``"open_panel"`` (a two-step CMP's "Manage consent", which reveals the accept
    in a sub-panel). ``label`` is the accessible name it was chosen by — audit
    only, never used to decide the task."""

    model_config = {"frozen": True}

    handle: str
    kind: str  # "accept" | "open_panel"
    label: str = ""


@runtime_checkable
class ConsentResolver(Protocol):
    """Find + clear a cookie/consent banner DETERMINISTICALLY.

    ``find()`` returns the ACCEPT control chosen by WHOLE-WORD accessible name
    (never 'Manage preferences' / 'Decline' / 'Settings' — clicking those leaves
    the banner up), or an ``open_panel`` control for a two-step CMP, or ``None``.
    Whole-word matching is load-bearing: as a bare substring 'ok' matches inside
    'Bespoke', 'yes' inside 'eyes', 'allow' inside 'swallow' — so a substring
    matcher clicks product links. ``dismiss()`` performs the full clear
    (open_panel → accept) across OPEN shadow roots and child frames (CMPs render
    in cross-origin iframes) via the :class:`ConnectedSurface`, and returns
    whether the banner was actually cleared — so a host can latch 'handled'
    rather than re-detecting a persistent 'Manage consent' footer tab forever.
    (Depends on the ConnectedSurface port for ``dismiss``.)"""

    def find(self, view: SurfaceView) -> ConsentControl | None: ...

    async def dismiss(self, surface: ConnectedSurface) -> bool: ...


class RequiredSelection(BaseModel):
    """One required single-choice control a :class:`SelectionSatisfier` set, and
    the option label it chose — so the #39 'control is now selected' invariant
    can confirm each took (and the host knows add-to-basket should now enable)."""

    model_config = {"frozen": True}

    handle: str
    chosen_label: str


@runtime_checkable
class SelectionSatisfier(Protocol):
    """Deterministically satisfy UNSET required single-choice controls (a
    product-variant ``<select>``: colour / size / fitting whose default is an
    unselected placeholder).

    ``satisfy_required()`` chooses the first VALID option — non-placeholder
    (empty value / 'Choose an option'), non-disabled, and where known in-stock —
    by option STRUCTURE, never page prose (content-free), and returns what it set
    so a caller can confirm each took via the #39 invariant. Until every required
    option is set, add-to-basket is disabled and the click is a silent no-op —
    the single biggest hidden cause of 'couldn't add to basket'. Pairs with the
    #39 ``VariantPicker`` pattern + 'control became selected' invariant. Here
    'required' means funnel-REQUIRED (the shop won't let you buy without it), NOT
    the HTML ``required`` attribute — real variant selects gate via the shop's JS,
    not the attribute (#110)."""

    async def satisfy_required(self, surface: ConnectedSurface) -> list[RequiredSelection]: ...


class CheckoutState(BaseModel):
    """What a :class:`CheckoutProceeder` reads off one surface.

    ``in_cart`` — add-to-cart took (a cart-count / 'added to cart' / mini-cart
    drawer signal). ``at_checkout`` — the checkout/shipping page is reached, still
    SHORT of place-order/pay. ``proceed_handle`` — the control that advances one
    step toward checkout (a drawer/mini-cart 'Checkout', or a 'View cart'), if one
    is present AND we are not already at checkout; it is NEVER a committing
    (place-order/pay) control."""

    model_config = {"frozen": True}

    in_cart: bool
    at_checkout: bool
    proceed_handle: str | None = None


@runtime_checkable
class CheckoutProceeder(Protocol):
    """After add-to-cart, ADVANCE the funnel to the checkout page deterministically
    — the post-add step the model stalls on (add succeeds, a mini-cart drawer pops,
    its 'Checkout' is never clicked). The natural third sibling of
    :class:`ConsentResolver` / :class:`SelectionSatisfier`.

    ``inspect()`` reports the :class:`CheckoutState`. ``proceed()`` clicks the
    post-add drawer / mini-cart 'Checkout' (or 'View cart' → 'Checkout'), chosen by
    WHOLE-WORD accessible name, advancing ONE step and returning whether it moved
    toward checkout. It STOPS at the checkout page: the place-order/pay step is
    COMMITTING (already classified by ``zu_patterns.cart_checkout``) and is NEVER
    crossed — the host's approval/vault owns that boundary; ``proceed`` will not
    click any control a commit-label check would flag. Content-free; a bounded,
    structural funnel step, not goal orchestration (broader cross-surface
    orchestration stays with the host's model-driven drive). Builds on
    ``cart_checkout`` (recognition + commit boundary) and drives the
    :class:`ConnectedSurface` (#93). (#117)"""

    def inspect(self, view: SurfaceView) -> CheckoutState: ...

    async def proceed(self, surface: ConnectedSurface) -> bool: ...


class CartAddition(BaseModel):
    """What a :class:`CartAdder` reads off one surface. ``added`` — a 'took' signal
    is present (a mini-cart drawer / cart-count / 'added to cart'). ``handle`` — the
    LIVE add-to-cart control, if one is present (``None`` when absent or disabled,
    e.g. a required option is unmet)."""

    model_config = {"frozen": True}

    added: bool
    handle: str | None = None


@runtime_checkable
class CartAdder(Protocol):
    """Deterministically add the current product to the cart and CONFIRM it took —
    the ``product → cart`` transition, symmetric with :class:`CheckoutProceeder`
    (#117) and the step before it.

    ``inspect`` finds a LIVE (non-disabled) add-to-cart control by WHOLE-WORD
    accessible name — NEVER a committing control (place-order/pay/buy-now excluded,
    as in #117). ``add`` clicks it and verifies via a genuine before/after DELTA
    (the #39 'a control became acted' invariant): a mini-cart drawer / a NEW checkout
    control / an 'added to cart' confirmation APPEARED. A persistent header 'View
    basket' link present in both is NOT a 'took' signal. A silent no-op (a required
    option unmet) returns False, so the host can satisfy the option and retry rather
    than falsely claim success. Short of the commit boundary — the host's
    vault/approval still owns pay. (#122)"""

    def inspect(self, view: SurfaceView) -> CartAddition: ...

    async def add(self, surface: ConnectedSurface) -> bool: ...


class FunnelPhase(str, Enum):
    """Where a page sits in a COMMIT FUNNEL — the content-free STATE the connected-surface
    resolvers transition OVER (#121). UNIVERSAL rungs, so ANY vertical maps onto them: a
    per-vertical :class:`FunnelPhaseClassifier` reads its OWN structural signals and returns
    a shared rung — shopping (add-to-cart → cart → checkout → card fields) and booking (a
    service/slot choice → details → a confirm/book control) both progress ENTRY→…→AT_COMMIT.
    Derived purely from page SHAPE, never prose. Powers observability (a phase timeline; drift
    is a regression) and resilience (revert on regression; don't escalate while it can advance).

    (Renamed from the shopping-specific ``BROWSING/ON_PRODUCT/IN_CART/AT_PAYMENT`` — #121 shipped
    the vocabulary shopping-named; generalised here for the second vertical, booking.)"""

    ENTRY = "entry"              # browsing / discovery / search — nothing selected yet
    SELECTING = "selecting"      # choosing an item — a product (add-to-cart), a service/business/slot
    ASSEMBLING = "assembling"    # the choice is locked, the order is being built (cart / selected slot)
    AT_CHECKOUT = "at_checkout"  # the checkout / details step — SHORT of the commit
    AT_COMMIT = "at_commit"      # the commit boundary — card/pay fields, or a confirm/book control
    UNKNOWN = "unknown"          # nothing recognisable (an empty / off-funnel surface)

    @property
    def rank(self) -> int:
        """Position in the funnel — higher is closer to the commit; ``UNKNOWN`` is ``-1``. Lets a
        consumer tell an ADVANCE from a REGRESS without hardcoding the phase order."""
        order = ("entry", "selecting", "assembling", "at_checkout", "at_commit")
        return order.index(self.value) if self.value in order else -1


@runtime_checkable
class FunnelPhaseClassifier(Protocol):
    """Classify which :class:`FunnelPhase` a surface sits in — structural and
    content-free. The web reference impl reads a ``SurfaceView``; a non-web producer
    may supply its own, but the :class:`FunnelPhase` enum is the shared vocabulary
    regardless (#121)."""

    def classify(self, view: SurfaceView) -> FunnelPhase: ...


# --- the interaction-primitive family — ONE closed vocabulary of generic,
#     self-locating, verified moves that compose to cover ANY funnel (#125) -------
#
# The connected-surface resolvers above (consent #94, selection #95, checkout #117,
# cart #122) are each a VERTICAL-NAMED capability. Generalised, they are all
# instances of ONE shape: a PRIMITIVE that (1) self-LOCATES its affordance(s) on a
# content-free :class:`~zu_core.surface.SurfaceView`, (2) APPLIES the move over a
# :class:`ConnectedSurface`, and (3) checks a SUCCESS INVARIANT (an option became
# selected, the funnel advanced, the banner cleared). A funnel then stops being
# bespoke code and becomes a TRAJECTORY through a fixed vocabulary:
#
#   dismiss     — clear an interstitial (consent / modal / popup)      [wraps #94]
#   search      — type a query into the recognised search box + submit
#   choose_one  — pick ONE from a group of equivalent options (+ a content-free
#                 hint); UNIFIES select-variant (#95), pick-slot, pick-service,
#                 pick-search-result — one 'choose from a group' call for all
#   advance     — click the primary move-forward control; UNIFIES add-to-cart (#122)
#                 + proceed-to-checkout (#117) + 'view times' / continue / next
#   commit_stop — the IRREVERSIBLE terminal (pay / place-order / confirm-booking) —
#                 recognised and STOPPED for human approval; NEVER crossed
#
# The hint is a content-free NUDGE — a position ('earliest' / 'last') or a token to
# match against option NAMES — so it is injection-immune: option names are DATA the
# primitive matches, never instructions it obeys. The model's per-step job shrinks
# to {primitive, hint}; every mechanic (locate, execute, verify, report) is
# deterministic here — 'the model isn't strong enough' is never the fix.

#: The closed primitive vocabulary. A consumer switches on ``kind``; adding a verb
#: is a deliberate edit here, not an open-ended string space.
PRIMITIVE_KINDS: tuple[str, ...] = ("dismiss", "search", "choose_one", "advance", "commit_stop")

#: The generic PROGRESS verdict a primitive reports — the content-free signal a
#: composition runtime routes on. ``advance`` moved the funnel forward; ``regress``
#: moved it backward (a wander); ``no_op`` changed nothing (the affordance was
#: absent or the click was silent); ``blocked`` reached a self-resolvable wall (a
#: required field, a login); ``commit_stop`` reached the irreversible boundary and
#: STOPPED for approval.
PrimitiveProgress = Literal["advance", "regress", "no_op", "blocked", "commit_stop"]


class PrimitivePlan(BaseModel):
    """What a primitive's content-free ``inspect`` reports about the CURRENT surface:
    whether it is APPLICABLE here, the affordance handle(s) it would act on, and the
    (bound) content-free ``hint``. Pure structure — derived from role/name/group/state
    only, never page prose, and no I/O. A plan is a PROPOSAL confirmed by ``apply``'s
    invariant, never ground truth."""

    model_config = {"frozen": True}

    kind: str
    applicable: bool
    handles: tuple[str, ...] = ()
    hint: str | None = None
    detail: str = ""


class PrimitiveOutcome(BaseModel):
    """What a primitive's ``apply`` DID: the generic :data:`PrimitiveProgress` verdict
    (the content-free routing signal) and the handle(s) it actually acted on. A host
    routes on ``progress`` — advance forward, treat regress/no_op as a wall to recover
    from, and hand a ``commit_stop`` to human approval — without reading page meaning."""

    model_config = {"frozen": True}

    kind: str
    progress: PrimitiveProgress
    handles: tuple[str, ...] = ()
    detail: str = ""


@runtime_checkable
class InteractionPrimitive(Protocol):
    """One generic, self-locating, VERIFIED interaction — the uniform contract the
    consent (#94) / selection (#95) / cart (#122) / checkout (#117) resolvers all
    reduce to (#125).

    ``inspect`` is CHEAP + content-free over a core :class:`~zu_core.surface.SurfaceView`:
    it self-locates the primitive's affordance(s) and reports a :class:`PrimitivePlan`
    (applicable? which handles?), taking an optional content-free ``hint``. ``apply``
    drives the :class:`ConnectedSurface` to EXECUTE the move and VERIFY its success
    invariant, returning a :class:`PrimitiveOutcome`. A primitive NEVER crosses the
    commit boundary: ``commit_stop`` recognises it (and reports ``commit_stop``);
    ``advance`` excludes committing controls by construction. ``kind`` is one of
    :data:`PRIMITIVE_KINDS`."""

    name: str
    kind: str

    def inspect(self, view: SurfaceView, *, hint: str | None = None) -> PrimitivePlan: ...

    async def apply(
        self, surface: ConnectedSurface, *, hint: str | None = None
    ) -> PrimitiveOutcome: ...


@runtime_checkable
class PrimitiveRuntime(Protocol):
    """The thin COMPOSITION layer over the primitive family: given a SurfaceView +
    the model's ``{kind, hint}`` step, dispatch to the right primitive, apply, verify,
    and report — so a host drives ONE uniform loop instead of N hardcoded capability
    blocks (#125).

    ``free`` reports the applicable SELF-GATING primitives (the ones that fire WITHOUT
    the model — dismiss a banner, satisfy required variants via ``choose_one`` with no
    hint, ``advance`` the funnel) as content-free :class:`PrimitivePlan`\\ s, in the
    order a host should try them. ``step`` runs ONE named primitive (a model-directed
    ``{kind, hint}`` or a free plan's kind) over the surface and returns its outcome.
    ``get`` resolves a kind to its primitive (``None`` for an unknown kind)."""

    def free(self, view: SurfaceView) -> tuple[PrimitivePlan, ...]: ...

    def get(self, kind: str) -> InteractionPrimitive | None: ...

    async def step(
        self, surface: ConnectedSurface, kind: str, *, hint: str | None = None
    ) -> PrimitiveOutcome: ...
