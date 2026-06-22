"""Harness-driven pathfinding — turn a coding agent's exploration into a track.

A developer's OWN harness model (Claude Code, Codex, Cursor — any MCP client) drives zu's
off-box tools step by step over ``zu mcp``: fetch a page, open the browser, act, read, react
to what it sees, and find the path. Each step + its observation is recorded; when the path
reaches the data, the session is projected into a ``fixtures/`` bundle — so the discovery the
developer would do anyway BECOMES the agent's replayable path. The frontier reasoning is
spent once, in the harness they already pay for; everything downstream replays it free
(``zu run --offline``), with the model returning only on divergence at run time.

This module is the SESSION core, with the tools injected — the real off-box tools in
production (a live browser via the docker backend), fakes in tests — so the orchestration is
verified at $0. The ``zu mcp`` server wraps one session per process as ``zu_explore`` /
``zu_explore_save``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .offline import Bundle

# The off-box tools a harness drives while pathfinding. Pure tools (html_parse, recall) need
# no exploration — they run unchanged offline — so they are not part of an explore session.
EXPLORABLE = ("http_fetch", "render_dom", "browser")


@dataclass
class ExplorationSession:
    """One live pathfinding session: the tools being driven (the persistent ``browser`` keeps
    its session across steps), a read-only ``ctx``, and the ordered (tool, args, observation)
    trail the harness builds. Project it into a :class:`~zu_cli.offline.Bundle` when the path
    reaches the data."""

    tools: dict[str, Any]
    ctx: Any
    steps: list[dict] = field(default_factory=list)

    async def step(self, tool: str, args: dict) -> dict:
        """Drive one tool call, record the (tool, args, observation), and return the
        observation so the harness model can decide the next step. An unknown/​non-explorable
        tool is a loud error, not a silent no-op."""
        if tool not in self.tools:
            raise KeyError(
                f"{tool!r} is not an explorable tool; choose one of {sorted(self.tools)}")
        obs = await self.tools[tool](self.ctx, **args)
        self.steps.append({"tool": tool, "args": dict(args), "observation": obs})
        return obs

    def to_bundle(self, *, task: str, answer: Any, model: str | None = None) -> Bundle:
        """Project the recorded trail into a replayable Bundle — the SAME shape
        :func:`zu_cli.offline.project_capture` produces: one move per step in order, then a
        final text move carrying ``answer`` (so offline replay reproduces both the navigation
        and the extraction); observations grouped by tool. A browser ``op=close`` has no
        replayable observation (the tool returns without a session read), so it is dropped to
        keep each tool's observation sequence aligned with its calls."""
        moves: list[dict] = [{"tool": s["tool"], "args": s["args"]} for s in self.steps]
        moves.append({"text": json.dumps(answer), "finish": "stop"})
        observations: dict[str, list[dict]] = {}
        for s in self.steps:
            if s["tool"] == "browser" and s["args"].get("op") == "close":
                continue
            if isinstance(s["observation"], dict):
                observations.setdefault(s["tool"], []).append(dict(s["observation"]))
        return Bundle(task=task, moves=moves, observations=observations, model=model)


def default_tools(*, allow_private: bool = False) -> dict[str, Any]:
    """The real off-box tools a LIVE exploration drives: a one-shot ``http_fetch`` /
    ``render_dom`` and a persistent ``browser`` (a real headless browser via the docker
    backend). ``allow_private`` stays False so the SSRF guard holds against a real site."""
    from zu_tools.browser import Browser
    from zu_tools.fetch import HttpFetch
    from zu_tools.render import RenderDom

    return {
        "http_fetch": HttpFetch(allow_private=allow_private),
        "render_dom": RenderDom(allow_private=allow_private),
        "browser": Browser(allow_private=allow_private),
    }


def new_session(*, tools: dict[str, Any] | None = None, allow_private: bool = False
                ) -> ExplorationSession:
    """A fresh exploration session — real off-box tools unless ``tools`` is injected (tests)."""
    from zu_core.ports import RunContext

    return ExplorationSession(
        tools=tools if tools is not None else default_tools(allow_private=allow_private),
        ctx=RunContext(spec=None),
    )
