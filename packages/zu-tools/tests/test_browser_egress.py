"""Browser egress scoping (issue #54) — the GENERIC, offline-testable pieces.

The tier-2/3 browser tools render attacker-influenced pages. Tier-1's
``validate_and_pin`` checks only the INITIAL URL; once Chromium runs, in-page
fetch/XHR, redirects and subresource loads reach anywhere if the container is
launched with bare ``network: True``. This proves the two tool-side fixes:

  * the launch spec now carries the validated target set as an egress
    ``allowlist`` (and switches off bare ``network: True`` onto the isolated +
    proxy mode when a proxy is provisioned), so the tools no longer under-declare;
  * the pure ``subresource_allowed`` decision the proxy enforces refuses an
    off-allowlist host and any internal/metadata literal, while permitting the
    validated target.

All $0, no Docker, no network — the spec is asserted via the fake backend and the
decision is a pure function.
"""

from __future__ import annotations

from zu_core.ports import EGRESS_OPEN
from zu_testing import FakeSandboxBackend
from zu_tools.action_surface import ActionSurface
from zu_tools.browser import Browser
from zu_tools.browser_egress import browser_egress_spec, egress_caveat, subresource_allowed
from zu_tools.render import RenderDom

_RENDERED = "<html><body>ok</body></html>"


# --- the pure allowlist decision ------------------------------------------

def test_subresource_to_allowlisted_host_is_permitted() -> None:
    allow = frozenset({"shop.test"})
    assert subresource_allowed("shop.test", allow) is True


def test_subresource_off_allowlist_is_refused() -> None:
    allow = frozenset({"shop.test"})
    assert subresource_allowed("evil.test", allow) is False


def test_subresource_to_cloud_metadata_is_refused_even_if_allowlisted() -> None:
    # The cloud-metadata endpoint the tier-1 SSRF guard exists to block: refused as
    # an internal literal regardless of the allowlist.
    assert subresource_allowed("169.254.169.254", frozenset({"169.254.169.254"})) is False
    assert subresource_allowed("169.254.169.254", frozenset({EGRESS_OPEN})) is False


def test_subresource_to_rfc1918_internal_is_refused() -> None:
    assert subresource_allowed("10.0.0.5", frozenset({"shop.test"})) is False
    assert subresource_allowed("192.168.1.10", frozenset({EGRESS_OPEN})) is False


def test_open_egress_permits_any_external_host() -> None:
    # The honest open-web declaration: a public host is permitted, an internal one
    # is still refused.
    assert subresource_allowed("anything.example", frozenset({EGRESS_OPEN})) is True


def test_empty_host_is_refused() -> None:
    assert subresource_allowed(None, frozenset({EGRESS_OPEN})) is False
    assert subresource_allowed("", frozenset({"shop.test"})) is False


# --- the spec builder ------------------------------------------------------

def test_spec_carries_allowlist_even_without_a_proxy() -> None:
    # No proxy provisioned: still the honest open egress, but the validated target
    # is attached as the allowlist for a firewall-capable backend to enforce — the
    # tool never emits the bare/unscoped form.
    spec = browser_egress_spec({"shop.test"})
    assert spec["allowlist"] == ["shop.test"]
    assert spec["network"] is True


def test_spec_uses_isolated_proxy_mode_when_provisioned() -> None:
    # With a proxy + internal network, the container leaves bare network:True for
    # the isolated default-DROP mode whose only route off-box is the allowlist proxy.
    spec = browser_egress_spec(
        {"shop.test"}, proxy={"host": "10.1.0.2", "port": 8080}, network_name="zu-internal",
    )
    assert spec["network"] == "isolated"
    assert spec["network_name"] == "zu-internal"
    assert spec["proxy"] == {"host": "10.1.0.2", "port": 8080}
    assert spec["allowlist"] == ["shop.test"]


def test_caveat_is_derived_from_egress_set_not_hardcoded() -> None:
    open_caveat = egress_caveat(frozenset({EGRESS_OPEN}))
    assert "open egress" in open_caveat.lower()
    scoped = egress_caveat(frozenset({"api.shop.test"}))
    assert "api.shop.test" in scoped
    assert "open egress" not in scoped.lower()


# --- the three tools no longer launch bare network:True -------------------

async def test_render_dom_spec_carries_target_allowlist() -> None:
    backend = FakeSandboxBackend(rendered=_RENDERED)
    await RenderDom(backend=backend, allow_private=True).__call__(ctx=None, url="http://spa.test/")
    spec = backend.launched[0]
    # The validated target is the allowlist; in-browser egress is scoped to it.
    assert spec["allowlist"] == ["spa.test"]


async def test_render_dom_uses_isolated_proxy_when_provisioned() -> None:
    backend = FakeSandboxBackend(rendered=_RENDERED)
    tool = RenderDom(
        backend=backend, allow_private=True,
        proxy={"host": "10.1.0.2", "port": 8080}, network_name="zu-internal",
    )
    await tool.__call__(ctx=None, url="http://spa.test/")
    spec = backend.launched[0]
    assert spec["network"] == "isolated"
    assert spec["proxy"] == {"host": "10.1.0.2", "port": 8080}
    assert spec["allowlist"] == ["spa.test"]


def test_render_dom_prompt_discloses_egress_posture() -> None:
    assert "egress" in RenderDom.prompt_fragment.lower()


def test_browser_prompt_discloses_egress_posture() -> None:
    assert "egress" in Browser.prompt_fragment.lower()


def test_action_surface_prompt_discloses_egress_posture() -> None:
    assert "egress" in ActionSurface.prompt_fragment.lower()


class _Ctx:
    def __init__(self, task_id: str) -> None:
        self.spec = type("S", (), {"task_id": task_id})()


class _FakeSession:
    async def send(self, cmd: dict) -> dict:
        return {"status": 200, "url": "http://spa.test/", "text": "ok"}

    async def close(self) -> None:
        pass


class _SpecCapturingSessionBackend:
    def __init__(self) -> None:
        self.specs: list[dict] = []

    async def open_session(self, spec: dict) -> _FakeSession:
        self.specs.append(spec)
        return _FakeSession()


async def test_browser_open_spec_carries_target_allowlist() -> None:
    backend = _SpecCapturingSessionBackend()
    tool = Browser(backend=backend, allow_private=True)
    await tool(_Ctx("run-egress"), op="open", url="http://spa.test/")
    assert backend.specs[0]["allowlist"] == ["spa.test"]
    from zu_tools._session import close_run
    await close_run("run-egress")


class _FakeAxSession:
    async def send(self, cmd: dict) -> dict:
        return {"axtree": [{"role": {"value": "button"}, "name": {"value": "Buy"}}],
                "title": "T", "url": cmd["url"]}

    async def close(self) -> None:
        pass


class _SpecCapturingAxBackend:
    def __init__(self) -> None:
        self.specs: list[dict] = []

    async def open_session(self, spec: dict) -> _FakeAxSession:
        self.specs.append(spec)
        return _FakeAxSession()


async def test_action_surface_open_spec_carries_target_allowlist() -> None:
    backend = _SpecCapturingAxBackend()
    tool = ActionSurface(backend=backend, allow_private=True)
    await tool(_Ctx("run-as-egress"), op="open", url="http://shop.test/")
    assert backend.specs[0]["allowlist"] == ["shop.test"]
    from zu_tools._session import close_run
    await close_run("run-as-egress")


# --- the CONTAINED configuration emits isolated+proxy, derived from the env ---

def test_contained_config_derives_proxy_and_network_from_env(monkeypatch) -> None:
    from zu_tools.browser_egress import contained_egress_config

    # Uncontained (no ZU_SANDBOXED): no config, the host/test path.
    monkeypatch.delenv("ZU_SANDBOXED", raising=False)
    assert contained_egress_config() == (None, None)

    # Contained: ZU_SANDBOXED + HTTPS_PROXY + ZU_SANDBOX_NETWORK -> isolated+proxy.
    monkeypatch.setenv("ZU_SANDBOXED", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://egress-proxy:8080")
    monkeypatch.setenv("ZU_SANDBOX_NETWORK", "zu-sandbox-net")
    proxy, network_name = contained_egress_config()
    assert proxy == {"host": "egress-proxy", "port": 8080}
    assert network_name == "zu-sandbox-net"


async def test_render_dom_in_contained_run_emits_isolated_proxy_spec(monkeypatch) -> None:
    # The standard contained construction (no explicit proxy passed) must STILL
    # yield the scoped isolated+proxy spec — the production-contained path derives
    # the proxy from the run's env, never bare network:True.
    monkeypatch.setenv("ZU_SANDBOXED", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://egress-proxy:8080")
    monkeypatch.setenv("ZU_SANDBOX_NETWORK", "zu-sandbox-net")
    backend = FakeSandboxBackend(rendered=_RENDERED)
    await RenderDom(backend=backend, allow_private=True).__call__(ctx=None, url="http://spa.test/")
    spec = backend.launched[0]
    assert spec["network"] == "isolated"
    assert spec["network_name"] == "zu-sandbox-net"
    assert spec["proxy"] == {"host": "egress-proxy", "port": 8080}
    assert spec["allowlist"] == ["spa.test"]
    assert spec.get("network") is not True  # NOT bare network:True


# --- the EXACT acceptance scenario (criterion 3): in-browser subresources -----

class _SubresourceProxyBackend(FakeSandboxBackend):
    """A fake sandbox that, during the render, replays the scripted browser session's
    in-browser subresource/redirect attempts THROUGH the launch spec's allowlist (the
    job the real LocalEgressProxy does on the isolated network). Each attempt to a host
    outside the validated target set — or to any internal/metadata literal — is DENIED
    and recorded as a blocked-egress observation; the validated target succeeds."""

    def __init__(self, *, subresource_hosts: list[str], **kw) -> None:
        super().__init__(**kw)
        self._subresource_hosts = subresource_hosts
        self.fetched_subresources: list[str] = []

    async def exec(self, sandbox, call):
        allow = frozenset(sandbox["spec"]["allowlist"])
        blocked: list[str] = []
        for host in self._subresource_hosts:
            if subresource_allowed(host, allow):
                self.fetched_subresources.append(host)
            else:
                blocked.append(host)
        return {"status": 200, "html": self.rendered, "url": call.args["url"],
                "blocked_egress": blocked}


async def test_acceptance_in_browser_subresources_are_scoped_to_target(monkeypatch) -> None:
    # Criterion 3 (verbatim): a render whose scripted browser session attempts a
    # subresource/redirect to 169.254.169.254 AND to an RFC1918 host is DENIED, while
    # a request to the validated target host succeeds — asserted via the sandbox spec
    # passed to a fake backend (isolated + proxy + expected allowlist) and the
    # resulting blocked-egress observation. No network, no Docker.
    monkeypatch.setenv("ZU_SANDBOXED", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://egress-proxy:8080")
    monkeypatch.setenv("ZU_SANDBOX_NETWORK", "zu-sandbox-net")
    backend = _SubresourceProxyBackend(
        rendered=_RENDERED,
        subresource_hosts=["spa.test", "169.254.169.254", "10.0.0.5", "evil.test"],
    )
    out = await RenderDom(backend=backend, allow_private=True).__call__(
        ctx=None, url="http://spa.test/"
    )
    # The spec is the scoped isolated+proxy form with the validated target allowlist.
    spec = backend.launched[0]
    assert spec["network"] == "isolated"
    assert spec["proxy"] == {"host": "egress-proxy", "port": 8080}
    assert spec["allowlist"] == ["spa.test"]
    # The cloud-metadata endpoint, the RFC1918 host, and the off-allowlist host are
    # all DENIED (surfaced as a blocked-egress observation); only the validated
    # target is fetched.
    assert set(out["blocked_egress"]) == {"169.254.169.254", "10.0.0.5", "evil.test"}
    assert backend.fetched_subresources == ["spa.test"]
