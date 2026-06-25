"""The live headed capture's pure translation, tested offline.

The headed drive (launch Chrome, the human clicks) is manual, but the payload->RawInput
mapping it relies on is pure and is the load-bearing contract: a captured page action
becomes the SAME semantic RawInput the synthetic/offline path uses — {role,name,label},
never a selector or coordinate.
"""

from __future__ import annotations

from zu_shadow.live_capture import _payload_to_raw


def test_click_payload_is_semantic() -> None:
    ri = _payload_to_raw({"kind": "click", "role": "button", "name": "Confirm booking"})
    assert ri is not None and ri.kind == "click"
    assert ri.target is not None
    assert (ri.target.role, ri.target.name, ri.target.label) == ("button", "Confirm booking", "Confirm booking")
    # no selector/coordinate fields exist on the captured target
    assert set(ri.target.model_dump()) == {"role", "name", "label"}


def test_type_payload_carries_value_and_role_fallback() -> None:
    ri = _payload_to_raw({"kind": "type", "name": "Your email", "value": "a@b.com"})
    assert ri is not None and ri.kind == "type" and ri.value == "a@b.com"
    assert ri.target is not None and ri.target.role == "textbox"  # default when role absent


def test_unknown_payload_is_skipped() -> None:
    assert _payload_to_raw({"kind": "scroll"}) is None
    assert _payload_to_raw({}) is None
