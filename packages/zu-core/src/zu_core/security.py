"""The thin security contract the core knows about.

The core stays capability-free, but it owns one small security primitive: a
plugin (a tool, a guard) raises :class:`SecurityBlock` when it *contains* an
action — an SSRF/egress refusal, a denied capability — and the interpreter loop
turns that into a ``harness.defense.blocked`` event on the log. That makes a
contained adversarial attempt **visible by construction**: it is on the
append-only record, never a silent return. A concrete guard (e.g. zu-tools'
SSRF check) subclasses this; the loop only depends on the base shape.
"""

from __future__ import annotations


class SecurityBlock(Exception):
    """A guard refused an action. ``kind`` categorises it (e.g. ``"ssrf"``),
    ``target`` is what was refused (a host/path), when known. Raising it (rather
    than returning a bare error) is what gets the block recorded as a defense."""

    kind = "security_block"

    def __init__(self, message: str, *, kind: str | None = None, target: str | None = None) -> None:
        super().__init__(message)
        if kind is not None:
            self.kind = kind
        self.target = target
