"""Validation of model-supplied browser actions at the tool boundary (issue #65 F52).

``render_dom`` and ``browser`` forward a list of model-chosen actions
(selectors + fill values + waits) into the sandbox browser session. Cluster-6
(#74/#76) gates NAVIGATION (which url may be opened) and the write-shaped ``op``;
this is the complementary guard on the ACTION ARGS themselves — the shape of each
``{click|fill|select|wait_for: <selector>, value?, near?} | {wait_ms:<n>}`` item.
Without it an arbitrary dict (wrong types, a stray/unknown key, a missing
selector) is forwarded verbatim; a clear refusal at the boundary is safer and
gives the model an actionable error instead of an opaque sandbox failure.

The check is deliberately generic over the action set — it is driven by the
``_ACTION_OPS`` / ``_WAIT_MS`` declaration below, not by any site knowledge — and
is dependency-free (mirrors zu-core's C8 ``_validate_args_against_schema`` style:
allowed op, required fields present + typed, unexpected fields rejected).
"""

from __future__ import annotations

from typing import Any

# The selector-shaped ops: each is a dict whose single OP KEY names the op and
# whose value is the selector string. Optional companion fields are ``value``
# (fill text) and ``near`` (disambiguation label) — both strings when present.
_ACTION_OPS: frozenset[str] = frozenset({"click", "fill", "select", "wait_for"})
# The one non-selector op: an explicit wait, keyed by ``wait_ms`` (an int).
_WAIT_MS = "wait_ms"
# Fields allowed ALONGSIDE a selector op. Anything else is an unexpected key.
_OPTIONAL_FIELDS: frozenset[str] = frozenset({"value", "near"})

# The allowed op set as a stable, human-readable list for error messages.
_ALLOWED = sorted(_ACTION_OPS | {_WAIT_MS})


def validate_action(action: Any) -> str | None:
    """Validate one model-supplied action. Return an error string describing the
    refusal, or ``None`` when the action conforms.

    Accepted shapes (generic over ``_ACTION_OPS``):
      * ``{op: <selector:str>, value?: <str>, near?: <str>}`` for op in
        click/fill/select/wait_for — exactly one op key, selector a non-empty str.
      * ``{wait_ms: <int>}`` — a bounded, non-negative integer wait, no other key.

    Anything else (not a dict, no op key, more than one op key, wrong types, an
    unexpected key) is refused so it is never forwarded to the sandbox.
    """
    if not isinstance(action, dict):
        return f"each action must be an object, got {type(action).__name__}"
    keys = set(action)
    if not keys:
        return "empty action: expected one of " + ", ".join(_ALLOWED)

    # The wait_ms form: {wait_ms: <int>} and nothing else.
    if _WAIT_MS in keys:
        if keys != {_WAIT_MS}:
            stray = sorted(keys - {_WAIT_MS})
            return f"wait_ms action must not carry other fields; unexpected {stray}"
        ms = action[_WAIT_MS]
        # bool is a subtype of int in Python but is not a millisecond count here.
        if isinstance(ms, bool) or not isinstance(ms, int):
            return f"wait_ms must be an integer, got {type(ms).__name__}"
        if ms < 0:
            return "wait_ms must be non-negative"
        return None

    # The selector-shaped form: exactly one op key from the allowed set.
    op_keys = keys & _ACTION_OPS
    if not op_keys:
        return (
            f"unknown action {sorted(keys)}; expected one of "
            + ", ".join(_ALLOWED)
        )
    if len(op_keys) > 1:
        return f"action declares multiple ops {sorted(op_keys)}; use exactly one"
    op = next(iter(op_keys))

    selector = action[op]
    if not isinstance(selector, str):
        return f"{op} selector must be a string, got {type(selector).__name__}"
    if not selector.strip():
        return f"{op} selector must not be empty"

    unexpected = sorted(keys - {op} - _OPTIONAL_FIELDS)
    if unexpected:
        return f"unexpected field(s) {unexpected} on a {op} action"
    for field in _OPTIONAL_FIELDS & keys:
        if not isinstance(action[field], str):
            return f"{field!r} must be a string, got {type(action[field]).__name__}"
    return None


def validate_actions(actions: Any) -> str | None:
    """Validate a whole ``actions`` list. Return the FIRST refusal (with its
    position) or ``None`` when every action conforms. A non-list is refused."""
    if not isinstance(actions, (list, tuple)):
        return f"actions must be a list, got {type(actions).__name__}"
    for idx, action in enumerate(actions):
        err = validate_action(action)
        if err is not None:
            return f"action[{idx}] invalid: {err}"
    return None
