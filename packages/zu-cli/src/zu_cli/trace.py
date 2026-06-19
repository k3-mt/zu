"""Live trace — turn the event stream into a human-readable, real-time view.

The bus notifies every subscriber *as each event is appended* (append-before-
notify), so a subscriber that prints is a live window into the running loop: the
model's train of thought, every tool call and its result, detector verdicts,
escalations, and the final answer — streaming with no refresh, no polling, no
restart. The same formatter renders the CLI trace and the HTTP (SSE) stream, so
what you watch locally and what you watch against a container are identical.
"""

from __future__ import annotations

from typing import Any, Callable


def _truncate(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_event(event: Any, *, full: bool = True) -> str | None:
    """A one-line view of an event, or None to omit it from the trace. Pure and
    side-effect-free so it serves both the console and the SSE stream.

    ``full=True`` (the local console default) shows content — the query, the
    model's reasoning, tool args, extracted values. ``full=False`` is the
    allowlist-render scope for a networked window: the *actions and decisions* (so
    you see what the agent is doing and what its guards blocked) without dumping
    the content it read or produced."""
    t = getattr(event, "type", "")
    p = getattr(event, "payload", {}) or {}

    if t == "harness.task.started":
        if not full:
            return "▶ task started"
        target = f" → {p['target']}" if p.get("target") else ""
        return f"▶ task: {_truncate(p.get('query', ''))}{target}"
    if t == "harness.turn.started":
        return f"· turn {p.get('step')}"
    if t == "harness.turn.completed":
        # The model's natural-language output this turn — the train of thought.
        text = p.get("text")
        if not text:
            return None  # a pure tool-call turn with no prose; the tool lines speak
        if not full:
            return f"💭 reasoning ({len(text)} chars)"  # content-light: that it thought, not what
        return f"💭 {_truncate(text, 240)}"
    if t == "harness.tool.invoked":
        args = {k: v for k, v in (p.get("args") or {}).items() if k != "html"}
        if not full:
            keys = ", ".join(args)  # arg names only, never values
            return f"🔧 {p.get('tool')}({keys})"
        return f"🔧 {p.get('tool')}({_truncate(args, 120)})"
    if t == "harness.tool.returned":
        if not full:
            return f"↩ {p.get('tool')} returned"
        return f"↩ {p.get('tool')} → {_truncate(p.get('observation'), 140)}"
    if t == "data.source.fetched":
        # Already a summary (length + status), safe in either scope.
        body = p.get("html") or p.get("text") or p.get("content") or ""
        return f"📄 fetched {len(body)} chars (status {p.get('status', '?')})"
    if t == "harness.detector.fired":
        return f"🔎 detector {p.get('detector')} [{p.get('severity')}] — {_truncate(p.get('detail'), 120)}"
    if t == "harness.defense.blocked":
        target = f" → {p['target']}" if p.get("target") else ""
        return f"🛡 BLOCKED {p.get('kind')}{target} ({p.get('tool')}) — {_truncate(p.get('detail'), 120)}"
    if t == "harness.task.escalated":
        if p.get("exhausted"):
            return f"⛔ escalation exhausted at tier {p.get('tier')}: {p.get('reason')}"
        return f"⬆️  ESCALATE {p.get('from_tier')}→{p.get('to_tier')}: {p.get('reason')} — climbing a tier"
    if t == "harness.validation.failed":
        return f"❌ validation {p.get('detector')} [{p.get('severity')}] — {_truncate(p.get('detail'), 120)}"
    if t == "data.record.extracted":
        value = p.get("value")
        if not full:
            n = len(value) if isinstance(value, dict) else 1
            return f"📦 extracted ({n} field{'s' if n != 1 else ''})"  # shape, not content
        return f"📦 extracted: {_truncate(value, 200)}"
    if t == "harness.task.completed":
        return "✅ completed"
    if t == "harness.task.terminal":
        return f"🛑 terminal: {p.get('reason')}"
    return None


def live_printer(
    write: Callable[[str], None] | None = None, *, clock: bool = True
) -> Callable[[Any], None]:
    """A bus subscriber that prints each event the moment it is published. Pass a
    custom ``write`` to redirect; ``clock`` prefixes a wall-clock timestamp."""

    def _write(line: str) -> None:
        if write is not None:
            write(line)
        else:
            print(line, flush=True)  # flush so the trace is truly real-time

    def _on_event(event: Any) -> None:
        line = format_event(event)
        if line is None:
            return
        if clock:
            ts = getattr(event, "ts", None)
            stamp = ts.strftime("%H:%M:%S") if ts is not None else ""
            _write(f"  {stamp}  {line}")
        else:
            _write(f"  {line}")

    return _on_event
