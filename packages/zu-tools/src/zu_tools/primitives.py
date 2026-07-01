"""primitives — the reference interaction-primitive family + composition runtime (#125).

The four connected-surface resolvers (consent #94, selection #95, checkout #117, cart
#122) plus the commit boundary, RE-EXPRESSED as ONE closed vocabulary of interchangeable
:class:`~zu_core.ports.InteractionPrimitive`\\ s, and a thin
:class:`~zu_core.ports.PrimitiveRuntime` that dispatches ``{kind, hint}`` over them. The
shipped resolvers do the heavy lifting; these ADAPT them to one uniform contract
(inspect → apply → verify) so a host drives ONE loop instead of N hardcoded blocks:

  * ``dismiss``     wraps :class:`~zu_tools.consent.WholeWordConsentResolver` (#94).
  * ``choose_one``  is :class:`~zu_tools.choose.ChooseOne` (#125) — generalises
                    :class:`~zu_tools.selection.FirstOptionSelectionSatisfier` (#95).
  * ``advance``     wraps :class:`~zu_tools.cart.WholeWordCartAdder` (#122) +
                    :class:`~zu_tools.checkout.WholeWordCheckoutProceeder` (#117) — the
                    primary move-forward control, add-to-cart OR proceed-to-checkout.
  * ``search``      types a query into the recognised search box and submits.
  * ``commit_stop`` recognises the IRREVERSIBLE boundary (a card/pay field is present)
                    and STOPS — it never acts, so the boundary is never crossed.

Content-free throughout; the commit boundary is absolute (``advance`` excludes committing
controls, ``commit_stop`` only ever reports).
"""

from __future__ import annotations

from zu_core.ports import (
    ConnectedSurface,
    InteractionPrimitive,
    PrimitiveOutcome,
    PrimitivePlan,
    SurfaceAction,
)
from zu_core.surface import SurfaceAffordance, SurfaceView

from ._commerce import (
    CLICKABLE_ROLES,
    add_to_cart_handle,
    has_card_field,
    in_cart,
    is_commit,
)
from ._wholeword import matches_whole_word
from .cart import WholeWordCartAdder
from .checkout import WholeWordCheckoutProceeder
from .choose import ChooseOne
from .consent import WholeWordConsentResolver

# --- dismiss ----------------------------------------------------------------- #


class DismissPrimitive:
    """``dismiss`` — clear a consent/interstitial banner, over the shipped
    :class:`~zu_tools.consent.WholeWordConsentResolver` (#94)."""

    __zu_interface__ = 1
    name = "dismiss"
    kind = "dismiss"

    def __init__(self, resolver: WholeWordConsentResolver | None = None) -> None:
        self._resolver = resolver or WholeWordConsentResolver()

    def inspect(self, view: SurfaceView, *, hint: str | None = None) -> PrimitivePlan:
        ctrl = self._resolver.find(view)
        return PrimitivePlan(
            kind=self.kind,
            applicable=ctrl is not None,
            handles=(ctrl.handle,) if ctrl else (),
            detail=ctrl.kind if ctrl else "",
        )

    async def apply(
        self, surface: ConnectedSurface, *, hint: str | None = None
    ) -> PrimitiveOutcome:
        cleared = await self._resolver.dismiss(surface)
        return PrimitiveOutcome(
            kind=self.kind, progress="advance" if cleared else "no_op",
            detail="banner cleared" if cleared else "no banner / not cleared",
        )


# --- advance ----------------------------------------------------------------- #


class AdvancePrimitive:
    """``advance`` — click the primary MOVE-FORWARD control, unifying add-to-cart (#122)
    and proceed-to-checkout (#117). On a product it adds to cart; once in the cart it
    advances toward checkout. NEVER a committing control — both underlying resolvers
    exclude place-order/pay, so ``advance`` cannot cross the commit boundary."""

    __zu_interface__ = 1
    name = "advance"
    kind = "advance"

    def __init__(
        self,
        cart_adder: WholeWordCartAdder | None = None,
        proceeder: WholeWordCheckoutProceeder | None = None,
    ) -> None:
        self._cart = cart_adder or WholeWordCartAdder()
        self._proceed = proceeder or WholeWordCheckoutProceeder()

    def inspect(self, view: SurfaceView, *, hint: str | None = None) -> PrimitivePlan:
        # Prefer add-to-cart on a product not yet in the cart; else the checkout advance.
        add = add_to_cart_handle(view)
        if add is not None and not in_cart(view):
            return PrimitivePlan(kind=self.kind, applicable=True, handles=(add,),
                                 detail="add to cart")
        adv = self._proceed.inspect(view).proceed_handle
        return PrimitivePlan(
            kind=self.kind, applicable=adv is not None,
            handles=(adv,) if adv else (), detail="proceed to checkout" if adv else "",
        )

    async def apply(
        self, surface: ConnectedSurface, *, hint: str | None = None
    ) -> PrimitiveOutcome:
        view = await surface.perceive()
        if add_to_cart_handle(view) is not None and not in_cart(view):
            took = await self._cart.add(surface)
            return PrimitiveOutcome(
                kind=self.kind, progress="advance" if took else "no_op",
                detail="added to cart" if took else "add did not take",
            )
        moved = await self._proceed.proceed(surface)
        return PrimitiveOutcome(
            kind=self.kind, progress="advance" if moved else "no_op",
            detail="advanced to checkout" if moved else "no forward control",
        )


# --- commit_stop ------------------------------------------------------------- #


class CommitStopPrimitive:
    """``commit_stop`` — the IRREVERSIBLE terminal. It recognises the structural commit
    boundary (a card/pay field is present — the payment step) and STOPS: ``apply`` NEVER
    acts, so the boundary is never crossed; it only reports ``commit_stop`` so the host
    hands off to human approval. The per-MOVE label guard (never CLICK a place-order/pay
    control) is :func:`~zu_tools._commerce.is_commit`, applied by the host to a chosen
    control; this primitive is the structural, page-shape half of the same boundary."""

    __zu_interface__ = 1
    name = "commit_stop"
    kind = "commit_stop"

    def inspect(self, view: SurfaceView, *, hint: str | None = None) -> PrimitivePlan:
        card = has_card_field(view)
        commit_ctrls = tuple(
            a.handle for a in view.affordances
            if a.role in CLICKABLE_ROLES and is_commit(a.label)
        )
        return PrimitivePlan(
            kind=self.kind, applicable=card, handles=commit_ctrls,
            detail="payment/commit boundary reached" if card else "",
        )

    async def apply(
        self, surface: ConnectedSurface, *, hint: str | None = None
    ) -> PrimitiveOutcome:
        view = await surface.perceive()
        at_boundary = has_card_field(view)
        # Deliberately does NOT act — the commit boundary is the human-approval line.
        return PrimitiveOutcome(
            kind=self.kind,
            progress="commit_stop" if at_boundary else "no_op",
            detail="reached the commit boundary — stop for approval" if at_boundary else "",
        )


# --- search ------------------------------------------------------------------ #

# The search-box vocabulary — the language-of-the-archetype, content-free (kept in step
# with zu_patterns' SEARCH_TOKENS; a lone role='searchbox' is the strongest signal).
_SEARCH_TOKENS: tuple[str, ...] = ("search", "find", "query", "look up", "lookup", "go")
_SEARCHBOX_ROLE = "searchbox"
_TEXTISH_ROLES: frozenset[str] = frozenset({"textbox", "combobox"})
_PASSWORDISH: frozenset[str] = frozenset({"password"})


def _find_search_box(view: SurfaceView) -> tuple[SurfaceAffordance, SurfaceAffordance | None] | None:
    """The search input (+ an optional submit control), or ``None``. A dedicated
    ``role=searchbox`` is the strong signal; a plain textbox/combobox labelled 'search'
    is the weaker one. A password field present means this is a login, not a search."""
    box: SurfaceAffordance | None = None
    for a in view.affordances:
        role = a.role.lower()
        if role in _PASSWORDISH or "password" in {_norm(s) for s in a.states}:
            return None  # a login form — not a search box
        if box is None and role == _SEARCHBOX_ROLE:
            box = a
    if box is None:
        for a in view.affordances:
            if a.role.lower() in _TEXTISH_ROLES and matches_whole_word(a.label, _SEARCH_TOKENS):
                box = a
                break
    if box is None:
        return None
    submit = next(
        (a for a in view.affordances
         if a.role.lower() == "button" and matches_whole_word(a.label, _SEARCH_TOKENS)),
        None,
    )
    return box, submit


def _norm(s: str) -> str:
    return s.split(":", 1)[0].lower()


class SearchPrimitive:
    """``search`` — type a query into the recognised search box and submit. Applicable
    when a search box is present; ``apply`` needs the query as its ``hint``. It clicks a
    submit control if the box has one, else issues a ``submit`` verb (an Enter keypress,
    for a search-on-enter box). Success invariant: the surface CHANGED (a results/listing
    surface appeared) — a no-op yields no change."""

    __zu_interface__ = 1
    name = "search"
    kind = "search"

    def inspect(self, view: SurfaceView, *, hint: str | None = None) -> PrimitivePlan:
        found = _find_search_box(view)
        if found is None:
            return PrimitivePlan(kind=self.kind, applicable=False, hint=hint)
        box, submit = found
        handles = tuple(h for h in (box.handle, submit.handle if submit else None) if h)
        return PrimitivePlan(kind=self.kind, applicable=True, handles=handles, hint=hint,
                             detail="search box")

    async def apply(
        self, surface: ConnectedSurface, *, hint: str | None = None
    ) -> PrimitiveOutcome:
        if not hint:
            return PrimitiveOutcome(kind=self.kind, progress="no_op", detail="no query")
        before = await surface.perceive()
        found = _find_search_box(before)
        if found is None:
            return PrimitiveOutcome(kind=self.kind, progress="no_op", detail="no search box")
        box, submit = found
        await surface.act(SurfaceAction(handle=box.handle, kind="type", text=hint))
        if submit is not None:
            after = await surface.act(SurfaceAction(handle=submit.handle, kind="click"))
        else:
            after = await surface.act(SurfaceAction(handle=box.handle, kind="submit"))
        changed = after.fingerprint() != before.fingerprint()
        return PrimitiveOutcome(
            kind=self.kind, progress="advance" if changed else "no_op",
            handles=(box.handle,), detail="results shown" if changed else "no change",
        )


# --- the composition runtime ------------------------------------------------- #

# The order a host tries the SELF-GATING primitives each turn: the commit-boundary GUARD
# first (stop before anything else if we are at the irreversible step), then dismiss an
# interstitial, satisfy a required choice, advance the funnel. search + a HINTED
# choose_one are model-directed, not free.
_FREE_ORDER: tuple[str, ...] = ("commit_stop", "dismiss", "choose_one", "advance")


class StandardPrimitiveRuntime:
    """The reference :class:`~zu_core.ports.PrimitiveRuntime` — the thin composition layer
    over the primitive family (#125). Holds one instance of each primitive keyed by
    ``kind``; ``free`` reports the applicable self-gating plans in priority order, ``step``
    runs one named primitive over the surface. Constructed with the five built-ins; a host
    may pass its own set."""

    __zu_interface__ = 1
    name = "standard_primitive_runtime"

    def __init__(self, primitives: tuple[InteractionPrimitive, ...] | None = None) -> None:
        prims = primitives or (
            DismissPrimitive(), SearchPrimitive(), ChooseOne(),
            AdvancePrimitive(), CommitStopPrimitive(),
        )
        self._by_kind: dict[str, InteractionPrimitive] = {p.kind: p for p in prims}

    def get(self, kind: str) -> InteractionPrimitive | None:
        return self._by_kind.get(kind)

    def free(self, view: SurfaceView) -> tuple[PrimitivePlan, ...]:
        plans: list[PrimitivePlan] = []
        for kind in _FREE_ORDER:
            prim = self._by_kind.get(kind)
            if prim is None:
                continue
            plan = prim.inspect(view)  # self-gating: no hint (choose_one -> variants)
            if plan.applicable:
                plans.append(plan)
        return tuple(plans)

    async def step(
        self, surface: ConnectedSurface, kind: str, *, hint: str | None = None
    ) -> PrimitiveOutcome:
        prim = self._by_kind.get(kind)
        if prim is None:
            return PrimitiveOutcome(kind=kind, progress="no_op", detail="unknown primitive")
        return await prim.apply(surface, hint=hint)
