"""Zu core — the small, stable runtime: contracts, ports, registry, loop, bus.

Depends only on the standard library and Pydantic. It contains no model SDK,
no domain branching, and no knowledge of any specific tool or provider.
"""

from __future__ import annotations

from .contracts import Budget, Event, Result, Status, TaskSpec
from .ports import (
    Capabilities,
    Detector,
    EventSink,
    Finish,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    RunContext,
    SandboxBackend,
    Scope,
    Severity,
    Tool,
    ToolCall,
    Validator,
    Verdict,
)
from .registry import (
    REGISTRY,
    Registry,
    backend,
    detector,
    provider,
    sink,
    tool,
    validator,
)

__all__ = [
    # contracts
    "Budget",
    "Event",
    "Result",
    "Status",
    "TaskSpec",
    # ports
    "Capabilities",
    "Detector",
    "EventSink",
    "Finish",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "RunContext",
    "SandboxBackend",
    "Scope",
    "Severity",
    "Tool",
    "ToolCall",
    "Validator",
    "Verdict",
    # registry
    "REGISTRY",
    "Registry",
    "backend",
    "detector",
    "provider",
    "sink",
    "tool",
    "validator",
]
