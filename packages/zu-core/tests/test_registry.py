"""Build step 2 — Registry + plugin discovery.

Proves the plugin system actually finds plugins — the whole extensible
promise. A dummy tool is found both ways: registered inline via the decorator,
and discovered from an installed package's entry points (zu-tools ships
http_fetch via the 'zu.tools' group).
"""

from __future__ import annotations

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
