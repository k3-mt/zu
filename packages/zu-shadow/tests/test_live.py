"""The live recorder's CDP→RawInput translation — the pure half, tested offline.

``record_live`` itself needs a real Chromium + a human (manual), but the translation
it relies on (``ax_node_to_target`` / ``_cdp_to_raw``) is pure dict→value logic and is
the load-bearing contract: the live binding must produce the SAME abstract ``RawInput``
stream the offline recorder consumes, captured SEMANTICALLY ({role,name,label}) with
selectors/coordinates dropped. These tests pin that contract at $0.
"""

from __future__ import annotations

from zu_shadow.live import _cdp_to_raw, ax_node_to_target


def test_ax_node_to_target_is_semantic_and_drops_coordinates() -> None:
    node = {"role": "button", "name": "Place order", "label": "Submit",
            "input_type": "text", "autocomplete": "cc-number", "submits": True,  # structural signals
            "x": 412, "y": 880, "selector": "#order-btn"}  # the brittle bits...
    t = ax_node_to_target(node)
    assert (t.role, t.name, t.label) == ("button", "Place order", "Submit")
    # The locale-independent structural signals carry through...
    assert (t.input_type, t.autocomplete, t.submits) == ("text", "cc-number", True)
    # ...but selectors/coordinates are dropped: a SemanticTarget carries neither.
    blob = t.model_dump()
    assert set(blob) == {"role", "name", "label", "input_type", "autocomplete", "submits"}
    assert "x" not in blob and "selector" not in blob


def test_ax_node_to_target_falls_back_when_fields_missing() -> None:
    # No role → "generic"; no explicit label → the accessible name carries through.
    t = ax_node_to_target({"name": "Email"})
    assert t.role == "generic" and t.name == "Email" and t.label == "Email"
    empty = ax_node_to_target({})
    assert empty.role == "generic" and empty.name == "" and empty.label == ""


def test_cdp_to_raw_translates_each_kind_semantically() -> None:
    click = _cdp_to_raw({"method": "Input.dispatchMouseEvent",
                         "params": {"type": "mousePressed",
                                    "ax_node": {"role": "button", "name": "Book"},
                                    "intent": "pick the clinic"}})
    assert click is not None and click.kind == "click"
    assert click.target is not None and click.target.name == "Book"
    assert click.intent == "pick the clinic"

    typed = _cdp_to_raw({"method": "Input.insertText",
                        "params": {"ax_node": {"role": "textbox", "name": "Name"},
                                   "text": "Alex"}})
    assert typed is not None and typed.kind == "type" and typed.value == "Alex"
    assert typed.target is not None and typed.target.role == "textbox"

    nav = _cdp_to_raw({"method": "Page.navigate", "params": {"url": "https://x.example/book"}})
    assert nav is not None and nav.kind == "navigate" and nav.url == "https://x.example/book"

    page = _cdp_to_raw({"method": "Page.loadEventFired",
                       "params": {"url": "https://x.example/book", "title": "Book"}})
    assert page is not None and page.kind == "page" and page.title == "Book"


def test_cdp_to_raw_network_extracts_the_host() -> None:
    net = _cdp_to_raw({"method": "Network.responseReceived",
                      "params": {"response": {"url": "https://api.x.example/slots", "status": 200}}})
    assert net is not None and net.kind == "network"
    assert net.host == "api.x.example" and net.status == 200


def test_cdp_to_raw_skips_unknown_and_non_pressed_events() -> None:
    # A mouse MOVE (not a press) is not an action; an unknown CDP method is skipped.
    assert _cdp_to_raw({"method": "Input.dispatchMouseEvent",
                        "params": {"type": "mouseMoved"}}) is None
    assert _cdp_to_raw({"method": "Runtime.consoleAPICalled", "params": {}}) is None
    assert _cdp_to_raw({}) is None
