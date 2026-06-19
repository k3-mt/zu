"""The live-trace formatter — turns events into a real-time, readable view."""

from __future__ import annotations

from zu_cli.trace import format_event, live_printer
from zu_core.contracts import Event


def _ev(type_: str, payload: dict) -> Event:
    import uuid

    tid = uuid.uuid4()
    return Event(trace_id=tid, task_id=tid, type=type_, source="loop", payload=payload)


def test_formats_the_key_events():
    assert "task" in format_event(_ev("harness.task.started", {"query": "extract X"}))
    assert "http_fetch" in format_event(_ev("harness.tool.invoked", {"tool": "http_fetch", "args": {"url": "u"}}))
    assert "ESCALATE 1→2" in format_event(
        _ev("harness.task.escalated", {"from_tier": 1, "to_tier": 2, "reason": "js-shell"})
    )
    assert "extracted" in format_event(_ev("data.record.extracted", {"value": {"x": 1}}))
    assert "completed" in format_event(_ev("harness.task.completed", {"value": {}}))


def test_model_text_is_the_train_of_thought():
    line = format_event(_ev("harness.turn.completed", {"step": 1, "text": "I will fetch the page first."}))
    assert "fetch the page" in line
    # a pure tool-call turn (no prose) is omitted to keep the trace legible
    assert format_event(_ev("harness.turn.completed", {"step": 1, "text": None})) is None


def test_tool_invoked_drops_bulky_html_arg():
    line = format_event(_ev("harness.tool.invoked", {"tool": "html_parse", "args": {"html": "x" * 9999, "selector": "h1"}}))
    assert "x" * 100 not in line  # the bulky html value is dropped
    assert "selector" in line and "h1" in line


def test_live_printer_writes_as_events_arrive():
    out: list[str] = []
    printer = live_printer(write=out.append, clock=False)
    printer(_ev("harness.task.started", {"query": "q"}))
    printer(_ev("harness.turn.completed", {"step": 1, "text": None}))  # omitted
    printer(_ev("harness.task.completed", {"value": {}}))
    assert len(out) == 2  # the prose-less turn produced no line
    assert "task" in out[0] and "completed" in out[1]
