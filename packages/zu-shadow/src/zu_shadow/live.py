"""The LIVE recorder binding — real Chromium + a real human, over CDP.

This is the demo/manual half of Shadow: it drives a real browser and watches a real
human do the task, translating CDP events into the SAME abstract ``RawInput`` items
the offline recorder consumes. Because the live binding produces the identical
stream the synthetic tests do, the offline core (recorder → redaction → synthesizer
→ replay gate) is exercised exactly as it is live — nothing about the live path is
special-cased downstream.

It is NOT unit-tested offline (it needs a real Chromium + a human), so it sits
behind the ``live`` extra and this manual entrypoint, guarded so importing it
without the browser tools fails with an actionable message rather than at runtime.
The accessibility tree (CDP ``Accessibility.getFullAXTree`` / the §4 locate op) is
what makes capture SEMANTIC: each interacted node is resolved to its
``{role, name, label}`` — never a selector or coordinate — before it becomes a
``RawInput``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from zu_core.bus import EventBus

from .capture import SemanticTarget
from .recorder import RawInput, RecordedSession, Recorder
from .redaction import RedactionPolicy


def _require_browser() -> None:
    try:
        import zu_tools.browser  # noqa: F401
    except ModuleNotFoundError as exc:  # pragma: no cover - live-only path
        raise RuntimeError(
            "the live Shadow recorder needs the browser tools: pip install 'zu-shadow[live]'. "
            "The offline core (synthetic stream → recorder → synthesizer → gate) needs none."
        ) from exc


def ax_node_to_target(node: dict) -> SemanticTarget:
    """Resolve one CDP accessibility node to a SEMANTIC target — its role, accessible
    name, and label, plus the locale-independent STRUCTURAL signals (the raw input
    ``type``, the ``autocomplete`` token, and whether it ``submits``) that the
    credential/commit guards drive off. The single place selectors/coordinates are
    deliberately DROPPED (a live click's pixel position never becomes part of the record)."""
    name = str(node.get("name", "") or "")
    it = node.get("input_type")
    ac = node.get("autocomplete")
    return SemanticTarget(
        role=str(node.get("role", "") or "generic"),
        name=name,
        label=str(node.get("label", "") or name),
        input_type=str(it) if it else None,
        autocomplete=str(ac) if ac else None,
        submits=bool(node.get("submits", False)),
    )


async def record_live(  # pragma: no cover - live-only, manual entrypoint
    cdp_events: AsyncIterator[dict],
    *,
    site: str,
    bus: EventBus | None = None,
    policy: RedactionPolicy | None = None,
) -> RecordedSession:
    """Drive a live recording from a stream of CDP events. Translates each CDP event
    into a ``RawInput`` (resolving interacted nodes to semantic targets via the AX
    tree) and folds it through the SAME :class:`Recorder` the offline path uses, so
    redaction-before-append holds identically on a live session.

    Manual entrypoint: requires the browser tools and a real CDP source. Wire a real
    Chromium target's CDP feed as ``cdp_events`` (e.g. via ``zu_tools.browser``).
    """
    _require_browser()
    bus = bus or EventBus()
    recorder = Recorder(bus, site=site, policy=policy)
    await recorder.start()
    async for cdp in cdp_events:
        item = _cdp_to_raw(cdp)
        if item is not None:
            await recorder.record(item)
    await recorder.end()
    events = await bus.query()
    return RecordedSession(site=site, events=list(events))


def _cdp_to_raw(cdp: dict) -> RawInput | None:
    """Map one CDP event to a ``RawInput`` (or None to skip). Mirrors the abstract
    kinds the offline stream uses; the live CDP method names are translated here so
    the rest of Shadow never sees CDP."""
    method = cdp.get("method", "")
    params = cdp.get("params", {}) or {}
    if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed":
        node = params.get("ax_node", {})
        return RawInput(kind="click", target=ax_node_to_target(node),
                        intent=params.get("intent"))
    if method == "Input.insertText":
        node = params.get("ax_node", {})
        return RawInput(kind="type", target=ax_node_to_target(node),
                        value=params.get("text", ""), intent=params.get("intent"))
    if method == "Page.navigate":
        return RawInput(kind="navigate", url=params.get("url", ""),
                        intent=params.get("intent"))
    if method == "Page.frameStoppedLoading" or method == "Page.loadEventFired":
        return RawInput(kind="page", url=params.get("url", ""), title=params.get("title", ""))
    if method == "Network.responseReceived":
        resp = params.get("response", {}) or {}
        url = resp.get("url", "")
        from urllib.parse import urlsplit

        return RawInput(kind="network", url=url, status=int(resp.get("status", 0)),
                        host=urlsplit(url).hostname or "")
    return None
