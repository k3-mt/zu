"""Allowlist-render view scoping: structural control-plane fields render; content
(query, fetched text, extracted values, URL args) is summarized — default-deny."""

from __future__ import annotations

from uuid import uuid4

from zu_core.contracts import Event
from zu_core.view import scope_event, scope_payload


def _ev(type_: str, payload: dict) -> Event:
    return Event(trace_id=uuid4(), task_id=uuid4(), type=type_, source="loop", payload=payload)


def test_structural_fields_render_content_is_summarized():
    out = scope_payload({"query": "a sensitive question", "tier": 2, "step": 3}, full=False)
    assert out["tier"] == 2 and out["step"] == 3                 # allowlisted → verbatim
    assert out["query"]["_type"] == "str" and "sha256" in out["query"]  # content → summarized
    assert "sensitive" not in str(out["query"])


def test_full_scope_renders_everything():
    assert scope_payload({"query": "q"}, full=True) == {"query": "q"}


def test_arg_values_summarized_but_scalars_and_keys_kept():
    out = scope_payload({"tool": "http_fetch", "args": {"url": "http://x/p?token=abc", "n": 3}}, full=False)
    assert out["tool"] == "http_fetch"
    assert out["args"]["n"] == 3                                  # scalar arg → verbatim
    assert out["args"]["url"]["_type"] == "str"                   # URL value → summarized
    assert "token=abc" not in str(out["args"]["url"])


def test_scope_event_keeps_shape_and_renders_control_plane():
    d = scope_event(_ev("harness.task.started", {"query": "hello", "target": "http://x/"}), full=False)
    assert d["type"] == "harness.task.started" and d["source"] == "loop"
    assert d["payload"]["query"]["_type"] == "str"               # query hidden
    assert d["payload"]["target"]["_type"] == "str"              # URL hidden (key collides w/ defense)

    # A defense event's kind/severity (control plane) still render in full.
    dd = scope_event(_ev("harness.defense.blocked", {"kind": "ssrf", "detail": "refused"}), full=False)
    assert dd["payload"]["kind"] == "ssrf" and dd["payload"]["detail"] == "refused"
