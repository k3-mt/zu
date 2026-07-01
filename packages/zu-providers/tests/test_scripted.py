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


async def test_complete_returns_fresh_copy_not_shared_instance() -> None:
    # #65 F45: each complete() must hand back a fresh copy, so a caller that
    # mutates a returned response can't corrupt the recorded move for a later
    # replay. A ScriptedProvider built from ONE move, replayed twice (the second
    # call falls through to the same stored move only if we replayed it — instead
    # build a single move and replay by re-running the same index would exhaust;
    # so drive two providers off the same move object to prove copy-per-call).
    from zu_core.ports import ModelResponse, ToolCall

    move = ModelResponse(
        tool_calls=[ToolCall(name="http_fetch", args={"url": "https://example.com"})],
        finish=Finish.TOOL_CALLS,
        usage={"total_tokens": 5},
    )
    p = ScriptedProvider([move, move])  # same instance scripted twice

    r1 = await p.complete(await _req())
    # A caller mutates the returned response (appends a tool call, edits usage).
    r1.tool_calls.append(ToolCall(name="injected", args={}))
    r1.usage["total_tokens"] = 999

    r2 = await p.complete(await _req())
    # On old code r2 IS r1 (same shared instance), so it would carry the mutation.
    assert r2 is not r1
    assert len(r2.tool_calls) == 1  # not corrupted by r1's append
    assert r2.tool_calls[0].name == "http_fetch"
    assert r2.usage["total_tokens"] == 5  # not corrupted by r1's edit
    # And the stored move itself stays pristine.
    assert len(move.tool_calls) == 1
    assert move.usage["total_tokens"] == 5
