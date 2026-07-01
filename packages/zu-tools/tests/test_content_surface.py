"""content_surface — the parser-side producer of the reading projection.

The sibling test of ``test_action_surface``. Where that proves "what can I DO
here" (the content-free action view), this proves "what does the page SAY" — the
readable substance an agent reads ONLY on escalation. The reducer is pure and
non-executing, so the whole thing runs on a fixture HTML string + a captured
accessibility tree with no browser, no network, no model — $0 (Issue #41 §8,
"(A) extraction").

The load-bearing assertions:

* ``main_text`` is READABILITY-extracted article prose — NOT nav/header/footer
  chrome, NOT a raw ``<body>`` dump;
* ``tables``/``lists`` are populated from structural extraction;
* ``field_states`` REUSE the AX states (an empty required ``Last name`` field
  that is invalid and shows ``Required``);
* ``errors`` carry the toast/alert text;
* every ``provenance.region`` is a GENERIC descriptor — never a CSS/XPath
  selector (the handle_map discipline, Issue #41 §3, §11.3).
"""

from __future__ import annotations

import re

from zu_tools.action_surface import Affordance, AxNode, Surface
from zu_tools.content_adapter import to_content_view
from zu_tools.content_surface import reduce_content

# A fixture page: nav/header/footer chrome wrapping a <main> article with prose,
# a <table>, and a <ul>, plus a toast error region. The chrome text is distinctive
# ("Home"/"About"/"Acme Store Header"/"Copyright"/"Privacy policy") so the test can
# prove it never bleeds into the readability-extracted prose.
_FIXTURE_HTML = """
<html><body>
  <nav><a href="/">Home</a> <a href="/about">About</a></nav>
  <header>Acme Store Header</header>
  <main>
    <h1>Order Summary</h1>
    <p>Thank you for shopping with Acme. Your order is being prepared and will
       ship within two business days.</p>
    <p>Please review the details below and complete the remaining required fields.</p>
    <table>
      <tr><th>Item</th><th>Qty</th></tr>
      <tr><td>Widget</td><td>2</td></tr>
      <tr><td>Gadget</td><td>1</td></tr>
    </table>
    <ul>
      <li>Free returns within 30 days</li>
      <li>Carbon-neutral shipping</li>
    </ul>
  </main>
  <footer>Copyright Acme 2026. Privacy policy.</footer>
  <div class="toast" role="alert" aria-live="assertive">Please complete all required fields</div>
</body></html>
"""

# The chrome strings that orient/decorate but are never the article. If any of
# these appears in main_text, the readability strip failed (it dumped the body).
_CHROME_STRINGS = ("Home", "About", "Acme Store Header", "Copyright", "Privacy policy")

# A GENERIC region descriptor: a human-meaningful name like 'main', 'form#checkout',
# 'modal', 'toast', 'table:0' — never a CSS/XPath selector. A selector carries the
# punctuation '.' '#' '>' '[' '/'; the lone allowed '#' is the 'form#<id>' descriptor.
# We forbid the selector punctuation OTHER than that one descriptor shape.
_SELECTOR_PUNCT = re.compile(r"[.>\[\]/]")


def _checkout_field_tree() -> list[AxNode]:
    """The diagnostic slice as an accessibility tree: an EMPTY REQUIRED 'Last name'
    field that the tree reports invalid with its error text ('Required') in the AX
    description (the aria-describedby association), plus an alert node. The reducer
    REUSES these AX states exactly as ``action_surface.normalize_axtree`` produced
    them."""
    return [
        AxNode(role="textbox", name="First name", value="Ada", states=[]),
        AxNode(
            role="textbox",
            name="Last name",
            value=None,  # empty → the missing required field the escalation reads
            states=["required", "invalid"],
            description="Required",  # the field's own error text (aria-describedby)
        ),
        AxNode(role="button", name="Place order"),  # an action, never a field state
    ]


def _assert_generic_region(region: str) -> None:
    """Every region is a generic descriptor, never a selector."""
    assert region, "a unit must carry a region"
    if region.startswith("form#"):
        # 'form#<id>' is the allowed descriptor; the rest of it must be a plain id.
        assert not _SELECTOR_PUNCT.search(region)
        return
    assert not _SELECTOR_PUNCT.search(region), f"region looks like a selector: {region!r}"
    assert "#" not in region, f"unexpected '#' in region: {region!r}"


def _all_regions(view: object) -> list[str]:
    """Every provenance.region across every unit + field in the view."""
    v = view  # narrowed below; ContentView carries flat region tuples.
    regions: list[str] = []
    for region_tuple in (v.main_text, v.headings, v.tables, v.lists, v.kv, v.errors):  # type: ignore[attr-defined]
        regions.extend(u.provenance.region for u in region_tuple)
    regions.extend(f.provenance.region for f in v.field_states)  # type: ignore[attr-defined]
    return regions


def test_main_text_is_readability_prose_not_a_body_dump() -> None:
    view = reduce_content(_checkout_field_tree(), _FIXTURE_HTML, url="http://shop.test/checkout")
    prose = " ".join(u.text for u in view.main_text)
    # The article prose IS present — both paragraphs of the <main> body.
    assert "Thank you for shopping with Acme" in prose
    assert "complete the remaining required fields" in prose
    # The chrome is NOT present — nav/header/footer were stripped before extraction,
    # so this is readability-extracted, not a raw <body> dump.
    for chrome in _CHROME_STRINGS:
        assert chrome not in prose, f"chrome leaked into main_text: {chrome!r}"
    # It is also not a single dumped blob: prose is paragraph-granular (two <p>s).
    assert len(view.main_text) == 2


def test_tables_and_lists_are_populated() -> None:
    view = reduce_content(_checkout_field_tree(), _FIXTURE_HTML, url="http://shop.test/checkout")
    assert len(view.tables) == 1
    assert view.tables[0].rows == (("Item", "Qty"), ("Widget", "2"), ("Gadget", "1"))
    assert len(view.lists) == 1
    assert view.lists[0].rows == (("Free returns within 30 days",), ("Carbon-neutral shipping",))


def test_field_states_reuse_the_ax_states() -> None:
    view = reduce_content(_checkout_field_tree(), _FIXTURE_HTML, url="http://shop.test/checkout")
    by_label = {f.label: f for f in view.field_states}
    assert "Last name" in by_label
    last = by_label["Last name"]
    # An empty required field that is invalid and shows 'Required' — the exact
    # missing-required-field the escalation diagnoses (Issue #41 §8 "(A) extraction").
    assert last.value in ("", None)
    assert last.required is True
    assert last.invalid is True
    assert "Required" in last.error_text
    # The filled, valid field is reported too but carries no error.
    assert by_label["First name"].value == "Ada"
    assert by_label["First name"].required is False
    assert by_label["First name"].invalid is False
    # The button is an action, not a field — it never appears in field_states.
    assert "Place order" not in by_label


def test_errors_carry_the_toast_text() -> None:
    view = reduce_content(_checkout_field_tree(), _FIXTURE_HTML, url="http://shop.test/checkout")
    error_texts = [u.text for u in view.errors]
    assert any("Please complete all required fields" in t for t in error_texts)


def test_every_region_is_a_generic_descriptor_not_a_selector() -> None:
    view = reduce_content(_checkout_field_tree(), _FIXTURE_HTML, url="http://shop.test/checkout")
    regions = _all_regions(view)
    assert regions, "the fixture should produce regions to check"
    for region in regions:
        _assert_generic_region(region)
    # The genuine alert's region is the GENERIC 'alert' kind, never the element's
    # .toast/.alert class selector (the fixture toast carries role=alert, so it is a
    # real error routed to the 'alert' region — issue #73).
    assert any(r == "alert" for r in regions)
    # The table region is the generic index, never a selector.
    assert any(r == "table:0" for r in regions)


# --- issue #73: benign modals/toasts must NOT pollute the errors channel -----

# A page with a benign Quick View modal + a cookie/promo toast (NO error semantics)
# alongside a genuine assertive validation alert. The benign dialogs are containers,
# not verdicts; only the assertive alert is an actual error. Class names are generic
# structural hints ("...modal...", "...toast...") — never product/site strings.
_BENIGN_MODAL_HTML = """
<html><body>
  <main><h1>Dog collar</h1><p>A sturdy dog collar in three sizes.</p></main>
  <div class="quick-view-modal">
    <h2>Quick view</h2>
    <button aria-label="Close (esc)">Close</button>
  </div>
  <div class="cookie-toast">We use cookies. Accept?</div>
  <div class="promo-dialog">Subscribe to our newsletter</div>
  <div role="alert" aria-live="assertive">Please enter a valid postcode</div>
</body></html>
"""


def test_benign_modal_and_toast_do_not_land_in_errors() -> None:
    """A dismissible Quick View modal / cookie toast / promo dialog carries no error
    signal, so it must NOT appear in ``errors`` — a modal is a container, not a
    verdict (issue #73). Only the genuine assertive alert does."""
    view = reduce_content([], _BENIGN_MODAL_HTML, url="http://shop.test/collar")
    error_texts = " ".join(u.text for u in view.errors)
    # The benign dialogs' text is NOT in errors — no false "validation error".
    assert "Quick view" not in error_texts
    assert "Close" not in error_texts
    assert "cookies" not in error_texts
    assert "newsletter" not in error_texts
    # The genuine assertive alert IS an error.
    assert "Please enter a valid postcode" in error_texts


def test_a_genuine_error_signal_still_lands_in_errors() -> None:
    """The positive half of the contract: a ``role=alert``/``aria-live=assertive``
    region and an error-CLASSED region both carry a real error signal and DO enter
    ``errors`` (issue #73)."""
    html = """
    <html><body>
      <div role="alert">Payment failed</div>
      <div class="form-error">Card number is invalid</div>
      <div class="quick-view-modal">Quick view</div>
    </body></html>
    """
    view = reduce_content([], html, url="http://shop.test/pay")
    texts = " ".join(u.text for u in view.errors)
    assert "Payment failed" in texts
    assert "Card number is invalid" in texts
    assert "Quick view" not in texts


def test_alertdialog_is_an_error_but_plain_dialog_is_not() -> None:
    """An ``alertdialog`` IS an error dialog (e.g. 'Payment failed'); a plain content
    ``dialog``/``modal`` is benign (issue #73). Structure, not class substring,
    decides."""
    html = """
    <html><body>
      <div role="alertdialog">Your session expired</div>
      <div role="dialog" class="size-guide-modal">Size guide</div>
    </body></html>
    """
    view = reduce_content([], html, url="http://shop.test/x")
    texts = " ".join(u.text for u in view.errors)
    assert "Your session expired" in texts
    assert "Size guide" not in texts


def test_ax_alertdialog_status_ge_400_analog_is_still_an_error() -> None:
    """The AX side: an ``alert`` / ``alertdialog`` node carries the validation
    message and still routes into ``errors`` (issue #73 keeps genuine errors)."""
    from zu_tools.action_surface import AxNode

    nodes = [
        AxNode(role="alert", name="Out of stock"),
        AxNode(role="button", name="Add to basket"),
    ]
    view = reduce_content(nodes, "", url="http://shop.test/x")
    assert any("Out of stock" in u.text for u in view.errors)


def test_adapter_projects_a_surface_onto_the_same_view() -> None:
    """``to_content_view`` re-expresses a ``Surface``'s affordances as AX nodes and
    runs the SAME reducer, so the diagnostic slice comes out identically whether the
    caller has a raw AX tree or an already-reduced ``Surface``."""
    surface = Surface(
        title="Checkout",
        url="http://shop.test/checkout",
        affordances=[
            Affordance(handle="a1", role="textbox", label="Last name", value=None,
                       states=["required", "invalid"]),
            Affordance(handle="a2", role="button", label="Place order"),
        ],
    )
    view = to_content_view(surface, _FIXTURE_HTML)
    assert view.url == "http://shop.test/checkout"
    by_label = {f.label: f for f in view.field_states}
    assert by_label["Last name"].required is True
    assert by_label["Last name"].invalid is True
    # The HTML-side extraction still runs through the adapter (prose + table + list).
    assert view.main_text and view.tables and view.lists
    for region in _all_regions(view):
        _assert_generic_region(region)
