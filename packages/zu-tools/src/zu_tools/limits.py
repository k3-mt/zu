"""Shared numeric limits for the built-in tools — single-sourced (issue #65 O12).

The body/document size cap is a *defensive* bound shared by more than one tool:
``http_fetch`` caps the (decompressed) bytes it reads off an untrusted page, and
``html_parse`` caps the HTML handed to the in-process parser. They must agree —
a parser cap looser than the fetch cap would let a document ``http_fetch`` would
have refused slip through the standalone parse path (and vice versa). Rather than
duplicate the literal in each module (where the two could silently drift), define
it ONCE here and import it in both.
"""

from __future__ import annotations

# Default cap on a single fetched/parsed document. Untrusted pages can be
# arbitrarily large, and an HTTP client transparently decompresses, so a small
# gzip can expand to gigabytes — cap what we read/parse, not just the bytes on
# the wire. Interpreted as bytes by ``http_fetch`` and as characters by
# ``html_parse``; the numeric bound is the same 5 MB either way.
MAX_DOCUMENT_BYTES = 5_000_000
