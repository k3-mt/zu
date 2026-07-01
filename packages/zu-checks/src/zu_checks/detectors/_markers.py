"""Shared anti-bot / captcha marker sets — the ONE source of truth for the
deterministic wall signal, owned by NEITHER detector.

``bot-wall`` (climbs the tooling tier) and ``captcha`` (routes to a human) share
the exact same detection signal but differ only in DESTINATION. Keeping the
marker sets here — a neutral module both import — means neither detector depends
on the other: the signal is a shared primitive, not a hidden coupling where one
detector reaches into the other's internals.

Marker semantics:

  * ``STRONG_MARKERS`` — phrasing characteristic of an anti-bot interstitial,
    specific enough that their presence is treated as the signal on its own. This
    is a deterministic heuristic, not a proof: a page that *discusses* CAPTCHAs (a
    news story, this very comment) can contain "captcha" and would escalate — the
    cost is a wasted tier-2 render (or a spurious human route), not a wrong
    answer, and escalating a borderline page is the safer failure.
    ``cf-browser-verification`` is unambiguous; the natural-language phrases are
    the ones with residual false-positive surface.
  * ``WEAK_MARKERS`` — real Cloudflare wall phrasing, but common-enough English
    that a substring match alone false-positives (an article titled "Just a
    Moment in History", a banner reading "Attention required"). They fire ONLY
    when a Cloudflare fingerprint is also present, so a normal page is never
    escalated.
  * ``CLOUDFLARE_FINGERPRINTS`` — the corroborating signal that gates the weak
    markers.
"""

from __future__ import annotations

STRONG_MARKERS = (
    "captcha",
    "are you a robot",
    "verify you are human",
    "cf-browser-verification",
)

WEAK_MARKERS = (
    "attention required",
    "just a moment",
)

CLOUDFLARE_FINGERPRINTS = (
    "cloudflare",
    "cf-ray",
    "cf-browser-verification",
    "__cf",
    "/cdn-cgi/",
)
