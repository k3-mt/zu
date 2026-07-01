"""render_dom — the tier-2 browser tool.

The escalation target when a JavaScript page defeats the tier-1 ``http_fetch``:
it renders the URL in a real headless browser (JS executed) and returns the
resulting DOM. The browser does not run in this process — it runs inside a
sandbox obtained through the :class:`SandboxBackend` port, so the live,
unpredictable part (a real browser) is isolated behind a seam the tests can
freeze, exactly as ``http_fetch`` isolates the network behind an httpx
transport.

By default the sandbox is the local-docker backend, so an installed Zu renders
out of the box. A test (or build step 8 config) injects a different backend —
including a scripted one that returns a saved rendered page, which is how the
escalation ladder is proven offline.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from zu_core.ports import CAP_NET, CAP_SANDBOX, EGRESS_OPEN, SandboxBackend, ToolCall

from .action_schema import validate_actions
from .browser_egress import browser_egress_spec, contained_egress_config, egress_caveat
from .net import validate_and_pin

# Default browser viewport. Chromium otherwise falls back to Playwright's
# implicit 1280x720; we set it explicitly so the rendered DOM is reproducible
# and a caller can override it (responsive pages branch on width).
_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 720

# The sandbox image is a spec detail, not a hard-coded constant in the loop:
# the backend launches it. The published Playwright/Chromium image is the default
# tier-2 environment (built from images/render-chromium in this repo); swap it via
# RenderDom(image=...) or config without touching the loop.
_DEFAULT_IMAGE = "ghcr.io/k3-mt/zu-render-chromium:latest"


class RenderDom:
    name = "render_dom"
    tier = 2  # unlocked only after a detector escalates off tier 1
    schema = {
        "name": "render_dom",
        "description": (
            "Render a URL in a headless browser and return the DOM after JS executes. "
            "Can wait for late-injected widget content and perform read-surfacing "
            "clicks/selects (expand, load-more, tabs, filters) before capturing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "width": {"type": "integer", "description": "viewport width in px (optional)"},
                "height": {"type": "integer", "description": "viewport height in px (optional)"},
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                    "description": "when to consider navigation done; use 'networkidle' "
                                   "for a widget that loads content after load (optional)",
                },
                "wait_for": {
                    "type": "string",
                    "description": "a CSS selector to wait for before capturing (optional)",
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "extra settle time in ms after load/actions (optional)",
                },
                "actions": {
                    "type": "array",
                    "description": (
                        "read-surfacing actions run IN ORDER before capture, to drive a "
                        "multi-step widget you reason through step by step. Each is "
                        "{click|fill|select|wait_for: <selector>, value?: <str>} or {wait_ms:<n>}. "
                        "A selector is a CSS selector OR a Playwright text selector "
                        "(text=\"I am a new client\") — target what you SEE in the rendered "
                        "page. Each render is a fresh browser, so include the FULL sequence "
                        "from the start each call; read the returned DOM, then append the next "
                        "action. Example: [{\"click\":\"text=Next\"},{\"click\":\"text=Dog\"},"
                        "{\"wait_for\":\"text=Choose a time\"}]."
                    ),
                    "items": {"type": "object"},
                },
                "capture_network": {
                    "type": "boolean",
                    "description": "also capture the page's XHR/JSON responses (the data a "
                                   "widget fetches — e.g. availability) into `network` + content. "
                                   "More robust than scraping a reactive grid.",
                },
            },
            "required": ["url"],
        },
    }
    # Provisions a sandbox (CAP_SANDBOX) and renders a model-chosen URL inside
    # it, so it declares open egress (the browser must reach the page). The
    # sandbox is where that egress is *enforced* and where scoping it to the
    # target is the deferred egress-policy work — but the declaration lives here.
    capabilities = frozenset({CAP_NET, CAP_SANDBOX})
    egress = frozenset({EGRESS_OPEN})
    # The model-facing prompt now DISCLOSES the egress posture, derived from the
    # ``egress`` capability set (issue #54) — not a hardcoded literal — so the model
    # knows rendering a page permits unvalidated in-browser egress.
    prompt_fragment = (
        "render_dom(url, [wait_until|wait_for|wait_ms|actions]): render a page in a "
        "real browser (JS executed). Use when a page needs JavaScript; pass wait_for/"
        "wait_until or read-surfacing actions (click/select to expand, load-more, pick "
        "a tab) when content loads after the page or behind a control. "
        + egress_caveat(egress)
    )

    def __init__(
        self,
        backend: SandboxBackend | None = None,
        image: str = _DEFAULT_IMAGE,
        *,
        allow_private: bool | None = None,
        proxy: dict | None = None,
        network_name: str | None = None,
        egress_dns: object | None = None,
        allowed_domains: list[str] | None = None,
    ) -> None:
        # The per-agent positive navigation allowlist (issue #74): None ⇒ unset.
        # Enforced on the target host via validate_and_pin, alongside the pre-exec gate.
        self.allowed_domains = allowed_domains
        # backend None -> the local-docker default, imported lazily so this
        # module (and tier-1-only deployments) need not pull in the backend
        # package or a Docker client until a render is actually attempted.
        self._backend = backend
        self.image = image
        # Mirrors HttpFetch: None consults ZU_HTTP_ALLOW_PRIVATE; True skips the
        # SSRF DNS check for local dev (and lets tests use a non-resolvable host).
        self.allow_private = allow_private
        # Egress-enforcement wiring (issue #54): when the run provisions an egress
        # proxy ({host, port}) on an internal docker network (network_name), the
        # render launches in the isolated, default-DROP mode whose only route
        # off-box is the allowlist-checking proxy — so in-browser subresources to
        # hosts outside the validated target set are refused, not just declared.
        self._proxy = proxy
        self._network_name = network_name
        self._egress_dns = egress_dns

    def _resolve_backend(self) -> SandboxBackend:
        if self._backend is None:
            from zu_backends.local_docker import LocalDockerBackend

            self._backend = LocalDockerBackend()
        return self._backend

    def _egress_config(self) -> tuple[dict | None, str | None]:
        """The (proxy, network_name) governing this launch: an explicit constructor
        config wins; otherwise the contained run's env-derived proxy is used, so the
        production-contained path emits the isolated+proxy spec, not bare network."""
        if self._proxy is not None and self._network_name is not None:
            return self._proxy, self._network_name
        return contained_egress_config()

    async def __call__(
        self, ctx: Any, url: str, width: int | None = None, height: int | None = None,
        wait_until: str | None = None, wait_for: str | None = None,
        wait_ms: int | None = None, actions: list | None = None,
        capture_network: bool = False,
    ) -> dict:
        # Validate the model-supplied action args at the tool boundary BEFORE any
        # sandbox is leased (issue #65 F52): each action must be a well-formed
        # {click|fill|select|wait_for: <selector>, value?, near?} | {wait_ms:<n>}
        # — allowed op, selector/value typed, no stray fields. A malformed action
        # is refused here (a clear error the model can act on), never forwarded.
        if actions:
            action_err = validate_actions(actions)
            if action_err is not None:
                return {"error": action_err, "blocked": "invalid_action"}
        # Apply the same host-level SSRF backstop tier-1 http_fetch uses, *before*
        # leasing a browser: escalating to tier 2 must not become a way to fetch
        # an internal address (cloud metadata, loopback, RFC1918) with the guard
        # bypassed. ``validate_and_pin`` does the scheme/host check, the SSRF
        # validation, AND returns the pinned IP from a SINGLE host resolution —
        # so the target's DNS pin below uses the exact address that was validated,
        # closing the double-resolve TOCTOU that a separate check_url + pin_ip
        # opened. Raises BlockedURLError (a SecurityBlock) → the loop records a
        # harness.defense.blocked event and surfaces it as an error observation.
        #
        # Scope (issue #54): the per-target rebind backstop above is identical in
        # strength to tier 1, but the launch spec now ALSO carries the validated
        # target host as the egress ``allowlist`` (and, when a proxy is provisioned,
        # routes the container through the isolated default-DROP network) — so the
        # sandbox's in-browser egress is scoped to the target, not bare/open. Absent
        # a provisioned proxy the declaration stays the honest EGRESS_OPEN, but the
        # allowlist is attached for a firewall-capable backend to enforce.
        pinned_ip = validate_and_pin(
            url, allow_private=self.allow_private, allowed_domains=self.allowed_domains
        )
        backend = self._resolve_backend()
        # Lease a sandbox for the render and always tear it down — a browser
        # container is expensive and must not leak even if the render raises.
        host = urlsplit(url).hostname
        proxy, network_name = self._egress_config()
        spec: dict[str, Any] = {"image": self.image, "tier": self.tier}
        spec.update(browser_egress_spec(
            {host} if host else set(),
            proxy=proxy, network_name=network_name, dns=self._egress_dns,
        ))
        # Pin the container's DNS for the target host to the validated IP, so the
        # browser cannot be DNS-rebound to an internal address at connect time —
        # the tier-2 analogue of tier-1's PinnedTransport. ``pinned_ip`` is None
        # only when allow_private skips pinning (local dev).
        if pinned_ip is not None and host:
            spec["extra_hosts"] = {host: pinned_ip}
        sandbox = await backend.launch(spec)
        args: dict[str, Any] = {
            "url": url,
            "width": int(width) if width else _DEFAULT_WIDTH,
            "height": int(height) if height else _DEFAULT_HEIGHT,
        }
        # Forward the model's wait/reveal choices verbatim (generic; no site logic).
        if wait_until:
            args["wait_until"] = wait_until
        if wait_for:
            args["wait_for"] = wait_for
        if wait_ms:
            args["wait_ms"] = int(wait_ms)
        if actions:
            args["actions"] = actions
        if capture_network:
            args["capture_network"] = True
        try:
            obs = await backend.exec(sandbox, ToolCall(name=self.name, args=args))
        finally:
            await backend.destroy(sandbox)
        # Normalise to the same observation shape http_fetch produces, so the
        # loop's content handling and the detectors are tool-agnostic. ``text`` is
        # the rendered visible text (shadow DOM + child frames) when the entrypoint
        # supplies it — where dynamic widgets put their data; it is a content key
        # the loop stores for grounding and the model reads.
        out: dict[str, Any] = {
            "status": obs.get("status", 200),
            "html": obs.get("html", ""),
            "url": obs.get("url", url),
            "rendered": True,
        }
        if obs.get("text"):
            out["text"] = obs["text"]
        if obs.get("content"):
            out["content"] = obs["content"]  # captured network bodies (groundable)
        if obs.get("network"):
            out["network"] = obs["network"]
        if obs.get("action_error"):
            out["action_error"] = obs["action_error"]
        # Surface any in-browser egress the sandbox proxy REFUSED (issue #54): a
        # subresource/redirect to a host outside the validated allowlist (or to an
        # internal/metadata address) is a contained attempt the model and detectors
        # should see, not a silent drop.
        if obs.get("blocked_egress"):
            out["blocked_egress"] = obs["blocked_egress"]
        return out
