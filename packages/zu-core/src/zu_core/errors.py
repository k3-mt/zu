"""Neutral provider-error taxonomy — the error surface of the ModelProvider port.

A ``ModelProvider`` adapter wraps a vendor SDK (anthropic, openai, …). Without a
neutral error family, a raw vendor exception (``anthropic.RateLimitError``,
``openai.AuthenticationError``, …) would cross the port and re-couple the loop —
and every caller — to the SDK at the error path, eroding the provider-neutral
seam the port is meant to provide.

These types are that neutral surface. Each adapter catches its SDK exceptions at
the port boundary (its ``complete``) and re-raises the matching neutral type
``from exc`` — so the *types* live here (zu-core, no SDK import) while the
*translation* (which references vendor exception classes) lives in each adapter.
A consumer that wants to behave differently per failure class — fail fast on a
fatal auth error, surface a clean operator message on persistent rate-limiting,
distinguish a connection failure from a model error — ``isinstance``-checks these
neutral classes, never a vendor one.

Backoff/retry policy remains the caller's choice on top of these classes; the
neutral taxonomy is the generic primitive, not a policy.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base for any failure surfaced by a :class:`ModelProvider` adapter.

    An adapter raises a subclass when it can classify the underlying SDK
    exception; otherwise it wraps the unknown cause in this base, preserving the
    original via ``raise ... from exc``. Catching ``ProviderError`` catches every
    provider failure regardless of vendor.
    """


class ProviderAuthError(ProviderError):
    """Authentication / authorization failure (bad or missing key, forbidden).

    Fatal for the run as configured — no amount of retry helps until the
    credential is fixed."""


class ProviderRateLimited(ProviderError):
    """The provider throttled the request (HTTP 429 / quota exhausted).

    Transient: the caller may back off and retry, or surface a clean operator
    message on persistent throttling."""


class ProviderTimeout(ProviderError):
    """The request exceeded the provider's wall-time (connect/read deadline).

    Distinct from the loop's own cooperative wall-time deadline — this is the
    SDK's timeout crossing the port as a neutral type."""


class ProviderUnavailable(ProviderError):
    """The provider could not be reached or served the request (connection
    failure, 5xx, overloaded). Typically transient and worth a retry."""
