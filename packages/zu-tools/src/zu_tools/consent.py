"""consent — deterministic cookie/consent dismissal (#94).

Zu ships a ``CookieBanner`` *recognition* pattern, but recognising a banner is
only useful if there is a deterministic, content-free ACTION that CLEARS it —
and the pattern's ``matched_handles[0]`` is NOT the accept control (it is often a
"Manage preferences" button, which opens a sub-panel and never clears the
banner). :class:`WholeWordConsentResolver` is that action, as a
:class:`~zu_core.ports.ConsentResolver`:

  * ``find()`` picks the ACCEPT control by WHOLE-WORD accessible name. Whole-word
    matching is load-bearing: as a bare substring 'ok' matches inside 'Bespoke',
    'yes' inside 'eyes', 'allow' inside 'swallow' — a substring matcher clicks
    product links. Controls whose label reads 'Manage' / 'Settings' / 'Decline'
    are disqualified from being accept (clicking them leaves the banner up) but
    kept as a two-step ``open_panel`` opener.
  * ``dismiss()`` performs the full clear — open_panel → accept — delegating every
    click to the :class:`~zu_core.ports.ConnectedSurface`, which resolves handles
    ACROSS open shadow roots and child frames (CMPs render in cross-origin
    iframes). It returns whether the banner was actually cleared, so a host can
    latch 'handled' instead of re-detecting a persistent 'Manage consent' footer
    tab forever.

Content boundary: this is an English accept/reject wordlist — the smallest
possible domain vocabulary, the same category as the reducer's role lists, not
site-specific prose. A non-English CMP is the natural follow-up; the whole-word
discipline + two-step / boundary handling carry over unchanged.
"""

from __future__ import annotations

import re

from zu_core.ports import ConnectedSurface, ConsentControl, SurfaceAction
from zu_core.surface import SurfaceView

# Accept wording, matched as WHOLE words/phrases (never a bare substring).
_ACCEPT_PHRASES: tuple[str, ...] = (
    "accept", "accept all", "accept cookies", "allow", "allow all", "agree",
    "i agree", "agree all", "got it", "ok", "okay", "yes", "understood",
    "i accept", "accept and continue",
)
# A label containing any of these means "NOT the accept": manage/settings open a
# sub-panel; decline/reject/necessary clear nothing you want. Substring here is
# the SAFE direction — over-excluding only means we skip a control (worst case:
# find() returns None and the host escalates), never a wrong click.
_REJECT_MARKERS: tuple[str, ...] = (
    "manage", "preference", "setting", "option", "customi", "decline", "reject",
    "refuse", "necessary", "essential", "more", "choice", "learn",
)
# Two-step openers: a 'Manage consent' control that reveals an accept in a panel.
_PANEL_MARKERS: tuple[str, ...] = (
    "manage", "customi", "preference", "setting", "choice", "option", "consent",
)
# Roles a consent control actually takes — a button or link, never a textbox that
# happens to be labelled 'OK'.
_CLICKABLE_ROLES: frozenset[str] = frozenset({"button", "link", "menuitem"})


def _tokens(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens — the unit whole-word matching compares."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _contains_phrase(tokens: list[str], phrase: list[str]) -> bool:
    """Does ``phrase`` (a token list) occur as a contiguous run in ``tokens``? This
    is the whole-word test: 'ok' matches ['ok'] but not ['bespoke']."""
    n, m = len(tokens), len(phrase)
    if m == 0 or m > n:
        return False
    return any(tokens[i : i + m] == phrase for i in range(n - m + 1))


def _is_accept(label: str) -> bool:
    tokens = _tokens(label)
    return any(_contains_phrase(tokens, phrase.split()) for phrase in _ACCEPT_PHRASES)


def _has_marker(label: str, markers: tuple[str, ...]) -> bool:
    low = label.lower()
    return any(m in low for m in markers)


class WholeWordConsentResolver:
    """The reference :class:`~zu_core.ports.ConsentResolver`."""

    __zu_interface__ = 1  # the consent_resolvers interface major this targets
    name = "whole_word_consent_resolver"

    def find(self, view: SurfaceView) -> ConsentControl | None:
        panel: ConsentControl | None = None
        for a in view.affordances:
            if a.role not in _CLICKABLE_ROLES:
                continue
            if _has_marker(a.label, _REJECT_MARKERS):
                # Not the accept. Remember the first one that could OPEN a panel
                # (a two-step CMP), but keep scanning for a real accept, which wins.
                if panel is None and _has_marker(a.label, _PANEL_MARKERS):
                    panel = ConsentControl(handle=a.handle, kind="open_panel", label=a.label)
                continue
            if _is_accept(a.label):
                return ConsentControl(handle=a.handle, kind="accept", label=a.label)
        return panel

    async def dismiss(self, surface: ConnectedSurface) -> bool:
        view = await surface.perceive()
        ctrl = self.find(view)
        if ctrl is None:
            return False  # no banner to dismiss
        if ctrl.kind == "open_panel":
            # Two-step CMP: open the panel, then look for the accept it reveals.
            view = await surface.act(SurfaceAction(handle=ctrl.handle, kind="click"))
            ctrl = self.find(view)
            if ctrl is None or ctrl.kind != "accept":
                return False  # opened a panel but found no accept — give up, don't loop
        view = await surface.act(SurfaceAction(handle=ctrl.handle, kind="click"))
        after = self.find(view)
        # Cleared iff no ACCEPT control remains. A lingering 'Manage consent'
        # footer tab classifies as open_panel (not accept), so it reads as cleared
        # — the host latches 'handled' instead of chasing it forever.
        return after is None or after.kind != "accept"
