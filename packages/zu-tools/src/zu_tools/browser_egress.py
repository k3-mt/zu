"""Egress scoping for the browser tools (render_dom / browser / action_surface).

The tier-2/3 browser tools render attacker-influenced pages in a headless
browser. Tier-1's ``validate_and_pin`` checks (and DNS-pins) only the *initial*
URL's host — it never sees the in-page ``fetch``/XHR, redirects, or subresource
loads Chromium issues once the page runs. Launched with bare ``network: True``
the container has unrestricted egress, so a hostile page can reach cloud metadata
(169.254.169.254), an RFC1918 internal service, or any other host with no SSRF
validation at all (issue #54).

This module supplies the two GENERIC, site-agnostic pieces of the fix that live
in the tools (the actual blocking enforcement lives in a firewall-capable
``SandboxBackend`` + the ``LocalEgressProxy`` in ``zu-backends``):

1. :func:`browser_egress_spec` — build the launch spec's egress fields. It ALWAYS
   carries the validated target set as ``allowlist`` (so a firewall-capable
   backend can scope the container's egress to exactly those hosts), and when an
   egress proxy is provisioned for the run it switches the container OFF bare
   ``network: True`` onto the internal ``network: "isolated"`` + ``proxy`` path —
   the backend's existing default-DROP mode whose only route off-box is the
   allowlist-checking proxy. No new backend primitive; the call sites just stop
   passing the unscoped form.

2. :func:`subresource_allowed` — the pure allowlist DECISION used by the proxy:
   an in-browser request to a host outside the validated target set (or to any
   internal/metadata address, ever) is refused. Pure and offline-testable;
   mirrors ``LocalEgressProxy._allowed`` so the declared envelope and the actual
   reach agree.

No per-site constants: the allowlist is the run's own validated target host(s).
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from zu_core.ports import EGRESS_OPEN
from zu_core.security import SANDBOX_ENV

from .net import _ip_blocked_reason  # the SSRF address classifier (loopback/RFC1918/metadata/…)

# The internal docker network the contained run attaches its sandboxes to. The
# contained launcher (zu_cli.sandbox) exports this alongside ``HTTP(S)_PROXY`` and
# ``ZU_SANDBOXED`` so a nested browser sandbox joins the SAME default-DROP network
# whose only route off-box is the allowlist-checking egress proxy.
SANDBOX_NETWORK_ENV = "ZU_SANDBOX_NETWORK"


def contained_egress_config() -> tuple[dict[str, Any] | None, str | None]:
    """Resolve ``(proxy, network_name)`` from the environment when the tool runs
    inside the Zu sandbox, mirroring ``HttpFetch._contained()``.

    A contained run sets ``ZU_SANDBOXED=1``, points ``HTTP(S)_PROXY`` at the egress
    proxy (``http://host:port``), and exports ``ZU_SANDBOX_NETWORK`` (the internal
    network name). From those a nested browser sandbox derives the isolated+proxy
    launch config, so its in-browser egress routes through the SAME allowlist proxy
    — no bare ``network: True``. Returns ``(None, None)`` uncontained (the host /
    test path), where an explicit constructor config governs instead."""
    if not os.environ.get(SANDBOX_ENV):
        return None, None
    proxy_url = (
        os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    )
    network_name = os.environ.get(SANDBOX_NETWORK_ENV)
    if not proxy_url or not network_name:
        return None, None
    parts = urlsplit(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    host, port = parts.hostname, parts.port
    if not host or not port:
        return None, None
    return {"host": host, "port": port}, network_name


def egress_caveat(egress: frozenset[str] | set[str]) -> str:
    """A one-line, model-facing caveat DERIVED from the tool's ``egress`` capability
    set (not a hardcoded string) — so the model-visible seam (prompt_fragment /
    schema description) reflects the real envelope. ``EGRESS_OPEN`` means rendering
    a page permits unvalidated in-browser egress; a scoped set names the hosts."""
    if EGRESS_OPEN in egress:
        return (
            "Note: rendering a page runs untrusted JS in a real browser, which can "
            "fetch subresources/redirects to ANY host (open egress) — only the "
            "initial URL is SSRF-validated."
        )
    hosts = ", ".join(sorted(egress))
    return f"Note: this tool's in-browser egress is scoped to {hosts}."


def _host_is_internal(host: str) -> bool:
    """True if ``host`` is an internal/metadata IP literal that must never be
    reachable regardless of the allowlist. We only classify IP LITERALS here (a
    name is resolved-and-checked at dial time by the proxy / ``validate_and_pin``);
    a literal like ``169.254.169.254`` or ``10.0.0.5`` is refused outright."""
    if not host:
        return True
    try:
        return _ip_blocked_reason(host) is not None
    except ValueError:
        # Not an IP literal — a hostname; the allowlist decides, resolution is the
        # proxy's job. Not internal *as a literal*.
        return False


def subresource_allowed(
    host: str | None, allowlist: frozenset[str] | set[str], *, block_internal: bool = True
) -> bool:
    """Decide whether an in-browser subresource/redirect to ``host`` is permitted.

    The pure decision the egress proxy enforces for the browser path:

      * an internal/metadata IP literal is refused outright (``block_internal``),
        even if somehow allowlisted — the cloud-metadata/RFC1918 SSRF target the
        tier-1 guard exists to block;
      * ``EGRESS_OPEN`` (``"*"``) in the allowlist permits any (the honest
        open-web declaration), otherwise only an exact host match is allowed.

    ``block_internal=False`` is for loopback tests only (a fake upstream)."""
    if not host:
        return False
    if block_internal and _host_is_internal(host):
        return False
    if EGRESS_OPEN in allowlist:
        return True
    return host in allowlist


def browser_egress_spec(
    target_hosts: frozenset[str] | set[str] | list[str],
    *,
    proxy: dict[str, Any] | None = None,
    network_name: str | None = None,
    dns: Any = None,
) -> dict[str, Any]:
    """Build the launch-spec egress fields for a browser tool.

    ``target_hosts`` is the run's validated target set (the host(s) the tool was
    asked to open). The returned dict ALWAYS carries ``allowlist`` so a
    firewall-capable backend can scope the container's egress to exactly those
    hosts — closing the gap that bare ``network: True`` left open even when no
    proxy is wired.

    When an egress ``proxy`` (``{host, port}``) is provisioned for the run, the
    container is launched on the internal, default-DROP network
    (``network: "isolated"`` + ``network_name``) whose ONLY route off-box is the
    allowlist-checking proxy — so in-browser subresources/redirects to hosts
    outside ``allowlist`` are refused, not merely declared. Absent a proxy (the
    out-of-the-box default until the run provisions one) it falls back to
    ``network: True`` but STILL carries the allowlist, so the tool never launches
    the *unscoped* form again.

    The no-proxy fallback is unrestricted egress, so it carries the explicit
    ``allow_unrestricted_egress`` opt-in the backend requires (F38): the choice is
    a deliberate, logged fallback rather than an accidental default, and callers
    who want it enforced should provision the proxy for the ``isolated`` path.
    """
    allowlist = sorted(set(target_hosts))
    spec: dict[str, Any] = {"allowlist": allowlist}
    if proxy and network_name:
        spec["network"] = "isolated"
        spec["network_name"] = network_name
        spec["proxy"] = dict(proxy)
        if dns is not None:
            spec["dns"] = dns
    else:
        # No proxy provisioned: the honest declaration is still open egress, but
        # the allowlist is attached for a firewall-capable backend to enforce.
        # Unrestricted egress is opted into explicitly (F38) so the backend permits
        # it rather than refusing an accidental unscoped launch.
        spec["network"] = True
        spec["allow_unrestricted_egress"] = True
    return spec
