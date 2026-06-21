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

from .net import validate_and_pin

_DEFAULT_IMAGE = "ghcr.io/k3-mt/zu-render-chromium:latest"
_OBS_KEYS = ("status", "url", "text", "controls", "html", "content", "network",
             "action_error", "consent_dismissed")


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
    prompt_fragment = (
        "browser(op=open|act|read|close, url?, actions?, capture_network?): a PERSISTENT "
        "headless browser. Open a url, then act/read step by step (state is kept) to drive "
        "a multi-step widget to the data you need; capture_network grabs the JSON it fetches."
    )
    capabilities = frozenset({CAP_NET, CAP_SANDBOX})
    egress = frozenset({EGRESS_OPEN})

    def __init__(
        self,
        backend: SessionBackend | None = None,
        image: str = _DEFAULT_IMAGE,
        *,
        allow_private: bool | None = None,
    ) -> None:
        self._backend = backend
        self.image = image
        self.allow_private = allow_private
        self._session: BrowserSessionHandle | None = None  # held across calls within a run

    def _resolve_backend(self) -> SessionBackend:
        if self._backend is None:
            from zu_backends.local_docker import LocalDockerBackend

            self._backend = LocalDockerBackend()
        return self._backend

    async def __call__(
        self, ctx: Any, op: str, url: str | None = None, actions: list | None = None,
        wait_until: str | None = None, capture_network: bool = False,
        width: int | None = None, height: int | None = None, html: bool = False,
    ) -> dict:
        if op == "open":
            if not url:
                return {"error": "op=open requires a url"}
            await self._close_session()  # one session at a time; replace any prior
            # Same SSRF backstop + DNS pin as render_dom, before leasing a browser.
            pinned_ip = validate_and_pin(url, allow_private=self.allow_private)
            spec: dict[str, Any] = {"image": self.image, "tier": self.tier, "network": True}
            host = urlsplit(url).hostname
            if pinned_ip is not None and host:
                spec["extra_hosts"] = {host: pinned_ip}
            self._session = await self._resolve_backend().open_session(spec)
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
            return self._normalise(await self._session.send(cmd))

        if op in ("act", "read"):
            if self._session is None:
                return {"error": "no open session; call browser(op=open, url=...) first"}
            cmd = {"op": op}
            if op == "act" and actions:
                cmd["actions"] = actions
            if html:
                cmd["html"] = True
            return self._normalise(await self._session.send(cmd))

        if op == "close":
            await self._close_session()
            return {"closed": True}

        return {"error": f"unknown op {op!r}; use open/act/read/close"}

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

    async def _close_session(self) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            try:
                await session.close()
            except Exception:  # noqa: BLE001 - teardown must not raise over a result
                pass

    async def aclose(self) -> None:
        """Close a lingering session — for run teardown so a container never leaks."""
        await self._close_session()
