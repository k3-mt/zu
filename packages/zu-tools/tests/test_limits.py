"""The document-size cap is single-sourced (issue #65 O12).

``http_fetch`` (bytes) and ``html_parse`` (chars) share ONE defensive cap. Before
the fix each module carried its own ``5_000_000`` literal, free to drift; now both
derive from ``zu_tools.limits.MAX_DOCUMENT_BYTES``. These tests fail on the old
code by asserting object identity to the shared constant (a duplicated literal is
an equal-but-not-``is`` int for large values, and more importantly a change to the
shared constant must move both).
"""

from __future__ import annotations

from zu_tools import fetch, limits, parse


def test_fetch_and_parse_share_the_single_constant() -> None:
    # Both modules reference the ONE shared constant, not an independent literal.
    assert fetch._DEFAULT_MAX_BYTES is limits.MAX_DOCUMENT_BYTES
    assert parse._MAX_HTML_CHARS is limits.MAX_DOCUMENT_BYTES


def test_single_source_value() -> None:
    # Backstop: both consumers agree on the 5 MB bound via the shared source.
    assert fetch._DEFAULT_MAX_BYTES == parse._MAX_HTML_CHARS == 5_000_000
