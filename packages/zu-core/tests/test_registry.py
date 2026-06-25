"""Build step 2 — Registry + plugin discovery.

Proves the plugin system actually finds plugins — the whole extensible
promise. A dummy tool is found both ways: registered inline via the decorator,
and discovered from an installed package's entry points (zu-tools ships
http_fetch via the 'zu.tools' group).
"""

from __future__ import annotations

import pytest

import zu_core.registry as registry_mod
from zu_core.registry import GROUPS, REGISTRY, Registry, tool


def test_inline_decorator_registration() -> None:
    @tool
    class MyTool:
        name = "my_tool"
        schema: dict = {}
        prompt_fragment = "does a thing"

        async def __call__(self, ctx, **kw):  # pragma: no cover - shape only
            return {}

    assert "my_tool" in REGISTRY.names("tools")
    assert REGISTRY.get("tools", "my_tool") is MyTool


def test_decorator_uses_class_name_without_name_attr() -> None:
    reg = Registry()

    class Bare:
        pass

    # exercise the registry directly so we don't pollute the global one
    reg.register("tools", getattr(Bare, "name", Bare.__name__), Bare)
    assert "Bare" in reg.names("tools")


def test_package_entry_point_discovery() -> None:
    """zu-tools declares http_fetch / html_parse under 'zu.tools'."""
    reg = Registry()
    reg.discover()
    names = reg.names("tools")
    assert "http_fetch" in names, f"expected built-in http_fetch via entry points, got {names}"
    assert "html_parse" in names

    HttpFetch = reg.get("tools", "http_fetch")
    inst = HttpFetch()
    assert inst.name == "http_fetch"
    assert "url" in inst.schema["parameters"]["properties"]


def test_all_groups_present() -> None:
    reg = Registry()
    for kind in GROUPS:
        assert reg.names(kind) == []  # empty until discover()/register()


def test_name_collision_is_warned_not_silent(caplog) -> None:
    """A second plugin shadowing a name (e.g. a typosquat on a built-in) must
    log a warning — last-write-wins is kept, but never silently."""
    reg = Registry()
    reg.register("tools", "http_fetch", "BUILTIN")
    with caplog.at_level("WARNING", logger="zu.registry"):
        reg.register("tools", "http_fetch", "SHADOW")
    assert reg.get("tools", "http_fetch") == "SHADOW"  # last write wins
    assert any("collision" in r.message for r in caplog.records)


def test_re_registering_same_object_is_quiet(caplog) -> None:
    reg = Registry()
    reg.register("tools", "t", "OBJ")
    with caplog.at_level("WARNING", logger="zu.registry"):
        reg.register("tools", "t", "OBJ")  # idempotent, not a collision
    assert not caplog.records


class _FakeEP:
    def __init__(self, name: str, loader) -> None:
        self.name = name
        self._loader = loader

    def load(self):
        return self._loader()


def test_discovery_isolates_a_broken_plugin(monkeypatch) -> None:
    """One plugin whose entry point raises must not take down the rest."""

    def boom():
        raise RuntimeError("third-party plugin blew up on import")

    good = _FakeEP("good_tool", lambda: "GOOD")
    bad = _FakeEP("bad_tool", boom)

    def fake_entry_points(group: str):
        return [good, bad] if group == "zu.tools" else []

    monkeypatch.setattr(registry_mod, "entry_points", fake_entry_points)

    reg = Registry()
    failures = reg.discover()

    # the good plugin still loaded…
    assert reg.get("tools", "good_tool") == "GOOD"
    assert "bad_tool" not in reg.names("tools")
    # …and the failure was isolated and recorded, not raised.
    assert len(failures) == 1
    assert failures[0].kind == "tools"
    assert failures[0].name == "bad_tool"
    assert isinstance(failures[0].error, RuntimeError)
    assert reg.failures == failures


# --- interface versioning (MLR §6) -------------------------------------------


def _tool(major=None):
    class T:
        name = "v_tool"
        schema: dict = {}
        prompt_fragment = "x"

        async def __call__(self, ctx, **kw):  # pragma: no cover - shape only
            return {}

    if major is not None:
        T.__zu_interface__ = major
    return T


def test_plugin_with_matching_interface_registers() -> None:
    from zu_core.ports import INTERFACE_VERSION

    reg = Registry()
    reg.register("tools", "v_tool", _tool(INTERFACE_VERSION["tools"]))
    assert "v_tool" in reg.names("tools")


def test_plugin_without_declaration_is_treated_as_v1() -> None:
    # Back-compat: existing built-ins declare nothing and must keep loading.
    reg = Registry()
    reg.register("tools", "v_tool", _tool())  # no __zu_interface__
    assert "v_tool" in reg.names("tools")


def test_incompatible_major_is_refused_with_a_clear_error() -> None:
    from zu_core.registry import IncompatibleInterfaceError

    reg = Registry()
    with pytest.raises(IncompatibleInterfaceError) as exc:
        reg.register("tools", "v_tool", _tool(major=999))
    msg = str(exc.value)
    assert "v999" in msg and "v_tool" in msg  # names both the bad version and the plugin
    assert "v_tool" not in reg.names("tools")  # and it did not enter the registry


def test_non_integer_declaration_is_refused() -> None:
    from zu_core.registry import IncompatibleInterfaceError

    reg = Registry()
    with pytest.raises(IncompatibleInterfaceError):
        reg.register("tools", "v_tool", _tool(major="two"))


def test_discovery_isolates_an_incompatible_plugin(monkeypatch) -> None:
    # A plugin built against a future interface major is isolated and recorded,
    # exactly like one that fails to import — discovery of the rest continues.
    good = _FakeEP("good_tool", lambda: _tool())
    future = _FakeEP("future_tool", lambda: _tool(major=999))

    def fake_entry_points(group: str):
        return [good, future] if group == "zu.tools" else []

    monkeypatch.setattr(registry_mod, "entry_points", fake_entry_points)
    reg = Registry()
    failures = reg.discover()

    assert "good_tool" in reg.names("tools")
    assert "future_tool" not in reg.names("tools")
    assert len(failures) == 1 and failures[0].name == "future_tool"
    from zu_core.registry import IncompatibleInterfaceError
    assert isinstance(failures[0].error, IncompatibleInterfaceError)


# --- ZU-EXT-1: consumers register new port kinds without forking the core ----


def test_consumer_registers_new_kind_without_core_edit() -> None:
    """A consumer can introduce a brand-new typed port (e.g. a payment
    orchestrator) and register implementations through the one registry the loop
    reads, with zero edits to zu_core — the ZU-EXT-1 conformance test. (The example
    kind is one zu_core does NOT ship: ``credential_brokers`` is now a built-in
    after §8, so this uses a still-foreign kind to prove the no-core-edit path.)"""
    reg = Registry()
    assert "payment_orchestrators" not in reg.kinds()

    reg.register_kind("payment_orchestrators", "zu.payment_orchestrators")
    assert "payment_orchestrators" in reg.kinds()

    class FakeOrchestrator:
        name = "fake"

    reg.register("payment_orchestrators", "fake", FakeOrchestrator)
    assert reg.get("payment_orchestrators", "fake") is FakeOrchestrator
    assert "fake" in reg.names("payment_orchestrators")


def test_register_kind_is_idempotent_but_refuses_group_conflict() -> None:
    reg = Registry()
    reg.register_kind("brokers", "zu.brokers")
    reg.register_kind("brokers", "zu.brokers")  # idempotent, no error
    with pytest.raises(ValueError):
        reg.register_kind("brokers", "zu.other_group")


def test_interface_gate_applies_to_a_new_kind() -> None:
    """The interface-major gate works for consumer kinds too, not just built-ins."""
    from zu_core.registry import IncompatibleInterfaceError

    reg = Registry()
    reg.register_kind("brokers", "zu.brokers", interface_major=1)

    class V2:
        name = "v2"
        __zu_interface__ = 2

    with pytest.raises(IncompatibleInterfaceError):
        reg.register("brokers", "v2", V2)


def test_new_kind_discovered_via_zu_kinds_group(monkeypatch) -> None:
    """A package declaring a kind via the ``zu.kinds`` entry-point group makes
    that kind (and its plugins) discoverable with no core edit."""
    from zu_core.registry import KINDS_GROUP, KindSpec

    spec = KindSpec("brokers", "zu.brokers")
    kind_ep = _FakeEP("brokers", lambda: spec)
    impl_ep = _FakeEP("acme", lambda: "ACME_BROKER")

    def fake_entry_points(group: str):
        if group == KINDS_GROUP:
            return [kind_ep]
        if group == "zu.brokers":
            return [impl_ep]
        return []

    monkeypatch.setattr(registry_mod, "entry_points", fake_entry_points)
    reg = Registry()
    reg.discover()

    assert "brokers" in reg.kinds()
    assert reg.get("brokers", "acme") == "ACME_BROKER"
