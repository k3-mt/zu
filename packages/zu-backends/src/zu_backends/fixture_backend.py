"""A file-driven, URL-keyed ``SandboxBackend`` for offline tier-2 replay.

The tier-2 ``render_dom`` tool renders a URL in a real browser inside a sandbox
obtained through the :class:`~zu_core.ports.SandboxBackend` port. For offline
construction loops we replace that live browser with recorded observations:
``FixtureBackend`` holds a map of URL → the rendered page that was captured for
it, and replays the matching one on each ``exec`` — no Docker, no network, $0.

It generalizes the two one-off stand-ins already in the tree — ``demo._FixtureBrowser``
(a single fixed body) and ``scripted_sandbox.ScriptedSandbox`` (an empty body) —
into the keyed, multi-URL form an agent's ``fixtures/`` bundle needs. Construct it
with a ``responses`` map (the offline wiring builds this from the bundle); it is
not discovered no-arg, so it is injected by the offline harness rather than named
on a bare ``backend:`` config line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zu_core.ports import ToolCall


@dataclass
class FixtureBackend:
    """A ``SandboxBackend`` (see :mod:`zu_core.ports`) that replays recorded render
    observations keyed by URL. ``responses`` maps a URL to ``{status, html}`` — the
    page captured for that URL. ``launch``/``destroy`` are no-ops (there is no real
    sandbox); ``exec`` returns the recorded observation for ``call.args["url"]``, or
    a 404-shaped miss so an unrecorded URL fails loudly rather than silently passing."""

    name = "fixture-backend"
    responses: dict[str, dict] = field(default_factory=dict)

    async def launch(self, spec: dict) -> dict:
        return {"id": "fixture", "spec": spec}

    async def exec(self, sandbox: Any, call: ToolCall) -> dict:
        url = call.args.get("url")
        recorded = self.responses.get(url) if isinstance(url, str) else None
        if recorded is None:
            # A miss is not a silent pass: an unrecorded URL returns an empty 404
            # so the loop's detectors/validators see a failed render, exactly as a
            # live browser hitting a dead page would.
            return {"status": 404, "html": "", "url": url}
        return {"status": recorded.get("status", 200), "html": recorded.get("html", ""), "url": url}

    async def destroy(self, sandbox: Any) -> None:
        return None
