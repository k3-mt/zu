"""zu_core.hosts — the ONE shared wildcard host matcher every allowlist consumer
uses (the pre-exec gate, the per-hop check_url, the post-hoc DOMAIN_ALLOWLIST audit),
so they cannot drift. Plus the wildcard mode of the domain-allowlist predicate.
"""

from __future__ import annotations

from uuid import uuid4

from zu_core.contracts import Event
from zu_core.hosts import host_matches, host_matches_any, normalize_host
from zu_core.invariants import Predicate, PredicateKind, predicate_holds


def test_exact_host() -> None:
    assert host_matches("api.example.com", "api.example.com")
    assert not host_matches("evil.example.com", "api.example.com")
    assert not host_matches("notexample.com", "example.com")


def test_wildcard_subdomain_and_apex() -> None:
    assert host_matches("a.example.com", "*.example.com")
    assert host_matches("a.b.example.com", "*.example.com")
    assert host_matches("example.com", "*.example.com")  # apex counts
    assert not host_matches("notexample.com", "*.example.com")  # sibling does not


def test_open_wildcard() -> None:
    assert host_matches("anything.test", "*")


def test_normalize_strips_trailing_dot_and_case() -> None:
    assert normalize_host("Example.COM.") == "example.com"
    assert host_matches("Example.COM.", "*.example.com")


def test_match_any_empty_is_default_deny() -> None:
    assert host_matches_any("a.example.com", []) is False
    assert host_matches_any("a.example.com", None) is False
    assert host_matches_any("a.example.com", ["*.example.com"]) is True


def _fetched(url: str) -> Event:
    return Event(trace_id=uuid4(), task_id=uuid4(), type="data.source.fetched",
                 source="t", payload={"url": url})


def test_domain_allowlist_wildcard_mode_over_urls() -> None:
    pred = Predicate(kind=PredicateKind.DOMAIN_ALLOWLIST,
                     params={"event_type": "data.source.fetched", "field": "url",
                             "allow": ["*.good.example"], "wildcard": True})
    assert predicate_holds(pred, [_fetched("https://a.good.example/x")]) is True
    assert predicate_holds(pred, [_fetched("https://evil.test/x")]) is False
    # a non-URL / hostless value is skipped (nothing to judge), not a violation
    assert predicate_holds(pred, [_fetched("not a url")]) is True


def test_domain_allowlist_exact_mode_unchanged() -> None:
    # the original (non-wildcard) mode still does literal set membership
    pred = Predicate(kind=PredicateKind.DOMAIN_ALLOWLIST,
                     params={"event_type": "data.source.fetched", "field": "url",
                             "allow": ["https://x.test/p"]})
    assert predicate_holds(pred, [_fetched("https://x.test/p")]) is True
    assert predicate_holds(pred, [_fetched("https://y.test/p")]) is False
