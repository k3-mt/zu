"""browser — a PERSISTENT, event-driven headless-browser session (tier 2).

Where ``render_dom`` is one-shot (a fresh browser per call), ``browser`` keeps ONE
headless browser ALIVE across calls so a model can drive a reactive, multi-step
widget the way a person does: ``open`` a url, then ``act`` / ``read`` repeatedly —
observing the real state (and the network responses it triggered) after each step,
reacting to what actually happened — then ``close``. That removes the
timing-fragility of replaying a fixed action sequence into a fresh browser, which a
reactive SPA defeats (a selection must register before the next step).

It surfaces content (rendered text, captured XHR/JSON, optional html); it does not
provide a transaction-submitting primitive. The session lives in the same hardened,
headless container as ``render_dom`` (caps dropped, DNS-pinned, --no-sandbox); the
state is held by the long-lived ``zu-browser`` server inside it.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from zu_core.ports import CAP_NET, CAP_SANDBOX, EGRESS_OPEN, BrowserSessionHandle, SessionBackend

from ._session import close_run, get_or_open, run_key
from .action_schema import validate_actions
from .browser_egress import browser_egress_spec, contained_egress_config, egress_caveat
from .net import validate_and_pin
from .settle import settle, settle_budget_ms

_DEFAULT_IMAGE = "ghcr.io/k3-mt/zu-render-chromium:latest"
_OBS_KEYS = ("status", "url", "text", "html", "content", "network",
             "action_error", "action_error_kind", "consent_dismissed")


class Browser:
    name = "browser"
    tier = 2  # like render_dom — unlocked only after a detector escalates
    schema = {
        "name": "browser",
        "description": (
            "Drive a PERSISTENT headless browser across calls to work through a "
            "reactive, multi-step JS widget. op=open a url, then op=act / op=read "
            "repeatedly (the page state is held between calls), then op=close. "
            "Read the returned text after each step and decide the next action — "
            "if action_error comes back, the selector missed; try another."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["open", "act", "read", "close"]},
                "url": {"type": "string", "description": "for op=open: the page to open"},
                "actions": {
                    "type": "array",
                    "description": "for op=act: actions run in order on the HELD page — "
                                   "{click|fill|select|wait_for: <selector>, value?} | {wait_ms:<n>}. "
                                   "A selector is CSS or a text= selector; target what you SEE. "
                                   "For an AMBIGUOUS option (e.g. a '1'/'2'/'3' button that appears "
                                   "many times), add \"near\": \"<label text>\" to a click — it picks "
                                   "the matching control closest to that label, e.g. "
                                   "{\"click\": \"1\", \"near\": \"Number of pets\"}.",
                    "items": {"type": "object"},
                },
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle", "commit"],
                    "description": "for op=open: when navigation is done (optional)",
                },
                "capture_network": {
                    "type": "boolean",
                    "description": "for op=open: capture XHR/JSON responses (the widget's data) "
                                   "for the whole session (optional)",
                },
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "html": {"type": "boolean", "description": "also return raw html (optional)"},
            },
            "required": ["op"],
        },
    }
    capabilities = frozenset({CAP_NET, CAP_SANDBOX})
    egress = frozenset({EGRESS_OPEN})
    # The WRITE-SHAPED ops (issue #76): ops that MUTATE the page/session rather than
    # read it. ``act`` runs click/fill/select on the live surface — a state change.
    # The ``read-only`` action-policy preset reads THIS declaration (not a hardcoded
    # tool-name list) to deny write-shaped ops generically; a tool that omits it is
    # read-only by default. ``op`` is the arg the rule matches against (browser's
    # discriminator), so the preset stays content-free and tool-agnostic.
    write_ops = frozenset({"act"})
    # The model-facing prompt DISCLOSES the egress posture, derived from ``egress``
    # (issue #54) — not a hardcoded literal.
    prompt_fragment = (
        "browser(op=open|act|read|close, url?, actions?, capture_network?): a PERSISTENT "
        "headless browser. Open a url, then act/read step by step (state is kept) to drive "
        "a multi-step widget to the data you need; capture_network grabs the JSON it fetches. "
        + egress_caveat(egress)
    )

    def __init__(
        self,
        backend: SessionBackend | None = None,
        image: str = _DEFAULT_IMAGE,
        *,
        allow_private: bool | None = None,
        proxy: dict | None = None,
        network_name: str | None = None,
        egress_dns: object | None = None,
        allowed_domains: list[str] | None = None,
    ) -> None:
        self._backend = backend
        self.image = image
        self.allow_private = allow_private
        # The per-agent positive navigation allowlist (issue #74): None ⇒ unset.
        # Enforced on each op=open url via validate_and_pin, alongside the pre-exec gate.
        self.allowed_domains = allowed_domains
        # Egress-enforcement wiring (issue #54): a provisioned proxy + internal
        # network scopes in-browser egress to the validated target set.
        self._proxy = proxy
        self._network_name = network_name
        self._egress_dns = egress_dns
        self._session: BrowserSessionHandle | None = None  # held across calls within a run

    def _resolve_backend(self) -> SessionBackend:
        if self._backend is None:
            from zu_backends.local_docker import LocalDockerBackend

            self._backend = LocalDockerBackend()
        return self._backend

    def _egress_config(self) -> tuple[dict | None, str | None]:
        """(proxy, network_name) governing this launch: explicit constructor config
        wins; else the contained run's env-derived proxy, so the production-contained
        path emits the isolated+proxy spec instead of bare network."""
        if self._proxy is not None and self._network_name is not None:
            return self._proxy, self._network_name
        return contained_egress_config()

    async def __call__(
        self, ctx: Any, op: str, url: str | None = None, actions: list | None = None,
        wait_until: str | None = None, capture_network: bool = False,
        width: int | None = None, height: int | None = None, html: bool = False,
    ) -> dict:
        if op == "open":
            if not url:
                return {"error": "op=open requires a url"}
            # Same SSRF backstop + DNS pin as render_dom, before leasing a browser.
            # The spec also carries the validated target as the egress allowlist
            # (issue #54): in-browser subresources/redirects are scoped to it.
            pinned_ip = validate_and_pin(
                url, allow_private=self.allow_private, allowed_domains=self.allowed_domains
            )
            host = urlsplit(url).hostname
            proxy, network_name = self._egress_config()
            spec: dict[str, Any] = {"image": self.image, "tier": self.tier}
            spec.update(browser_egress_spec(
                {host} if host else set(),
                proxy=proxy, network_name=network_name, dns=self._egress_dns,
            ))
            if pinned_ip is not None and host:
                spec["extra_hosts"] = {host: pinned_ip}
            # Open via the SHARED run-scoped registry: the run's session is shared with
            # action_surface/pointer/vision. A reused same-host session re-navigates via
            # the open command's url (the container honours it).
            key = run_key(ctx)
            backend = self._resolve_backend()
            self._session = await get_or_open(
                key, lambda: self._open_session(backend, spec, key)
            )
            cmd: dict[str, Any] = {"op": "open", "url": url}
            if wait_until:
                cmd["wait_until"] = wait_until
            if capture_network:
                cmd["capture_network"] = True
            if width:
                cmd["width"] = int(width)
            if height:
                cmd["height"] = int(height)
            if html:
                cmd["html"] = True
            obs = self._normalise(await self._session.send(cmd))
            # Auto-settle after navigation (navigation-reliability layer): wait, bounded by
            # the settle budget, for the page to go quiescent / SPA-settled before the model
            # reads it — a harness precondition, not a model-chosen wait. Inert when the run
            # carries no settle budget or the session can't answer the probe.
            post = await settle(self._session, budget_ms=settle_budget_ms(ctx),
                                phase="post", want_stable=True)
            if post is not None:
                obs["settle"] = [post]
            return obs

        if op == "read":
            if self._session is None:
                return {"error": "no open session; call browser(op=open, url=...) first"}
            cmd = {"op": "read"}
            if html:
                cmd["html"] = True
            return self._normalise(await self._session.send(cmd))

        if op == "act":
            if self._session is None:
                return {"error": "no open session; call browser(op=open, url=...) first"}
            # Validate the model-supplied action args at the tool boundary BEFORE
            # forwarding them to the live session (issue #65 F52): each action must
            # be a well-formed {click|fill|select|wait_for: <selector>, value?, near?}
            # | {wait_ms:<n>} — allowed op, selector/value typed, no stray fields.
            # A malformed action is refused here, never forwarded to the sandbox.
            if actions:
                action_err = validate_actions(actions)
                if action_err is not None:
                    return {"error": action_err, "blocked": "invalid_action"}
            cmd = {"op": "act"}
            if actions:
                cmd["actions"] = actions
            if html:
                cmd["html"] = True
            budget_ms = settle_budget_ms(ctx)
            settles: list[dict] = []
            # Auto-settle BEFORE acting: let the prior step's mutations land so the action
            # targets a stable surface (no longer the model's job to remember wait_until).
            pre = await settle(self._session, budget_ms=budget_ms, phase="pre")
            if pre is not None:
                settles.append(pre)
            obs = self._normalise(await self._session.send(cmd))
            # Auto-settle AFTER acting: wait for the reactive surface to stabilise before read.
            post = await settle(self._session, budget_ms=budget_ms, phase="post",
                                want_stable=True)
            if post is not None:
                settles.append(post)
            if settles:
                obs["settle"] = settles
            return obs

        if op == "close":
            # The model explicitly closed the page: tear down the run's SHARED session
            # authoritatively (the registry owns the live container), and drop our ref.
            self._session = None
            await close_run(run_key(ctx))
            return {"closed": True}

        return {"error": f"unknown op {op!r}; use open/act/read/close"}

    @staticmethod
    async def _open_session(backend: SessionBackend, spec: dict, key: str) -> Any:
        """Lease the live session the run shares (refcounted ``open_run_session`` when
        the backend has it, else the one-shot ``open_session``)."""
        opener = getattr(backend, "open_run_session", None)
        if key and callable(opener):
            return await opener(spec, run_key=key)
        return await backend.open_session(spec)

    @staticmethod
    def _normalise(obs: Any) -> dict:
        """The session response as a loop-friendly observation (content keys the
        loop stores for grounding; a session/command error passed through)."""
        if not isinstance(obs, dict):
            return {"error": "bad session response"}
        if "error" in obs and "text" not in obs:
            return {"error": obs["error"]}
        out: dict[str, Any] = {"rendered": True}
        for k in _OBS_KEYS:
            if obs.get(k) is not None:
                out[k] = obs[k]
        return out

    async def aclose(self) -> None:
        """Drop this instance's reference to the shared session. The AUTHORITATIVE
        run-end teardown is the shared registry's ``close_run`` (wired into the loop's
        run-end lifecycle), so the container is released exactly once — this tool must
        not close it out from under another tool that shares the same run page."""
        self._session = None
