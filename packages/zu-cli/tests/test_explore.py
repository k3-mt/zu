"""Harness-driven pathfinding — an exploration session becomes a replayable bundle.

All offline at $0: the tools are FAKES returning canned observations (no browser, no network,
no Docker, no model). The headline test is a round-trip — drive fakes that reproduce the
browser-widget arc, save the bundle, and confirm `zu run --offline` (replay_offline)
reproduces the answer — proving a discovered path is genuinely replayable.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from zu_cli.explore import ExplorationSession, new_session
from zu_core.ports import RunContext

_BROWSER_WIDGET = Path(__file__).resolve().parents[3] / "examples" / "agents" / "browser-widget"


class _Fake:
    """A one-shot fake tool — returns a fixed observation, records its calls."""

    def __init__(self, obs: dict) -> None:
        self._obs = obs
        self.calls: list[dict] = []

    async def __call__(self, ctx, **args) -> dict:
        self.calls.append(args)
        return self._obs


class _FakeSeq:
    """A fake tool that returns the next observation in a sequence (a persistent browser)."""

    def __init__(self, seq: list[dict]) -> None:
        self._seq = list(seq)
        self._i = 0
        self.calls: list[dict] = []

    async def __call__(self, ctx, **args) -> dict:
        self.calls.append(args)
        obs = self._seq[self._i]
        self._i += 1
        return obs


def _result(out) -> dict:
    if isinstance(out, tuple):
        out = out[0]
    return json.loads(out[0].text)


async def test_session_records_and_projects_a_bundle():
    fetch = _Fake({"status": 200, "html": "<div id=root></div>", "url": "http://x"})
    browser = _FakeSeq([{"rendered": True, "text": "page"}, {"rendered": True, "text": "Acme $9"}])
    sess = ExplorationSession(tools={"http_fetch": fetch, "browser": browser},
                              ctx=RunContext(spec=None))

    await sess.step("http_fetch", {"url": "http://x"})
    await sess.step("browser", {"op": "open", "url": "http://x"})
    await sess.step("browser", {"op": "read"})

    bundle = sess.to_bundle(task="get the price", answer={"price": "$9"})
    assert [m.get("tool") for m in bundle.moves[:-1]] == ["http_fetch", "browser", "browser"]
    assert bundle.moves[-1] == {"text": '{"price": "$9"}', "finish": "stop"}
    assert len(bundle.observations["http_fetch"]) == 1
    assert len(bundle.observations["browser"]) == 2


async def test_browser_close_observation_is_dropped():
    # A browser close has no replayable read, so its observation must not enter the sequence.
    browser = _FakeSeq([{"rendered": True, "text": "open"}, {"closed": True}])
    sess = ExplorationSession(tools={"browser": browser}, ctx=RunContext(spec=None))
    await sess.step("browser", {"op": "open", "url": "http://x"})
    await sess.step("browser", {"op": "close"})

    bundle = sess.to_bundle(task="t", answer={})
    assert len(bundle.observations["browser"]) == 1  # only the open, not the close


async def test_step_rejects_an_unknown_tool():
    sess = ExplorationSession(tools={"http_fetch": _Fake({})}, ctx=RunContext(spec=None))
    with pytest.raises(KeyError):
        await sess.step("browser", {"op": "open"})


async def test_new_session_injects_tools_without_touching_docker():
    # new_session with injected tools must NOT build the real (docker-backed) defaults.
    sess = new_session(tools={"http_fetch": _Fake({"ok": 1})})
    assert set(sess.tools) == {"http_fetch"}


async def test_explored_bundle_replays_offline(tmp_path):
    # Round-trip: drive fakes that reproduce the browser-widget arc, save the bundle, and
    # confirm replay_offline reproduces the answer — a harness-discovered path is replayable.
    from zu_cli.config import load_agent
    from zu_cli.offline import Bundle, bundle_path, replay_offline

    d = tmp_path / "agent"
    shutil.copytree(_BROWSER_WIDGET, d, ignore=shutil.ignore_patterns("track.json", "cost.jsonl"))

    fetch = _Fake({"status": 200, "url": "http://shop.test/widget",
                   "html": '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'})
    browser = _FakeSeq([
        {"rendered": True, "url": "http://shop.test/widget",
         "text": "Acme Widget. Choose an option, then press Show price."},
        {"rendered": True, "text": "Acme Widget. Loading price…"},
        {"rendered": True, "text": "Acme Widget — Price: $9.00 — In stock."},
    ])
    sess = ExplorationSession(tools={"http_fetch": fetch, "browser": browser},
                              ctx=RunContext(spec=None))
    await sess.step("http_fetch", {"url": "http://shop.test/widget"})
    await sess.step("browser", {"op": "open", "url": "http://shop.test/widget"})
    await sess.step("browser", {"op": "act", "actions": [{"click": "text=Show price"}]})
    await sess.step("browser", {"op": "read"})

    spec, cfg = load_agent(str(d / "agent.yaml"))
    # Save the EXPLORED bundle over the example's shipped one, then replay it.
    sess.to_bundle(task=spec.query, answer={"name": "Acme Widget", "price": "$9.00"}).save(
        bundle_path(d))
    result, _events = await replay_offline(spec, cfg, Bundle.load(bundle_path(d)))

    assert result.status.value == "success"
    assert result.value == {"name": "Acme Widget", "price": "$9.00"}


async def test_mcp_explore_and_save_capture_a_bundle(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    import zu_cli.explore as explore_mod
    from zu_cli.mcp_server import build_server
    from zu_cli.offline import Bundle, bundle_path

    fetch = _Fake({"status": 200, "html": "<div id=root></div>", "url": "http://x"})
    browser = _FakeSeq([{"rendered": True, "text": "page"}, {"rendered": True, "text": "Acme $9"}])
    monkeypatch.setattr(explore_mod, "default_tools",
                        lambda **kw: {"http_fetch": fetch, "browser": browser})

    srv = build_server()
    tools = {t.name for t in await srv.list_tools()}
    assert {"zu_explore", "zu_explore_save", "zu_explore_reset"} <= tools

    assert _result(await srv.call_tool("zu_explore", {"tool": "http_fetch", "url": "http://x"}))["step"] == 1
    assert _result(await srv.call_tool("zu_explore", {"tool": "browser", "op": "open", "url": "http://x"}))["ok"]
    assert _result(await srv.call_tool("zu_explore", {"tool": "browser", "op": "read"}))["ok"]

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    saved = _result(await srv.call_tool(
        "zu_explore_save", {"agent": str(agent_dir), "task": "get price", "answer": {"price": "$9"}}))
    assert saved["ok"] and saved["steps"] == 3

    bundle = Bundle.load(bundle_path(agent_dir))
    assert [m.get("tool") for m in bundle.moves[:-1]] == ["http_fetch", "browser", "browser"]
    assert bundle.moves[-1]["finish"] == "stop"
    assert set(bundle.observations) == {"http_fetch", "browser"}


async def test_mcp_explore_save_without_steps_is_clean_error(tmp_path):
    pytest.importorskip("mcp")
    from zu_cli.mcp_server import build_server

    srv = build_server()
    res = _result(await srv.call_tool(
        "zu_explore_save", {"agent": str(tmp_path), "task": "t", "answer": {}}))
    assert res["ok"] is False and "no exploration" in res["error"]
