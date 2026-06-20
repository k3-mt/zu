"""The test kit tests itself — the doubles behave, the factories assemble, and
the headline ``agent_runner`` fixture drives a real loop end to end."""

from __future__ import annotations

from zu_testing import (
    FakeSandboxBackend,
    FakeSink,
    mock_transport,
    registry_with,
    scripted_config,
)


async def test_fake_sink_records_and_closes() -> None:
    sink = FakeSink()
    await sink.append("e1")
    assert await sink.count() == 1
    assert await sink.query() == ["e1"]
    sink.close()
    assert sink.closed == 1


async def test_fake_sandbox_backend_render_and_lifecycle() -> None:
    class _Call:
        args = {"url": "http://spa.test/"}
    backend = FakeSandboxBackend(rendered="<h1>ok</h1>")
    sbx = await backend.launch({"tier": 2})
    obs = await backend.exec(sbx, _Call())
    await backend.destroy(sbx)
    assert obs == {"status": 200, "html": "<h1>ok</h1>", "url": "http://spa.test/"}
    assert backend.last_launch == {"tier": 2}
    assert backend.destroyed == 1


async def test_fake_sandbox_backend_entrypoint_mode() -> None:
    backend = FakeSandboxBackend(exec_output='{"hi": 1}\n', exit_code=0)
    sbx = await backend.launch({"image": "img"})
    code, out, err = await backend.exec_entrypoint(sbx, ["run"], environment={"K": "V"})
    assert (code, out.strip(), err) == (0, '{"hi": 1}', "")
    assert backend.exec_env == {"K": "V"}


def test_mock_transport_text_shortcut() -> None:
    t = mock_transport(text="hello", status=201)
    import httpx
    with httpx.Client(transport=t) as c:
        r = c.get("http://x/")
    assert r.status_code == 201 and r.text == "hello"


def test_registry_with_is_isolated() -> None:
    class _T:
        name = "t"
    reg = registry_with(tools={"t": _T()})
    assert reg.names("tools") == ["t"]


def test_scripted_config_shape() -> None:
    cfg = scripted_config([{"text": "{}", "finish": "stop"}], tools=["http_fetch"],
                          containment="required")
    assert cfg["provider"]["name"] == "scripted"
    assert cfg["plugins"]["tools"] == ["http_fetch"]
    assert cfg["containment"] == "required"


async def test_agent_runner_drives_a_real_loop(agent_runner, make_fetch_tool) -> None:
    result, events = await agent_runner(
        [{"tool": "http_fetch", "args": {"url": "http://shop.test/"}},
         {"text": '{"title": "Acme"}', "finish": "stop"}],
        tools={"http_fetch": make_fetch_tool(text="<h1>Acme</h1>")},
    )
    assert result.status.value == "success"
    assert result.value == {"title": "Acme"}
    assert any(e.type == "data.source.fetched" for e in events)


async def test_agent_runner_enforces_containment_floor(agent_runner) -> None:
    # A capability tool under containment='required' on a bare host is refused —
    # proving the fixture wires the floor through, useful for plugin authors.
    from zu_core.security import ContainmentRequired

    class _NetTool:
        name = "net"
        tier = 1
        schema = {"name": "net", "parameters": {"type": "object", "properties": {}}}
        capabilities: frozenset[str] = frozenset()
        egress = frozenset({"*"})
        async def __call__(self, ctx):
            return {"ok": True}

    import pytest
    with pytest.raises(ContainmentRequired):
        await agent_runner([{"text": "{}", "finish": "stop"}],
                           tools={"net": _NetTool()}, containment="required")
