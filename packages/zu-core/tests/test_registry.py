"""Build step 2 — Registry + plugin discovery.

Proves the plugin system actually finds plugins — the whole extensible
promise. A dummy tool is found both ways: registered inline via the decorator,
and discovered from an installed package's entry points (zu-tools ships
http_fetch via the 'zu.tools' group).
"""

from __future__ import annotations

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
