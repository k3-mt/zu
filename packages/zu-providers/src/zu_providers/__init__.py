"""Zu model-provider adapters.

Every adapter implements the ModelProvider port: it turns a normalized
ModelRequest into a ModelResponse and declares its Capabilities. The harness
never imports a model SDK — it speaks only the port.
"""

from __future__ import annotations

from .scripted import ScriptedProvider

__all__ = ["ScriptedProvider"]
