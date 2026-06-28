"""Zu built-in tools.

The built-ins are written against the exact same Tool port users get — which
is what proves the plugin system is real, not a second-class add-on.

Also exposes :func:`extract` (#82) — the safe read->decide bridge that turns a
fenced ``ContentView`` into typed facts against a caller-supplied schema.
"""

from __future__ import annotations

from .content_extract import ExtractResult, extract

__all__ = ["extract", "ExtractResult"]
