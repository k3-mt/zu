"""action_surface — the perception-reduction tool (tier 3, Engineering Design §11).

A rendered web page is a DOM of 100k–1M+ tokens; the decision the agent needs
from it — "click Place order" — is a handful. Pushing the whole blob through the
model is slow, expensive, and *worse for accuracy* (the signal drowns in
markup). The way out is a reframe: the agent almost never needs the page — it
needs the **set of things it can do** on the page. That set is a few dozen
affordances, a few hundred tokens.

This tool produces that set, **deterministically**. The decision rule (§4.5)
settles why it is a tool and not a model job: a script may *enumerate what is
possible* (every actionable element), but it must not *decide what is reasonable*
(which one to pick) — that is the policy's judgment. So the reducer surfaces the
possible and never ranks or prunes by guessed task-relevance.

The pipeline (§11.2), run over an accessibility tree rather than the raw DOM:

  1. Walk the accessibility tree — roles, names, states — an order of magnitude
     smaller than the DOM, built to answer "what can a user do here".
  2. Filter to interactive + meaningful (actions, plus the headings/labels/errors
     an action needs); drop the rest.
  3. Prune the invisible — ignored, off-screen, zero-area, hidden.
  4. Resolve a stable, human-meaningful label per element.
  5. Assign a stable, opaque handle (a1, a2 …) that maps back, harness-side, to a
     role+name locator. The model emits the handle, never a selector (§11.3).
  6. Emit a compact, typed representation.

And the competence boundary (§11.4): the honest risk is a false negative —
pruning the one element the task needed (a canvas button, an unlabeled icon). So
the reducer must know when it is **blind** and *signal* escalation to tier-4
vision rather than silently return an incomplete surface. ``blind`` on the
result is that signal; the ``action-surface-blind`` detector turns it into an
ESCALATE. Graceful degradation, never silent incompleteness.

The deterministic reducer (:func:`reduce_surface`) is the whole value and is
pure — it runs on an accessibility-tree snapshot with no browser, which is how a
coding harness drives it offline and how it is tested at $0. The live arm asks a
browser session for the tree (:meth:`ActionSurface.__call__` with ``op=open``)
and runs the same reducer over it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from zu_core.ports import CAP_NET, CAP_SANDBOX, EGRESS_OPEN, BrowserSessionHandle, SessionBackend

from .net import validate_and_pin

_DEFAULT_IMAGE = "ghcr.io/k3-mt/zu-render-chromium:latest"

# Roles that represent something the agent can *do*. The list is generous on
# purpose — enumerating the possible is the job; choosing among it is the
# policy's. Anything actionable a real accessibility tree exposes belongs here.
INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio",
    "switch", "slider", "spinbutton", "menuitem", "menuitemcheckbox",
    "menuitemradio", "tab", "option", "textarea", "listbox", "menubutton",
    "togglebutton", "datepicker", "colorwell",
})

# Roles whose *text* is meaningful context for choosing an action — headings
# orient, alerts/status carry the error and validation text an action needs —
# but which are not themselves actionable. We keep their names as context, never
# as affordances.
CONTEXT_ROLES: frozenset[str] = frozenset({
    "heading", "alert", "status", "alertdialog", "log", "marquee",
})


class AxNode(BaseModel):
    """One normalised accessibility-tree node — the reducer's input currency.

    A small, serialisable shape so the reducer is pure and a harness can feed it
    a captured tree directly. :func:`normalize_axtree` produces these from the
    raw CDP ``Accessibility.getFullAXTree`` format.
    """

    role: str
    name: str = ""
    value: str | None = None
    states: list[str] = Field(default_factory=list)
    placeholder: str | None = None
    description: str | None = None
    # Pruning inputs. ``visible`` folds in aria-hidden/display:none/off-screen;
    # ``ignored`` is the tree's own "not exposed" flag; ``bounds`` is [x,y,w,h].
    visible: bool = True
    ignored: bool = False
    bounds: list[float] | None = None


class Affordance(BaseModel):
    """One thing the policy can do, addressed by an opaque handle."""

    handle: str
    role: str
    label: str
    value: str | None = None
    states: list[str] = Field(default_factory=list)


class Surface(BaseModel):
    """The compact, typed reduction of a page — a few hundred tokens.

    ``handle_map`` is the harness-side indirection (§11.3): handle → role+name
    locator. The model only ever sees and emits handles; the durable locator
    stays here and is re-resolved at action time.
    """

    title: str = ""
    url: str = ""
    affordances: list[Affordance] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    handle_map: dict[str, dict] = Field(default_factory=dict)
    blind: bool = False
    blind_reason: str | None = None


def _label_of(node: AxNode) -> str:
    """The stable, human-meaningful label (§11.2 step 4): accessible name first
    (which already folds in aria-label and an associated <label>), then
    placeholder, then description. Class soup never reaches here — if none of
    these is set, the element is unlabeled and counts toward blindness."""
    for candidate in (node.name, node.placeholder, node.description):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _is_pruned(node: AxNode) -> bool:
    """Step 3 — prune the invisible. ignored / not-visible / zero-area go."""
    if node.ignored or not node.visible:
        return True
    if node.bounds is not None and len(node.bounds) == 4:
        w, h = node.bounds[2], node.bounds[3]
        if w <= 0 or h <= 0:
            return True
    return False


def reduce_surface(
    nodes: list[AxNode],
    *,
    title: str = "",
    url: str = "",
    unlabeled_ratio: float = 0.5,
) -> Surface:
    """Reduce an accessibility tree to the action surface — pure, deterministic.

    Handles are assigned ``a1, a2 …`` in document (input) order over the emitted
    affordances, so the same tree always yields the same handles. The blind
    signal (§11.4) fires when the surface cannot be trusted to be complete: the
    page had content but yielded no affordances, or too large a fraction of the
    interactive elements have no resolvable label (a canvas/icon-heavy page the
    accessibility tree describes poorly).
    """
    affordances: list[Affordance] = []
    handle_map: dict[str, dict] = {}
    context: list[str] = []
    unlabeled = 0
    interactive_seen = 0
    kept_any_content = False

    for node in nodes:
        if _is_pruned(node):
            continue
        kept_any_content = True
        role = node.role

        if role in CONTEXT_ROLES:
            label = _label_of(node)
            if label:
                context.append(label)
            continue

        if role in INTERACTIVE_ROLES:
            interactive_seen += 1
            label = _label_of(node)
            if not label:
                # Enumerated as possible, but unaddressable — a blindness signal,
                # not a meaningless handle handed to the model.
                unlabeled += 1
                continue
            handle = f"a{len(affordances) + 1}"
            affordances.append(
                Affordance(
                    handle=handle,
                    role=role,
                    label=label,
                    value=node.value,
                    states=list(node.states),
                )
            )
            # The durable locator the model never sees (role + accessible name).
            handle_map[handle] = {"role": role, "name": label}

    blind = False
    blind_reason: str | None = None
    if not affordances and kept_any_content:
        blind = True
        blind_reason = "page had content but the accessibility tree yielded no addressable actions"
    elif interactive_seen and (unlabeled / interactive_seen) > unlabeled_ratio:
        blind = True
        blind_reason = (
            f"{unlabeled}/{interactive_seen} interactive elements are unlabeled "
            "in the accessibility tree — too thin to trust"
        )

    return Surface(
        title=title,
        url=url,
        affordances=affordances,
        context=context,
        handle_map=handle_map,
        blind=blind,
        blind_reason=blind_reason,
    )


def _ax_string(field: Any) -> str:
    """Read a CDP AX value object ``{"type":...,"value":...}`` as a string."""
    if isinstance(field, dict):
        v = field.get("value")
        return str(v) if v is not None else ""
    return ""


def normalize_axtree(cdp_nodes: list[dict]) -> list[AxNode]:
    """Normalise the raw CDP ``Accessibility.getFullAXTree`` node list into
    :class:`AxNode` records, in document (pre-order) order as CDP returns them.

    CDP shape per node: ``role``/``name`` are ``{type,value}`` objects;
    ``properties`` is a list of ``{name, value:{value}}``; ``ignored`` is a bool.
    States we surface: disabled, checked, expanded, required, focused, selected,
    invalid. Placeholder/description/value are read from their AX properties.
    """
    out: list[AxNode] = []
    state_props = {"disabled", "checked", "expanded", "required", "focused", "selected", "invalid"}
    for n in cdp_nodes:
        role = _ax_string(n.get("role"))
        if not role:
            continue
        props = {p.get("name"): p.get("value", {}) for p in n.get("properties", []) if isinstance(p, dict)}
        states: list[str] = []
        for sp in sorted(state_props):
            val = props.get(sp, {})
            v = val.get("value") if isinstance(val, dict) else None
            if v is True or (isinstance(v, str) and v not in ("false", "")):
                states.append(sp if not isinstance(v, str) or v == "true" else f"{sp}:{v}")
        out.append(
            AxNode(
                role=role,
                name=_ax_string(n.get("name")),
                value=_ax_string(n.get("value")) or None,
                states=states,
                placeholder=_ax_string(props.get("placeholder")) or None,
                description=_ax_string(n.get("description")) or None,
                ignored=bool(n.get("ignored", False)),
                # CDP marks unexposed nodes via ``ignored``; visibility off-screen
                # is folded into ``hidden`` when the server supplies bounds.
                visible=not bool(props.get("hidden", {}).get("value", False))
                if isinstance(props.get("hidden"), dict) else True,
            )
        )
    return out


class ActionSurface:
    """Tier-3 tool: reduce a page to its action surface (and keep the handle map).

    Two ways in, one reducer:

      * ``op=reduce`` (default) — reduce a tree the caller already has. Pass
        ``nodes`` (AxNode dicts) or raw ``axtree`` (CDP nodes), plus ``title`` /
        ``url``. No browser, fully offline — the harness-driven and tested path.
      * ``op=open`` — open ``url`` in a headless browser session, ask it for the
        accessibility tree, and reduce that. The live arm.

    After a reduction the handle→locator map is held on the instance for the run;
    ``op=resolve`` returns the durable locator for a handle (a stale handle is an
    escalation, not a crash — the caller re-resolves at action time, §11.3).
    """

    name = "action_surface"
    tier = 3  # the accessibility-tree tier; unlocked by a detector ESCALATE
    schema = {
        "name": "action_surface",
        "description": (
            "Reduce a web page to the compact SET OF THINGS YOU CAN DO on it — a "
            "flat list of affordances (button/link/textbox/…) each with an opaque "
            "handle (a1, a2 …) and a human label. You choose a handle and act on "
            "it; you never see or emit a CSS selector. op=open a url to capture and "
            "reduce its accessibility tree; op=resolve a handle to its locator. If "
            "'blind' is true the tree is too thin to trust — escalate to vision."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["reduce", "open", "resolve"]},
                "url": {"type": "string", "description": "for op=open: the page to reduce"},
                "handle": {"type": "string", "description": "for op=resolve: the handle to resolve"},
                "axtree": {"type": "array", "items": {"type": "object"},
                           "description": "for op=reduce: raw CDP getFullAXTree nodes"},
                "nodes": {"type": "array", "items": {"type": "object"},
                          "description": "for op=reduce: pre-normalised AxNode dicts"},
                "title": {"type": "string"},
            },
            "required": ["op"],
        },
    }
    prompt_fragment = (
        "action_surface(op=open, url): reduce a page to a short list of affordances "
        "(handles a1,a2,… with labels) instead of reading the whole DOM. Pick a handle "
        "to act on; resolve(handle) gives its locator. 'blind' means escalate to vision."
    )
    capabilities = frozenset({CAP_NET, CAP_SANDBOX})
    egress = frozenset({EGRESS_OPEN})

    def __init__(
        self,
        backend: SessionBackend | None = None,
        image: str = _DEFAULT_IMAGE,
        *,
        allow_private: bool | None = None,
        unlabeled_ratio: float = 0.5,
    ) -> None:
        self._backend = backend
        self.image = image
        self.allow_private = allow_private
        self.unlabeled_ratio = unlabeled_ratio
        self._handle_map: dict[str, dict] = {}
        self._session: BrowserSessionHandle | None = None

    def _resolve_backend(self) -> SessionBackend:
        if self._backend is None:
            from zu_backends.local_docker import LocalDockerBackend

            self._backend = LocalDockerBackend()
        return self._backend

    async def __call__(
        self,
        ctx: Any,
        op: str = "reduce",
        url: str | None = None,
        handle: str | None = None,
        axtree: list | None = None,
        nodes: list | None = None,
        title: str | None = None,
    ) -> dict:
        if op == "reduce":
            return self._reduce_op(nodes=nodes, axtree=axtree, title=title or "", url=url or "")

        if op == "resolve":
            if not handle:
                return {"error": "op=resolve requires a handle"}
            locator = self._handle_map.get(handle)
            if locator is None:
                # Stale/unknown handle: signal a re-resolve, never a crash (§11.3).
                return {"stale_handle": handle,
                        "error": f"handle {handle!r} is not on the current surface; re-capture"}
            return {"handle": handle, "locator": locator}

        if op == "open":
            if not url:
                return {"error": "op=open requires a url"}
            return await self._open_op(url, title or "")

        return {"error": f"unknown op {op!r}; use reduce/open/resolve"}

    def _reduce_op(self, *, nodes: list | None, axtree: list | None, title: str, url: str) -> dict:
        if nodes is not None:
            ax = [n if isinstance(n, AxNode) else AxNode.model_validate(n) for n in nodes]
        elif axtree is not None:
            ax = normalize_axtree([n for n in axtree if isinstance(n, dict)])
        else:
            return {"error": "op=reduce requires 'nodes' or 'axtree'"}
        surface = reduce_surface(ax, title=title, url=url, unlabeled_ratio=self.unlabeled_ratio)
        return self._emit(surface)

    async def _open_op(self, url: str, title: str) -> dict:
        await self._close_session()
        pinned_ip = validate_and_pin(url, allow_private=self.allow_private)
        spec: dict[str, Any] = {"image": self.image, "tier": self.tier, "network": True}
        host = urlsplit(url).hostname
        if pinned_ip is not None and host:
            spec["extra_hosts"] = {host: pinned_ip}
        self._session = await self._resolve_backend().open_session(spec)
        # Ask the session for the accessibility tree. The browser server returns
        # ``{axtree: [...CDP nodes...], title, url}``; an older server that lacks
        # the op returns an error, which we surface (not a crash).
        resp = await self._session.send({"op": "axtree", "url": url})
        if not isinstance(resp, dict) or resp.get("axtree") is None:
            err = resp.get("error") if isinstance(resp, dict) else "bad session response"
            return {"error": f"could not capture accessibility tree: {err}"}
        ax = normalize_axtree([n for n in resp["axtree"] if isinstance(n, dict)])
        surface = reduce_surface(
            ax,
            title=title or str(resp.get("title", "")),
            url=str(resp.get("url", url)),
            unlabeled_ratio=self.unlabeled_ratio,
        )
        return self._emit(surface)

    def _emit(self, surface: Surface) -> dict:
        """The surface as a loop-friendly observation. The handle map is held on
        the instance (harness-side) and echoed for the harness; ``surface_blind``
        is the top-level flag the blind detector reads."""
        self._handle_map = dict(surface.handle_map)
        return {
            "action_surface": surface.model_dump(exclude={"handle_map"}),
            "handle_map": surface.handle_map,
            "surface_blind": surface.blind,
        }

    async def _close_session(self) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            try:
                await session.close()
            except Exception:  # noqa: BLE001 — teardown must not raise over a result
                pass

    async def aclose(self) -> None:
        """Close any lingering session — for run teardown so a container never leaks."""
        await self._close_session()
