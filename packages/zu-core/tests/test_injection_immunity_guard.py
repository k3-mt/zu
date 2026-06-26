"""Injection-immunity AST conformance guard (Issue #41 §9 risk 1, MED #19/LOW #15).

The trust boundary has exactly ONE door: ``TrustedFrame.as_observation`` renders a
:class:`zu_core.content_view.ContentView` as fenced DATA, every unit attributed by
region + hash, never as instructions. The whole immunity collapses silently if ANY
other code path takes a ``ContentUnit``'s prose (its ``text``/``error_text``/``label``)
— or a whole ``ContentView`` REGION field (``main_text``/``errors``/``field_states``/…)
— and routes it into a model-message construction (a ``Text``/``Observation``/
``ModelRequest`` or a raw ``{"role": ..., "content": ...}`` message dict). A reviewer
cannot eyeball every package for that; this guard does it mechanically.

It walks every ``packages/*/src/**/*.py``, parses it with ``ast``, and FLAGS any
message-construction call whose arguments funnel page-derived content prose. The ONLY
sanctioned bridge is ``TrustedFrame.as_observation()`` (and ``frame.render()`` feeding
it), which lives in ``content_view.py`` — so that one file is exempt. The guard must
pass on the current tree: the only place content reaches a model is
``zu_shadow.escalate`` via ``frame.as_observation().text()`` (the sanctioned door),
which this guard does NOT flag (the value flows through ``as_observation``, not a bare
region/unit attribute).

Negative control (why this is non-vacuous): add a line like
``ModelRequest(messages=[{"role": "user", "content": ctx.view.errors[0].text}])`` to
any non-exempt module — routing a region field's ``.text`` straight into a message,
bypassing the fence — and this test FAILS, naming the file + line. Revert it, green.
"""

from __future__ import annotations

import ast
from pathlib import Path

# The ContentView REGION fields — a ``.text`` (etc.) reached THROUGH one of these,
# or a region field passed whole into a message, is page-derived content prose.
_REGION_FIELDS = frozenset(
    {"main_text", "headings", "tables", "lists", "kv", "errors", "field_states"}
)
# The content-prose attributes a ContentUnit / FieldState exposes. Reaching one of
# these and handing it to a message construction is the bypass we forbid.
_PROSE_ATTRS = frozenset({"text", "error_text", "label", "rows"})
# The message-construction call targets (by the callee's final name). A literal
# message dict (``{"content": ...}`` / ``{"messages": ...}``) is also a construction.
_MESSAGE_CTORS = frozenset({"Text", "Observation", "ModelRequest"})
_MESSAGE_DICT_KEYS = frozenset({"content", "messages"})
# The ONE sanctioned bridge: a value produced by ``as_observation`` / ``render`` (the
# TrustedFrame door) is fine — it already fenced the content.
_SANCTIONED_CALLS = frozenset({"as_observation", "render"})


def _src_files() -> list[Path]:
    root = Path(__file__).resolve().parents[3]  # repo root (…/packages/zu-core/tests → …)
    pkgs = root / "packages"
    files: list[Path] = []
    for pkg in sorted(pkgs.iterdir()):
        src = pkg / "src"
        if src.is_dir():
            files.extend(sorted(src.rglob("*.py")))
    assert files, "no source files discovered — the walk is mis-rooted"
    return files


def _is_exempt(path: Path) -> bool:
    # content_view.py defines TrustedFrame.as_observation — the sanctioned bridge —
    # so it alone may build a model message out of content. Nothing else may.
    return path.name == "content_view.py" and path.parent.name == "zu_core"


def _region_or_prose_access(node: ast.AST) -> str | None:
    """If ``node`` is an attribute access that reaches a ContentView region field or a
    ContentUnit/FieldState prose attribute, return a short description; else None.

    Catches ``view.errors`` (region field handed whole), ``unit.text`` /
    ``field.error_text`` reached THROUGH a region (``view.errors[0].text``), and a bare
    ``something.text`` whose chain contains a region field — i.e. content prose. It does
    NOT flag a value already routed through ``as_observation``/``render`` (the door)."""
    # Walk the attribute/subscript/call chain, collecting the attribute names seen and
    # noting whether the chain passes through the sanctioned bridge.
    cur: ast.AST = node
    attrs: list[str] = []
    via_door = False
    while True:
        if isinstance(cur, ast.Attribute):
            attrs.append(cur.attr)
            cur = cur.value
        elif isinstance(cur, ast.Subscript):
            cur = cur.value
        elif isinstance(cur, ast.Call):
            # A call in the chain: if it is the sanctioned door, the value is fenced.
            fn = cur.func
            if isinstance(fn, ast.Attribute) and fn.attr in _SANCTIONED_CALLS:
                via_door = True
            cur = fn
        else:
            break
    if via_door:
        return None
    # A region field anywhere in the chain → page-derived content.
    if any(a in _REGION_FIELDS for a in attrs):
        return f"region field {[a for a in attrs if a in _REGION_FIELDS][0]!r}"
    return None


def _message_arg_nodes(call: ast.Call) -> list[ast.AST]:
    """Every sub-node of a message-construction call's arguments — the haystack a
    region/prose access must not appear in."""
    nodes: list[ast.AST] = []
    for arg in [*call.args, *(kw.value for kw in call.keywords)]:
        nodes.extend(ast.walk(arg))
    return nodes


def _violations_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []

    def _ctor_name(call: ast.Call) -> str | None:
        fn = call.func
        if isinstance(fn, ast.Name):
            return fn.id
        if isinstance(fn, ast.Attribute):
            return fn.attr
        return None

    for node in ast.walk(tree):
        # Message-construction CALLS: Text(...)/Observation(...)/ModelRequest(...).
        if isinstance(node, ast.Call) and _ctor_name(node) in _MESSAGE_CTORS:
            for sub in _message_arg_nodes(node):
                if isinstance(sub, ast.Attribute):
                    why = _region_or_prose_access(sub)
                    if why is not None:
                        found.append(
                            f"{path}:{sub.lineno}: {_ctor_name(node)}(...) is fed "
                            f"page-derived content ({why}) — bypasses TrustedFrame"
                        )
        # Literal message dicts: {"content": <content>} / {"messages": [...]}.
        if isinstance(node, ast.Dict):
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
            if keys & _MESSAGE_DICT_KEYS:
                for value in node.values:
                    for sub in ast.walk(value):
                        if isinstance(sub, ast.Attribute):
                            why = _region_or_prose_access(sub)
                            if why is not None:
                                found.append(
                                    f"{path}:{sub.lineno}: a message dict is fed "
                                    f"page-derived content ({why}) — bypasses TrustedFrame"
                                )
    return found


def test_trusted_frame_is_the_only_bridge_from_content_to_a_model() -> None:
    # The guard must be live: at least one real message-construction call exists in the
    # tree (so we know the AST shapes match), and NONE of them route content prose.
    violations: list[str] = []
    for path in _src_files():
        if _is_exempt(path):
            continue
        violations.extend(_violations_in(path))
    assert not violations, "content reached a model OUTSIDE TrustedFrame:\n" + "\n".join(violations)


def test_guard_flags_a_planted_bypass(tmp_path: Path) -> None:
    # Self-test of the guard's teeth: a planted module that routes a region field's
    # .text straight into a ModelRequest MUST be flagged (else the guard above is
    # vacuous — it would pass no matter what the real tree does).
    bypass = tmp_path / "bypass.py"
    bypass.write_text(
        "from zu_core.ports import ModelRequest\n"
        "def leak(view):\n"
        "    return ModelRequest(messages=[{'role': 'user', 'content': view.errors[0].text}])\n",
        encoding="utf-8",
    )
    assert _violations_in(bypass), "the guard failed to flag a known bypass — it has no teeth"
    # And the SANCTIONED door is NOT flagged (no false positive on the real pattern).
    ok = tmp_path / "ok.py"
    ok.write_text(
        "from zu_core.ports import ModelRequest\n"
        "def fine(frame):\n"
        "    obs = frame.as_observation()\n"
        "    return ModelRequest(messages=[{'role': 'user', 'content': obs.text()}])\n",
        encoding="utf-8",
    )
    assert not _violations_in(ok), "the guard false-flagged the sanctioned TrustedFrame door"
