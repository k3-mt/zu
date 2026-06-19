"""Gate 2 — port/contract conformance.

Automated checks that a plugin correctly implements its port: the right shape,
types, and — for tools — a declared capability envelope (least privilege is part
of the contract, PHILOSOPHY.md §7). Deterministic and dependency-free; this is
the cheap gate that runs before a plugin is ever stood up in a runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zu_core.ports import Scope


@dataclass(frozen=True)
class ContractFinding:
    plugin: str
    detail: str


def _is_str(x: Any) -> bool:
    return isinstance(x, str) and bool(x)


def check_plugin(kind: str, name: str, obj: Any) -> list[ContractFinding]:
    """Return the conformance findings for one plugin (empty == conformant)."""
    where = f"{kind}:{name}"
    out: list[ContractFinding] = []

    def need(cond: bool, detail: str) -> None:
        if not cond:
            out.append(ContractFinding(where, detail))

    if kind == "tools":
        need(_is_str(getattr(obj, "name", None)), "missing a non-empty str `name`")
        need(isinstance(getattr(obj, "schema", None), dict), "missing a dict `schema`")
        need(_is_str(getattr(obj, "prompt_fragment", None)), "missing a str `prompt_fragment`")
        need(isinstance(getattr(obj, "tier", None), int), "missing an int `tier`")
        need(callable(getattr(obj, "__call__", None)), "is not callable")
        # The capability envelope is part of the contract — declare least privilege
        # explicitly rather than relying on the loop's safe default.
        caps = getattr(obj, "capabilities", None)
        egress = getattr(obj, "egress", None)
        need(_is_iterable_of_str(caps), "does not declare `capabilities` (least privilege)")
        need(_is_iterable_of_str(egress), "does not declare `egress` (host allowlist)")
    elif kind == "detectors":
        need(_is_str(getattr(obj, "name", None)), "missing a non-empty str `name`")
        need(isinstance(getattr(obj, "scope", None), Scope), "missing a `scope` of type Scope")
        need(callable(getattr(obj, "inspect", None)), "missing an `inspect` method")
    elif kind == "validators":
        need(_is_str(getattr(obj, "name", None)), "missing a non-empty str `name`")
        need(callable(getattr(obj, "check", None)), "missing a `check` method")
    elif kind == "providers":
        need(hasattr(obj, "capabilities"), "missing `capabilities`")
        need(hasattr(obj, "model"), "missing the `model` attribute/property")
        need(callable(getattr(obj, "complete", None)), "missing a `complete` method")
    elif kind == "backends":
        for m in ("launch", "exec", "destroy"):
            need(callable(getattr(obj, m, None)), f"missing a `{m}` method")
    elif kind == "sinks":
        for m in ("append", "query", "stream", "count"):
            need(callable(getattr(obj, m, None)), f"missing a `{m}` method")
    else:
        out.append(ContractFinding(where, f"unknown plugin kind {kind!r}"))

    return out


def _is_iterable_of_str(x: Any) -> bool:
    if x is None or isinstance(x, (str, bytes)):
        return False
    try:
        return all(isinstance(i, str) for i in x)
    except TypeError:
        return False
