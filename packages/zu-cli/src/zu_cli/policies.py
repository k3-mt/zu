"""Declarative guardrails → unbypassable pre-execution gates (issues #76, #74).

Two ``agent.yaml`` blocks compile, at config-load, to ``InvocationGate``s
(``zu_core.ports.InvocationGate``) that the loop runs BEFORE a tool executes. The
policy is DATA, but it is enforced by the gate the policy can't bypass — the model
only emits the ``ToolCall`` that is the gate's input, never its control — and every
decision lands on the hash-chained audit log as ``GATE_DECIDED``.

This compiler lives in the CLI/config layer (which already assembles the run); it
adds NO domain branch to zu-core — it reuses the existing ``InvocationGate`` port,
``Verdict`` vocabulary, and the shared wildcard host matcher (``zu_core.hosts``).

  * ``action_policies`` (#76) — an ORDERED list of rules
    ``{tool, op?, match?, effect: deny|escalate|allow}``. First matching rule wins;
    default allow. ``op`` matches a named call arg (default ``"op"``, the browser
    discriminator); ``match`` is an optional case-insensitive substring/glob over the
    call args. The ``read-only`` preset is a named policy expanding to "deny every
    write-shaped op" — read from each tool's own ``write_ops``/``writes`` declaration,
    never a hardcoded tool-name list.
  * ``allowed_domains`` (#74) — a wildcard host list compiled to a gate that DENIES a
    navigation tool call (http_fetch/render_dom/browser/action_surface) whose target
    URL host matches no pattern, BEFORE it runs. The SAME list also feeds the post-hoc
    ``DOMAIN_ALLOWLIST`` audit invariant (so the two can't drift) and is threaded into
    ``check_url`` for the per-redirect-hop check.

Validation is fail-fast at config-load: an unknown tool, an unknown op for a tool,
or a malformed rule/host pattern raises ``ConfigError`` (surfaced from ``assemble``),
never mid-run.
"""

from __future__ import annotations

import fnmatch
from typing import Any

from zu_core.hosts import host_matches_any, normalize_host
from zu_core.invariants import Invariant, InvariantKind, Predicate, PredicateKind
from zu_core.ports import RunContext, Severity, ToolCall, Verdict

from .config import ConfigError

# The navigation tools an allowed_domains gate guards: each takes a ``url`` arg the
# gate reads the host from. A bare-name set, derived from the tool contract (a URL
# arg), NOT a per-site constant. A tool not in this set is not URL-navigating, so
# the allowlist gate is inert for it (its own egress envelope still governs).
_NAV_URL_TOOLS = ("http_fetch", "render_dom", "browser", "action_surface")

_EFFECTS = {"deny", "escalate", "allow"}
_EFFECT_SEVERITY = {"deny": Severity.DENY, "escalate": Severity.ESCALATE}

# The event/field the post-hoc DOMAIN_ALLOWLIST invariant reads the navigated host
# from: every content-bearing tool observation lands on data.source.fetched carrying
# the URL it fetched under ``url`` (loop._invoke). Reusing it keeps the audit fed
# from the SAME config value as the gate.
_FETCHED_EVENT = "data.source.fetched"
_FETCHED_FIELD = "url"


# --- #76: action_policies -----------------------------------------------------


def _arg_blob(args: dict[str, Any]) -> str:
    """A flat, lower-cased string view of the call args for ``match`` testing —
    content-free (keys + scalar values), never page content."""
    parts: list[str] = []
    for k, v in (args or {}).items():
        parts.append(str(k))
        if isinstance(v, (str, int, float, bool)):
            parts.append(str(v))
        else:
            parts.append(str(v))
    return " ".join(parts).lower()


class _Rule:
    """One compiled action-policy rule (first-match-wins, evaluated in order)."""

    def __init__(self, tool: str, op: str | None, match: str | None, effect: str) -> None:
        self.tool = tool
        self.op = op
        self.match = match.lower() if match else None
        self.effect = effect

    def applies(self, call: ToolCall) -> bool:
        if call.name != self.tool:
            return False
        if self.op is not None and str((call.args or {}).get("op", "")) != self.op:
            return False
        if self.match is not None:
            blob = _arg_blob(call.args or {})
            if self.match not in blob and not fnmatch.fnmatch(blob, self.match):
                return False
        return True


class ActionPolicyGate:
    """A single ``InvocationGate`` compiled from the ordered ``action_policies``
    rules. First matching rule decides (deny/escalate/allow); no match ⇒ allow (the
    inert default). It IS the gate — the policy cannot bypass it."""

    name = "action_policies"
    # A side-effecting deny-gate guarding writes should fail CLOSED if it ever
    # crashes, regardless of the target tool's self-declared tier (loop ZU-CORE-2).
    fail_closed_on_crash = True

    def __init__(self, rules: list[_Rule]) -> None:
        self._rules = rules

    def check(self, call: ToolCall, ctx: RunContext) -> Verdict | None:
        for rule in self._rules:
            if rule.applies(call):
                if rule.effect == "allow":
                    return None  # explicit allow short-circuits later denies
                return Verdict(
                    severity=_EFFECT_SEVERITY[rule.effect],
                    detector=self.name,
                    detail=f"action_policies: {rule.effect} {call.name}"
                    + (f" op={rule.op}" if rule.op else ""),
                )
        return None  # default allow


def _write_shaped_rules(tool_name: str, tool: Any) -> list[_Rule]:
    """The read-only preset, expanded for ONE tool from its OWN declaration:

      * ``writes = True``      ⇒ the whole tool is write-shaped → deny every call.
      * ``write_ops = {…}``    ⇒ deny each listed op (an op-arg value).

    A tool declaring neither is read-only by nature, so the preset adds nothing for
    it. This is the generic write-shaped signal — read off the tool, never a
    hardcoded tool-name list."""
    rules: list[_Rule] = []
    if bool(getattr(tool, "writes", False)):
        rules.append(_Rule(tool_name, None, None, "deny"))
    for op in getattr(tool, "write_ops", ()) or ():
        rules.append(_Rule(tool_name, str(op), None, "deny"))
    return rules


def _known_ops(tool: Any) -> set[str] | None:
    """The op enum a tool declares in its schema (``parameters.properties.op.enum``),
    or ``None`` when the tool has no ``op`` discriminator — so an ``op:`` rule on a
    tool with no ops is a config error, caught fast."""
    schema = getattr(tool, "schema", None)
    if not isinstance(schema, dict):
        return None
    props = (schema.get("parameters") or {}).get("properties") or {}
    op = props.get("op") if isinstance(props, dict) else None
    if isinstance(op, dict) and isinstance(op.get("enum"), list):
        return {str(x) for x in op["enum"]}
    return None


def compile_action_policies(
    raw: list[Any], tools: dict[str, Any]
) -> ActionPolicyGate | None:
    """Compile the ``action_policies`` block into ONE gate, validating against the
    active ``tools`` map (name → instance). Raises ``ConfigError`` on a malformed
    rule, an unknown tool, an unknown op, or an unknown preset — at config-load,
    not mid-run. Returns ``None`` for an empty/absent block (the seam stays inert)."""
    if not raw:
        return None
    rules: list[_Rule] = []
    for i, entry in enumerate(raw):
        if isinstance(entry, str):
            # a named preset (the only one shipped is "read-only")
            if entry == "read-only":
                for name, tool in sorted(tools.items()):
                    rules.extend(_write_shaped_rules(name, tool))
                continue
            raise ConfigError(
                f"action_policies[{i}]: unknown preset {entry!r}; the only preset is "
                "'read-only'. (A rule is a mapping {tool, op?, match?, effect}.)"
            )
        if not isinstance(entry, dict):
            raise ConfigError(
                f"action_policies[{i}]: each rule must be a mapping "
                "{tool, op?, match?, effect} or the preset name 'read-only'."
            )
        if entry.get("preset") == "read-only":
            for name, tool in sorted(tools.items()):
                rules.extend(_write_shaped_rules(name, tool))
            continue
        tool_name = entry.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            raise ConfigError(f"action_policies[{i}]: missing/invalid 'tool'.")
        if tool_name not in tools:
            raise ConfigError(
                f"action_policies[{i}]: unknown tool {tool_name!r}; active tools: "
                f"{', '.join(sorted(tools)) or 'none'}."
            )
        effect = entry.get("effect")
        if effect not in _EFFECTS:
            raise ConfigError(
                f"action_policies[{i}]: 'effect' must be one of {sorted(_EFFECTS)}, "
                f"got {effect!r}."
            )
        op = entry.get("op")
        if op is not None:
            if not isinstance(op, str):
                raise ConfigError(f"action_policies[{i}]: 'op' must be a string.")
            known = _known_ops(tools[tool_name])
            if known is None:
                raise ConfigError(
                    f"action_policies[{i}]: tool {tool_name!r} has no 'op' parameter, "
                    f"so an op rule cannot apply to it."
                )
            if op not in known:
                raise ConfigError(
                    f"action_policies[{i}]: unknown op {op!r} for tool {tool_name!r}; "
                    f"known ops: {', '.join(sorted(known))}."
                )
        match = entry.get("match")
        if match is not None and not isinstance(match, str):
            raise ConfigError(f"action_policies[{i}]: 'match' must be a string.")
        rules.append(_Rule(tool_name, op, match, effect))
    if not rules:
        return None
    return ActionPolicyGate(rules)


# --- #74: allowed_domains -----------------------------------------------------


def _host_of_call(call: ToolCall) -> str | None:
    """The target host of a navigation call, from its ``url`` arg. ``None`` when the
    call carries no url (e.g. browser op=read/act/close — no navigation to gate)."""
    from urllib.parse import urlsplit

    url = (call.args or {}).get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


class AllowedDomainsGate:
    """An ``InvocationGate`` that DENIES a navigation tool call whose target host
    matches no ``allowed_domains`` pattern — BEFORE it runs. Inert for non-nav tools
    and for nav calls that carry no url (op=read/act/close). Uses the SAME shared
    matcher (``zu_core.hosts``) the redirect-hop ``check_url`` and the audit invariant
    use, so all three agree."""

    name = "allowed_domains"
    fail_closed_on_crash = True

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns

    def check(self, call: ToolCall, ctx: RunContext) -> Verdict | None:
        if call.name not in _NAV_URL_TOOLS:
            return None
        host = _host_of_call(call)
        if host is None:
            return None  # a non-navigating op (read/act/close) — nothing to gate
        if host_matches_any(normalize_host(host), self._patterns):
            return None
        return Verdict(
            severity=Severity.DENY,
            detector=self.name,
            detail=f"allowed_domains: host {host!r} not in {self._patterns!r}",
        )


def _validate_host_pattern(p: Any, i: int) -> str:
    if not isinstance(p, str) or not p.strip():
        raise ConfigError(f"allowed_domains[{i}]: each entry must be a non-empty host pattern.")
    s = p.strip()
    if " " in s or "/" in s or ":" in s:
        raise ConfigError(
            f"allowed_domains[{i}]: {p!r} is not a host pattern — give a bare host or "
            "wildcard (e.g. 'api.example.com' or '*.example.com'), not a URL."
        )
    # a pattern that normalises to nothing (e.g. just dots) is malformed
    if not normalize_host(s):
        raise ConfigError(f"allowed_domains[{i}]: {p!r} is not a valid host pattern.")
    return s


def compile_allowed_domains(raw: Any) -> list[str] | None:
    """Validate + normalise the ``allowed_domains`` block. Raises ``ConfigError`` on
    a malformed list/pattern at config-load. Returns the pattern list, or ``None``
    for an empty/absent block."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ConfigError("allowed_domains: must be a list of host patterns.")
    if not raw:
        return None
    return [_validate_host_pattern(p, i) for i, p in enumerate(raw)]


def allowed_domains_invariant(patterns: list[str]) -> Invariant:
    """The post-hoc ``DOMAIN_ALLOWLIST`` audit invariant fed from the SAME pattern
    list as the gate (issue #74) — so the enforced gate and the audit backstop can't
    drift. It folds ``data.source.fetched`` events and flags any fetched url whose
    host matches no pattern (wildcard mode). Compiles to a Monitor via
    ``zu_core.invariants.compile_invariant``."""
    return Invariant(
        name="allowed-domains-allowlist",
        kind=InvariantKind.THROUGHOUT,
        predicate=Predicate(
            kind=PredicateKind.DOMAIN_ALLOWLIST,
            params={
                "event_type": _FETCHED_EVENT,
                "field": _FETCHED_FIELD,
                "allow": list(patterns),
                "wildcard": True,
            },
        ),
    )


__all__ = [
    "ActionPolicyGate",
    "AllowedDomainsGate",
    "compile_action_policies",
    "compile_allowed_domains",
    "allowed_domains_invariant",
]
