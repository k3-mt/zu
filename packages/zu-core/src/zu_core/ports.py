"""The six extension points, as structural Protocols.

A plugin author implements the *shape* of a port without importing or
subclassing a Zu base class. The core depends only on these shapes, never on
a concrete adapter — which is what makes every adapter replaceable.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable

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

    async def complete(self, req: ModelRequest) -> ModelResponse: ...


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
    async def append(self, event: Any) -> None: ...

    async def query(self, flt: dict) -> list: ...
