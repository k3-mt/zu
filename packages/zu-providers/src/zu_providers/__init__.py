"""Zu model-provider adapters.

Every adapter implements the ModelProvider port: it turns a normalized
ModelRequest into a ModelResponse and declares its Capabilities. The harness
never imports a model SDK — it speaks only the port.
"""

from __future__ import annotations

from .reputation import (
    DeterministicReputationScorer,
    SignalSource,
    StaticSignalSource,
    score_signals,
)
from .retrieval import (
    ScriptedRetrievalProvider,
    WebSearchRetrievalProvider,
    domain_of,
)
from .scripted import ScriptedProvider

__all__ = [
    "ScriptedProvider",
    # RetrievalProvider — typed discovery (#81)
    "ScriptedRetrievalProvider",
    "WebSearchRetrievalProvider",
    "domain_of",
    # ReputationProvider — deterministic merchant trust (#84)
    "DeterministicReputationScorer",
    "SignalSource",
    "StaticSignalSource",
    "score_signals",
]
