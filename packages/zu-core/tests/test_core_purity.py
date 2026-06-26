"""Issue #41 §8 (Conformance) — the core-purity invariant, executably enforced.

The first invariant that gates every #41 decision: ``zu_core`` imports ONLY stdlib
+ pydantic (the readability/table/field-error PARSER lives in ``zu-tools``, never
core — design §0.1, §9.8). If the content_view keystone ever pulled a parser
(selectolax / bs4) or a sibling package (``zu_tools`` / ``zu_shadow`` /
``zu_patterns``) into core, the stdlib+pydantic leaf-of-the-dep-graph guarantee
would silently break and a hostile-content parser would become reachable from the
trust-boundary types.

This proves it by importing ``zu_core`` in a FRESH subprocess (a clean interpreter,
no test-harness imports already loaded) and asserting the set of third-party
top-level packages reachable from it is a SUBSET of pydantic's own closure — i.e.
NO selectolax / bs4 / zu_tools reachable. Stdlib is allowed wholesale (keyed off
``sys.stdlib_module_names``); the allow-set below is exactly pydantic + its
transitive runtime deps.

$0, offline — no live model, no network, no Docker.
"""

from __future__ import annotations

import subprocess
import sys

# pydantic's own runtime closure (pydantic + pydantic-core + their deps). Anything
# OUTSIDE this set AND outside stdlib reachable from a bare ``import zu_core`` is a
# purity violation. Kept deliberately tight: a new entry here is a deliberate review
# decision, not an accident.
_ALLOWED_THIRD_PARTY = frozenset(
    {
        "pydantic",
        "pydantic_core",
        "annotated_types",
        "typing_extensions",
        "typing_inspection",
    }
)

# Site/venv injections that are interpreter-environment artifacts, not a dependency
# of ``zu_core`` (they get loaded by the venv's site setup regardless of what we
# import). Ignored so the test asserts on REAL dependencies only.
_ENV_ARTIFACTS = frozenset({"sitecustomize", "usercustomize"})

# The parser + sibling packages that MUST NOT be reachable from core. The whole
# point of the invariant: a hostile-content parser stays out of the trust-boundary
# leaf, and the dep graph stays one-way (zu_core ← everyone).
_FORBIDDEN = ("selectolax", "bs4", "lxml", "zu_tools", "zu_shadow", "zu_patterns")

# A tiny probe run in a clean interpreter: import zu_core, then print the third-party
# top-level packages that became reachable (stdlib + env artifacts filtered out).
_PROBE = """
import sys
import zu_core  # noqa: F401  (import-for-effect — populates sys.modules)
stdlib = set(sys.stdlib_module_names)
tops = set()
for name in list(sys.modules):
    top = name.split(".")[0]
    if not top or top.startswith("_"):
        continue
    if top == "zu_core" or top in stdlib:
        continue
    tops.add(top)
print("\\n".join(sorted(tops)))
"""


def _reachable_third_party() -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in proc.stdout.splitlines() if line} - _ENV_ARTIFACTS


def test_zu_core_imports_only_stdlib_and_pydantic() -> None:
    # The reachable third-party set must be a SUBSET of pydantic's closure. Pull a
    # parser (e.g. ``import selectolax`` in content_view.py) into core and this set
    # gains 'selectolax' ⊄ the allow-set ⇒ this assertion FAILS. Non-vacuous: the
    # subprocess actually imports core, so the set is the real reachable closure.
    reachable = _reachable_third_party()
    extra = reachable - _ALLOWED_THIRD_PARTY
    assert not extra, f"zu_core reached non-stdlib, non-pydantic packages: {sorted(extra)}"


def test_no_parser_or_sibling_package_reachable_from_core() -> None:
    # The negative control, named explicitly: the parser (selectolax/bs4) and the
    # sibling packages (zu_tools/zu_shadow/zu_patterns) are NOT reachable from a bare
    # ``import zu_core``. This is the property whose violation the subset test catches;
    # asserting the named offenders directly makes the regression message obvious.
    # Revert the leaf-discipline (import zu_tools from content_view.py) and 'zu_tools'
    # shows up here ⇒ FAIL.
    reachable = _reachable_third_party()
    for forbidden in _FORBIDDEN:
        assert forbidden not in reachable, (
            f"{forbidden!r} is reachable from zu_core — the parser/sibling must stay "
            f"out of the stdlib+pydantic leaf (design §0.1, §9.8)"
        )
