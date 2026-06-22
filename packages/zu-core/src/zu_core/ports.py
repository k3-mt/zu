"""The six extension points, as structural Protocols.

A plugin author implements the *shape* of a port without importing or
subclassing a Zu base class. The core depends only on these shapes, never on
a concrete adapter — which is what makes every adapter replaceable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .content import Action, Observation
from .contracts import Result

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
    TERMINAL = "terminal"


class Verdict(BaseModel):
    severity: Severity
    detector: str
    detail: str | None = None


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


@runtime_checkable
class EventSink(Protocol):
    """The canonical event store — the single source of truth for a run.

    ``append`` is idempotent on ``event_id`` (re-appending the same event is a
    no-op), so a retried publish never duplicates a record. A filter value of
    ``None`` matches ``IS NULL`` (e.g. ``{"parent_id": None}`` selects roots).

    Reads come in two shapes so a large log never has to be materialised at
    once: ``query`` for a bounded window (always pass ``limit`` for big logs),
    and ``stream`` for memory-safe iteration over the whole result via keyset
    pagination.
    """

    async def append(self, event: Any) -> None: ...

    async def query(
        self, flt: dict | None = None, *, limit: int | None = None, after_seq: int = 0
    ) -> list: ...

    def stream(
        self, flt: dict | None = None, *, batch_size: int = 500
    ) -> AsyncIterator[Any]: ...

    async def count(self, flt: dict | None = None) -> int: ...
