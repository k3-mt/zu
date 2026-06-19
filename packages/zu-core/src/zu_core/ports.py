"""The six extension points, as structural Protocols.

A plugin author implements the *shape* of a port without importing or
subclassing a Zu base class. The core depends only on these shapes, never on
a concrete adapter — which is what makes every adapter replaceable.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .contracts import Result

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
    events: list = Field(default_factory=list)


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
    async def launch(self, spec: dict) -> Any: ...

    async def exec(self, sandbox: Any, call: ToolCall) -> dict: ...

    async def destroy(self, sandbox: Any) -> None: ...


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
