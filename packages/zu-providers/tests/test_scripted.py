"""Build step 2 — the fake model plays its script back in order."""

from __future__ import annotations

from zu_core.ports import Finish
from zu_providers.scripted import ScriptedProvider


async def _req():
    from zu_core.ports import ModelRequest

    return ModelRequest(messages=[{"role": "user", "content": "go"}])


async def test_plays_moves_in_order() -> None:
    p = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "https://example.com"}},
            {"tool": "html_parse", "args": {"selector": ".price"}},
            {"text": "the price is $9", "finish": "stop"},
        ]
    )

    r1 = await p.complete(await _req())
    assert r1.finish is Finish.TOOL_CALLS
    assert r1.tool_calls[0].name == "http_fetch"
    assert r1.tool_calls[0].args == {"url": "https://example.com"}

    r2 = await p.complete(await _req())
    assert r2.tool_calls[0].name == "html_parse"

    r3 = await p.complete(await _req())
    assert r3.finish is Finish.STOP
    assert r3.text == "the price is $9"
    assert p.exhausted


async def test_past_end_is_a_stop() -> None:
    p = ScriptedProvider.from_moves([{"text": "done"}])
    await p.complete(await _req())
    overrun = await p.complete(await _req())
    assert overrun.finish is Finish.STOP
    assert overrun.text is None


def test_declares_capabilities() -> None:
    p = ScriptedProvider.from_moves([])
    assert p.capabilities.native_tools is True
