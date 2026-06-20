"""Zu core — the small, stable runtime: contracts, ports, registry, loop, bus.

Depends only on the standard library and Pydantic. It contains no model SDK,
no domain branching, and no knowledge of any specific tool or provider.
"""

from __future__ import annotations

from . import events
from .bus import EventBus, SubscriberFailure
from .codec import IdentityCodec, KeyProvider, PayloadCodec, decode_payload, encode_payload
from .contracts import Budget, Event, Result, Status, TaskSpec
from .eventstore import ALLOWED_EVENT_FILTERS, event_matches, validate_filter
from .ports import (
    CAP_FS_READ,
    CAP_FS_WRITE,
    CAP_NET,
    CAP_SANDBOX,
    CAP_SUBPROCESS,
    EGRESS_OPEN,
    INTERFACE_ATTR,
    INTERFACE_VERSION,
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
    declared_envelope,
)
from .projections import SessionState, SessionStore
from .registry import (
    REGISTRY,
    IncompatibleInterfaceError,
    LoadFailure,
    Registry,
    backend,
    check_interface,
    detector,
    provider,
    sink,
    tool,
    validator,
)
from .security import SecurityBlock
from .sinks import MemoryEventSink
from .view import RENDER_KEYS, scope_event, scope_payload

__all__ = [
    # contracts
    "Budget",
    "Event",
    "Result",
    "Status",
    "TaskSpec",
    # event bus + taxonomy + projections + sinks + codec
    "EventBus",
    "SubscriberFailure",
    "SessionStore",
    "SessionState",
    "MemoryEventSink",
    "events",
    "ALLOWED_EVENT_FILTERS",
    "event_matches",
    "validate_filter",
    "IdentityCodec",
    "PayloadCodec",
    "KeyProvider",
    "encode_payload",
    "decode_payload",
    "SecurityBlock",
    "scope_event",
    "scope_payload",
    "RENDER_KEYS",
    # ports
    "CAP_NET",
    "CAP_SANDBOX",
    "CAP_FS_READ",
    "CAP_FS_WRITE",
    "CAP_SUBPROCESS",
    "EGRESS_OPEN",
    "INTERFACE_VERSION",
    "INTERFACE_ATTR",
    "declared_envelope",
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
    "LoadFailure",
    "IncompatibleInterfaceError",
    "check_interface",
    "Registry",
    "backend",
    "detector",
    "provider",
    "sink",
    "tool",
    "validator",
]
