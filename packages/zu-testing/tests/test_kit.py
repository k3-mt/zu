"""The test kit tests itself — the doubles behave, the factories assemble, and
the headline ``agent_runner`` fixture drives a real loop end to end."""

from __future__ import annotations

from zu_testing import (
    FakeSandboxBackend,
    FakeSink,
    mock_transport,
    registry_with,
    scripted_config,
    search_tool,
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


async def test_fake_sandbox_backend_exec_is_generic_over_non_render_calls() -> None:
    # F41 — exec must NOT be over-fit to the browser-render {url} contract. A
    # ToolCall whose args lack "url" must not KeyError; it gets a usable generic
    # observation. This fails on the old code (return {..., "url": call.args["url"]}).
    class _NonRenderCall:
        args = {"selector": ".price"}  # no "url"

    backend = FakeSandboxBackend(rendered="<h1>ok</h1>")
    sbx = await backend.launch({"tier": 2})
    obs = await backend.exec(sbx, _NonRenderCall())
    assert obs["status"] == 200
    assert obs["html"] == "<h1>ok</h1>"
    assert "url" not in obs  # no url arg -> no echoed url, and no crash
    assert backend.exec_calls  # the call was still recorded

    # A call with NO args attribute at all is also fine (fully generic).
    class _Bare:
        pass

    obs2 = await backend.exec(sbx, _Bare())
    assert obs2["status"] == 200 and "url" not in obs2

    # And the render behavior is unchanged when a url IS present.
    class _RenderCall:
        args = {"url": "http://spa.test/"}

    obs3 = await backend.exec(sbx, _RenderCall())
    assert obs3 == {"status": 200, "html": "<h1>ok</h1>", "url": "http://spa.test/"}


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


async def test_search_tool_default_url_is_valid() -> None:
    # F43 — search_tool's default result must carry a VALID absolute URL (not the
    # old malformed "https://example/"), so a downstream fetch of the result
    # doesn't trip URL validation. This fails on the old code.
    import inspect
    from urllib.parse import urlparse

    tool = search_tool()  # constructs the real WebSearch — proves the factory works
    assert tool is not None

    src = inspect.getsource(search_tool)
    assert "https://example/" not in src, "the malformed default URL must be gone"

    # The default URL the factory falls back to must be a real http(s) URL whose
    # host has a dot (a TLD-shaped host, not the bare "example"). Exercise it end
    # to end: run the real web_search over the fake transport with the DEFAULT
    # results and confirm the emitted result URL passes validation.
    result = await tool(None, query="anything")
    urls: list[str] = []
    if isinstance(result, dict):
        for r in result.get("results", []) or []:
            if isinstance(r, dict) and isinstance(r.get("url"), str):
                urls.append(r["url"])
    assert urls, "the default search result should carry a url"
    for u in urls:
        parsed = urlparse(u)
        assert parsed.scheme in ("http", "https")
        assert parsed.hostname and "." in parsed.hostname, f"{u!r} is not a valid host"


def test_make_search_tool_fixture_is_usable(make_search_tool) -> None:
    # F43 — search_tool must have a pytest fixture parallel to make_fetch_tool.
    tool = make_search_tool()
    assert tool is not None
    # It accepts scripted results just like the factory.
    tool2 = make_search_tool(results=[{"title": "T", "url": "https://acme.example/"}])
    assert tool2 is not None


async def test_agent_runner_query_is_parameterizable_not_bare_q(agent_runner) -> None:
    # F47 — the runner's query must not be a hardcoded meaningless 'q'. It defaults
    # to a sensible descriptive query and is overridable, and it reaches the run
    # (recorded on the started event's task query).
    import inspect

    # Introspect the inner run() default: it must not be the bare 'q'.
    # (agent_runner returns the closure; grab its source.)
    src = inspect.getsource(agent_runner)
    assert 'query: str = "q"' not in src, "the bare placeholder 'q' default must be gone"

    result, events = await agent_runner(
        [{"text": "{}", "finish": "stop"}], query="find the vet's opening hours"
    )
    assert result.status.value == "success"
    started = [e for e in events if e.type == "harness.task.started"]
    assert started, "a task.started event should be recorded"
    assert started[0].payload.get("query") == "find the vet's opening hours"


def test_agent_runner_containment_default_is_sourced_from_run_task_not_duplicated() -> None:
    # F47 — the runner must NOT re-state 'audit'; it defers to run_task's real
    # default so it can't drift. Prove the runner's own literal is gone and that
    # the deferred value equals the loop's actual default.
    import inspect

    from zu_core.loop import run_task
    from zu_testing.fixtures import agent_runner as agent_runner_fixture

    src = inspect.getsource(agent_runner_fixture)
    assert 'containment: str = "audit"' not in src, (
        "agent_runner must not duplicate the 'audit' containment default"
    )
    # The runner must reference run_task's signature default rather than a literal.
    assert 'signature(run_task)' in src and '"containment"' in src, (
        "agent_runner should source the containment default from run_task's signature"
    )
    real_default = inspect.signature(run_task).parameters["containment"].default
    assert real_default == "audit"  # documents the current real floor
