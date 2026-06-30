"""Generic wildcard host matching — one helper, shared, no per-site constants.

A navigation allowlist (``allowed_domains``) is a list of host patterns like
``["*.example.com", "api.partner.com"]``. The matching rule must be identical
everywhere it is enforced — the pre-execution gate, the per-hop redirect check in
``check_url``, and the post-hoc ``DOMAIN_ALLOWLIST`` audit invariant — or the gate
and the audit can DRIFT (one allows what the other flags). So the match lives ONCE,
here, in stdlib-only zu-core, and every consumer calls it.

The grammar is deliberately small and case-insensitive:

  * ``example.com``      — an exact host (and, conveniently, never matches a
    sibling like ``notexample.com``).
  * ``*.example.com``    — any subdomain (``a.example.com``, ``a.b.example.com``).
    By convention this ALSO matches the apex ``example.com`` — declaring a wildcard
    for a domain is the operator saying "this domain", and forcing them to also
    list the bare apex is a footgun. (Set this behaviour off by omitting the apex
    case is not offered — keep the rule one-line and predictable.)
  * ``*``                — the open wildcard (any host). Explicit, never a default.

A pattern with no ``*`` is an exact match. A leading ``*.`` is the subdomain form.
Any other ``*`` placement (``a*.com``, ``*foo``) falls back to a plain fnmatch over
the host, so the helper never raises on an odd pattern — it just matches literally.
A trailing root dot on the host is stripped so ``example.com.`` == ``example.com``.
"""

from __future__ import annotations

import fnmatch

__all__ = ["host_matches", "host_matches_any", "normalize_host"]


def normalize_host(host: str) -> str:
    """Lower-case and strip a single trailing root dot, so ``Example.COM.`` and
    ``example.com`` compare equal. Defensive on a non-str / empty host."""
    h = (host or "").strip().lower()
    if h.endswith(".") and not h.endswith(".."):
        h = h[:-1]
    return h


def host_matches(host: str, pattern: str) -> bool:
    """True iff ``host`` matches the single allowlist ``pattern`` (see grammar)."""
    h = normalize_host(host)
    p = normalize_host(pattern)
    if not h or not p:
        return False
    if p == "*":
        return True
    if p.startswith("*."):
        suffix = p[2:]  # "example.com"
        # any subdomain, AND the apex itself (declaring *.example.com means
        # "this domain"), but never a sibling like "notexample.com".
        return h == suffix or h.endswith("." + suffix)
    if "*" in p:
        # an unusual placement (a*.com); literal fnmatch, never raises.
        return fnmatch.fnmatch(h, p)
    return h == p  # exact host


def host_matches_any(host: str, patterns: object) -> bool:
    """True iff ``host`` matches ANY pattern in ``patterns`` (a list/tuple/set of
    host globs). An empty/None allowlist matches NOTHING (default-deny) — the
    positive allowlist semantics: only listed hosts are permitted."""
    if not patterns or not isinstance(patterns, (list, tuple, set, frozenset)):
        return False
    return any(host_matches(host, str(p)) for p in patterns)
