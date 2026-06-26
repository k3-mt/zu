"""content_surface — the parser-side producer of the reading projection.

This is the sibling of :func:`zu_tools.action_surface.reduce_surface`. Where that
reducer answers "what can I DO here" (the content-free action view), this one
answers "what does the page SAY" — the readable substance an agent reads ONLY on
escalation: the main article prose, tables, lists, key/value pairs, and the
diagnostic slice (validation errors + per-field states). It produces the CORE
:class:`zu_core.content_view.ContentView`; the readability/structural parsing
lives here in zu-tools (already deps ``selectolax``), never in zu-core (Issue #41
§1, §9.8).

:func:`reduce_content` is pure and non-executing — it walks an already-fetched
HTML string with selectolax and an already-captured accessibility tree; it never
runs a JS engine on hostile content (§9.5), and it caps the HTML at the same
5 MB ceiling :mod:`zu_tools.parse` uses so a huge hostile document cannot DoS the
parser in-process.

Three extraction strategies, one per kind of substance:

* **``main_text`` — a readability pass.** The main article prose is the
  ``<main>``/``<article>`` subtree (or, failing that, the densest block), with
  nav/header/footer/aside/script/style stripped FIRST — NOT a raw ``<body>`` dump
  and NOT ``obs['text']`` (Issue #41 §2.4). Pulling the whole body back would
  drown the signal exactly the way the action surface avoids dumping the DOM.
* **tables / lists / kv — structural extraction.** ``<table>`` rows, ``<ul>``/
  ``<ol>`` items, and ``<dl>`` definition pairs become the frozen ``rows`` carrier
  on a :class:`~zu_core.content_view.ContentUnit`.
* **field_states / errors — the diagnostic slice.** ``field_states`` REUSE the
  accessibility tree: the input-role nodes already carry ``required``/``invalid``
  in their states and the current ``value`` (``action_surface.normalize_axtree``),
  keyed by role + name/label, with the field's own error text read from its AX
  ``description`` (the aria-describedby association). ``errors`` come from
  ``role=alert`` / ``aria-live=assertive`` / toast / modal nodes — both AX alert
  nodes and the HTML toast/modal regions.

Every unit's ``region`` is a GENERIC descriptor (``'main'``, ``'form#checkout'``,
``'modal'``, ``'toast'``, ``'table:0'``) — NEVER a raw CSS/XPath selector, the
same handle_map discipline that keeps locators harness-side (Issue #41 §3,
§11.3). A regex test over the output guards it.
"""

from __future__ import annotations

from selectolax.parser import HTMLParser, Node

from zu_core.content_view import ContentUnit, ContentView, FieldState, Provenance

from .action_surface import AxNode

# Mirror ``zu_tools.parse._MAX_HTML_CHARS`` (5 MB): ``reduce_content`` is handed an
# ``html`` string whose origin is untrusted, and a huge hostile document is a CPU/
# memory DoS in-process. Above the cap the prose extraction is skipped (the AX-tree
# diagnostic slice still runs — it does not touch the raw HTML).
_MAX_HTML_CHARS = 5_000_000

# The roles whose nodes ARE form fields — the per-field diagnostic record is built
# from these. Same vocabulary as ``action_surface.INTERACTIVE_ROLES`` narrowed to
# the input-shaped roles (a button is an action, not a field that can be invalid).
_FIELD_ROLES: frozenset[str] = frozenset({
    "textbox", "searchbox", "combobox", "checkbox", "radio", "switch",
    "slider", "spinbutton", "listbox", "textarea", "datepicker",
})

# The accessibility roles whose text IS a page-level error/validation message.
_ALERT_ROLES: frozenset[str] = frozenset({"alert", "alertdialog"})

# The non-prose chrome stripped before the readability pass: nav/header/footer/
# aside orient or decorate but are never the article; script/style/noscript carry
# no readable substance. Stripping them is what makes ``main_text`` the prose and
# not a body dump.
_CHROME_TAGS: tuple[str, ...] = (
    "nav", "header", "footer", "aside", "script", "style", "noscript", "template",
)

# Generic region descriptors for the HTML-side error regions — toast/modal, NEVER
# the element's selector. Keyed by the structural pattern they match.
_TOAST_HINTS: tuple[str, ...] = ("toast", "snackbar", "notification", "flash")
_MODAL_HINTS: tuple[str, ...] = ("modal", "dialog")


def _label_of(node: AxNode) -> str:
    """The field's human label — accessible name, then placeholder, then
    description. Mirrors ``action_surface._label_of`` so the action and content
    views agree on what a field is called."""
    for candidate in (node.name, node.placeholder, node.description):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _main_subtree(tree: HTMLParser) -> Node | None:
    """The readability anchor: the ``<main>``/``<article>`` subtree if the page
    marks one, else the ``<body>`` (after chrome is stripped, the body is the
    article on a chrome-light page). Returns ``None`` when there is no body."""
    for selector in ("main", "article"):
        node = tree.css_first(selector)
        if node is not None:
            return node
    return tree.body


def _extract_main_text(html: str, *, url: str) -> tuple[ContentUnit, ...]:
    """The readability pass: strip nav/header/footer/aside/script/style, then take
    the prose paragraphs of the ``<main>``/``<article>`` (or body) subtree. NOT a
    raw body dump, NOT ``obs['text']`` — each paragraph is one unit so the prose
    stays attributable and the chrome never bleeds in."""
    if not html:
        return ()
    tree = HTMLParser(html)
    # Strip the non-prose chrome FIRST, so it cannot appear in the extracted prose
    # even when it sits inside the main/body subtree.
    for tag in _CHROME_TAGS:
        for node in tree.css(tag):
            node.decompose()
    root = _main_subtree(tree)
    if root is None:
        return ()
    region = "main" if tree.css_first("main") is not None else "article"
    units: list[ContentUnit] = []
    # Paragraph-granular prose: <p>, plus list-free headings stay in `headings`.
    for para in root.css("p"):
        text = para.text(strip=True)
        if text:
            units.append(
                ContentUnit.make(
                    "main_text", text=text, provenance=Provenance(url=url, region=region)
                )
            )
    # A page with prose but no <p> (rare) — fall back to the subtree's own text so
    # the reader is never handed an empty main_text on a page that clearly has prose.
    if not units:
        text = root.text(strip=True)
        if text:
            units.append(
                ContentUnit.make(
                    "main_text", text=text, provenance=Provenance(url=url, region=region)
                )
            )
    return tuple(units)


def _extract_headings(html: str, *, url: str) -> tuple[ContentUnit, ...]:
    """Headings orient — h1..h6 become heading units carrying their level."""
    if not html:
        return ()
    tree = HTMLParser(html)
    units: list[ContentUnit] = []
    for level in range(1, 7):
        for node in tree.css(f"h{level}"):
            text = node.text(strip=True)
            if text:
                units.append(
                    ContentUnit.make(
                        "heading",
                        text=text,
                        level=level,
                        provenance=Provenance(url=url, region="heading"),
                    )
                )
    return tuple(units)


def _extract_tables(html: str, *, url: str) -> tuple[ContentUnit, ...]:
    """Each ``<table>`` → one unit whose ``rows`` is a tuple of cell-tuples. The
    region is the generic ``'table:<i>'`` index, never the table's selector."""
    if not html:
        return ()
    tree = HTMLParser(html)
    units: list[ContentUnit] = []
    for i, table in enumerate(tree.css("table")):
        rows: list[tuple[str, ...]] = []
        for tr in table.css("tr"):
            cells = tuple(c.text(strip=True) for c in tr.css("th,td"))
            if cells:
                rows.append(cells)
        if rows:
            units.append(
                ContentUnit.make(
                    "table",
                    rows=tuple(rows),
                    provenance=Provenance(url=url, region=f"table:{i}"),
                )
            )
    return tuple(units)


def _extract_lists(html: str, *, url: str) -> tuple[ContentUnit, ...]:
    """Each ``<ul>``/``<ol>`` → one unit whose ``rows`` is a one-cell row per item.
    The region is the generic ``'list:<i>'`` index."""
    if not html:
        return ()
    tree = HTMLParser(html)
    units: list[ContentUnit] = []
    for i, lst in enumerate(tree.css("ul,ol")):
        items: list[tuple[str, ...]] = []
        for li in lst.css("li"):
            text = li.text(strip=True)
            if text:
                items.append((text,))
        if items:
            units.append(
                ContentUnit.make(
                    "list",
                    rows=tuple(items),
                    provenance=Provenance(url=url, region=f"list:{i}"),
                )
            )
    return tuple(units)


def _extract_kv(html: str, *, url: str) -> tuple[ContentUnit, ...]:
    """Each ``<dl>`` → one unit whose ``rows`` pairs ``<dt>`` term with ``<dd>``
    value. Definition lists are the page's structured key/value facts."""
    if not html:
        return ()
    tree = HTMLParser(html)
    units: list[ContentUnit] = []
    for i, dl in enumerate(tree.css("dl")):
        terms = [d.text(strip=True) for d in dl.css("dt")]
        defs = [d.text(strip=True) for d in dl.css("dd")]
        pairs: list[tuple[str, ...]] = []
        for term, value in zip(terms, defs, strict=False):
            if term or value:
                pairs.append((term, value))
        if pairs:
            units.append(
                ContentUnit.make(
                    "kv",
                    rows=tuple(pairs),
                    provenance=Provenance(url=url, region=f"kv:{i}"),
                )
            )
    return tuple(units)


def _extract_html_errors(html: str, *, url: str) -> list[ContentUnit]:
    """Toast/modal error regions from the HTML: an ``aria-live="assertive"`` or
    ``role="alert"`` region, or an element whose class/role names a toast/modal.
    The region is the GENERIC kind (``'toast'``/``'modal'``), never the selector."""
    if not html:
        return []
    tree = HTMLParser(html)
    units: list[ContentUnit] = []
    seen: set[str] = set()

    def _add(node: Node, kind: str, region: str) -> None:
        text = node.text(strip=True)
        if text and text not in seen:
            seen.add(text)
            units.append(
                ContentUnit.make(kind, text=text, provenance=Provenance(url=url, region=region))
            )

    # role=alert / aria-live=assertive — the ARIA live-region error channel.
    for node in tree.css('[role="alert"], [aria-live="assertive"], [role="alertdialog"]'):
        attrs = node.attributes
        cls = (attrs.get("class") or "") if attrs else ""
        if any(h in cls for h in _MODAL_HINTS) or "dialog" in (node.tag or ""):
            _add(node, "modal", "modal")
        elif any(h in cls for h in _TOAST_HINTS):
            _add(node, "toast", "toast")
        else:
            _add(node, "error", "alert")
    # class-named toast/modal regions that did not also carry an ARIA role.
    for node in tree.css("[class]"):
        attrs = node.attributes
        cls = (attrs.get("class") or "") if attrs else ""
        if any(h in cls for h in _TOAST_HINTS):
            _add(node, "toast", "toast")
        elif any(h in cls for h in _MODAL_HINTS):
            _add(node, "modal", "modal")
    return units


def _extract_ax_errors(nodes: list[AxNode], *, url: str) -> list[ContentUnit]:
    """Page-level errors from the accessibility tree: ``role=alert`` /
    ``alertdialog`` nodes whose name is the validation/error message."""
    units: list[ContentUnit] = []
    for node in nodes:
        if node.role in _ALERT_ROLES:
            text = (node.name or "").strip()
            if text:
                kind = "modal" if node.role == "alertdialog" else "error"
                region = "modal" if kind == "modal" else "alert"
                units.append(
                    ContentUnit.make(kind, text=text, provenance=Provenance(url=url, region=region))
                )
    return units


def _extract_field_states(
    nodes: list[AxNode], *, url: str, form_region: str
) -> tuple[FieldState, ...]:
    """Per-field diagnostic records from the accessibility tree (Issue #41 §2.4).

    REUSE the AX states ``required``/``invalid`` and the current ``value`` already
    on the input-role nodes (``action_surface.normalize_axtree``), keyed by
    role + name/label. The field's error text is read from its AX ``description``
    — the aria-describedby association, the standard way a field points at its
    error message — so an empty required field that shows "Required" carries that
    as its ``error_text``."""
    fields: list[FieldState] = []
    for node in nodes:
        if node.role not in _FIELD_ROLES:
            continue
        label = _label_of(node)
        if not label:
            continue
        states = set(node.states)
        # ``value`` distinguishes empty ("") from unknown (None): a field the tree
        # reports with no value is empty, which is exactly the "missing required
        # field" the escalation reads.
        value = node.value if node.value is not None else ""
        # The error text is the field's AX description (aria-describedby) when it
        # differs from the label/placeholder — that is the associated error region.
        error_text = ""
        desc = (node.description or "").strip()
        if desc and desc not in (label, (node.placeholder or "").strip()):
            error_text = desc
        fields.append(
            FieldState(
                label=label,
                value=value,
                required="required" in states,
                invalid="invalid" in states,
                error_text=error_text,
                provenance=Provenance(url=url, region=form_region),
            )
        )
    return tuple(fields)


def reduce_content(
    nodes: list[AxNode], html: str = "", *, url: str = "", title: str = ""
) -> ContentView:
    """Reduce HTML + an accessibility tree to the reading projection — pure,
    non-executing (Issue #41 §2.4).

    ``main_text`` comes from a selectolax readability pass over ``html`` (NOT
    ``obs['text']``, NOT a raw body dump); ``tables``/``lists``/``kv`` from
    selectolax structural extraction; ``field_states`` REUSE the AX-tree
    normalization (``required``/``invalid``/``value`` already in the AX states),
    keyed by role + name/label; ``errors`` from ``role=alert`` / aria-live
    assertive / toast / modal nodes (both AX and HTML). ``html`` is capped at the
    same 5 MB ceiling :mod:`zu_tools.parse` uses; above it the prose/structural
    extraction is skipped and only the AX-side diagnostic slice is built.

    The generic form region (``'form#<id>'`` or ``'form'``) is derived from the
    HTML form, never as a selector. Returns the CORE
    :class:`~zu_core.content_view.ContentView`.
    """
    if len(html) > _MAX_HTML_CHARS:
        html = ""  # over the cap: never parse a hostile oversized document.

    form_region = _form_region(html)

    errors: list[ContentUnit] = []
    errors.extend(_extract_ax_errors(nodes, url=url))
    errors.extend(_extract_html_errors(html, url=url))

    return ContentView(
        url=url,
        main_text=_extract_main_text(html, url=url),
        headings=_extract_headings(html, url=url),
        tables=_extract_tables(html, url=url),
        lists=_extract_lists(html, url=url),
        kv=_extract_kv(html, url=url),
        errors=tuple(errors),
        field_states=_extract_field_states(nodes, url=url, form_region=form_region),
    )


def _form_region(html: str) -> str:
    """The generic form descriptor: ``'form#<id>'`` when the page's form has an id,
    else ``'form'``. NEVER a selector — the id is a stable human-meaningful name,
    the same as ``surface.py``'s region examples (``'form#checkout'``)."""
    if not html:
        return "form"
    tree = HTMLParser(html)
    form = tree.css_first("form")
    if form is None:
        return "form"
    attrs = form.attributes
    form_id = (attrs.get("id") or "") if attrs else ""
    return f"form#{form_id}" if form_id else "form"
