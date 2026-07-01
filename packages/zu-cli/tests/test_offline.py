"""The offline keystone — `zu run --offline` and the `zu capture` projection.

These prove the construction sequence's central claim: after one (live) capture, the
WHOLE agent — including a persistent `browser` session — runs against fixtures with no
model and no network, at ~$0, exercising the real loop, the real tools (through their
injection seams), and the real validators. Plus the two failure modes that keep the
offline run honest: a short fixture must fail LOUDLY (not pass), and a soft miss must
replay as a soft miss (not a challenge).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from zu_cli.config import build_registry, load_agent
from zu_cli.offline import Bundle, bundle_path, project_capture, rebind_offline
from zu_core.bus import EventBus
from zu_core.contracts import Event, Result, Status
from zu_core.cost import summarize_cost
from zu_core.loop import run_task

_BROWSER_WIDGET = Path(__file__).resolve().parent / "agents" / "browser-widget"


async def _run_offline(agent_dir: Path, bundle: Bundle | None = None) -> tuple[Result, list]:
    """Drive an agent offline: build its real registry, rebind to the captured bundle,
    and run the real loop with a fresh (sink-free) bus. Mirrors `_execute_once`'s
    offline path without touching the filesystem (no track.json / zu.db)."""
    spec, cfg = load_agent(str(agent_dir / "agent.yaml"))
    registry = build_registry(cfg)
    bundle = bundle or Bundle.load(bundle_path(agent_dir))
    provider = rebind_offline(registry, bundle)
    bus = EventBus()
    try:
        result = await run_task(
            spec, provider, registry, bus,
            containment=cfg.containment,
            max_observation_chars=cfg.max_observation_chars,
            observation_strategy=cfg.observation_strategy,
            max_context_chars=cfg.max_context_chars,
        )
        return result, await bus.query()
    finally:
        await bus.aclose()


async def test_browser_widget_runs_offline_at_zero_cost() -> None:
    # The keystone: the tier-2 `browser` example replays through FixtureSessionBackend
    # to a grounded SUCCESS, with zero model spend.
    result, events = await _run_offline(_BROWSER_WIDGET)

    assert result.status is Status.SUCCESS
    assert result.value == {"name": "Acme Widget", "price": "$9.00"}
    # The run actually escalated to tier 2 and drove the browser — not a tier-1 shortcut.
    types = {e.type for e in events}
    assert "harness.task.escalated" in types
    assert any(e.type == "harness.tool.invoked" and e.payload.get("tool") == "browser"
               for e in events)
    # ~$0: a ScriptedProvider has no model and reports no tokens.
    cost = summarize_cost(events)
    assert cost.usd in (None, 0.0)
    assert cost.input_tokens == 0 and cost.output_tokens == 0


async def test_browser_fixture_overrun_fails_loudly() -> None:
    # A bundle whose browser sequence runs short must NOT silently succeed: the cursor
    # returns an error observation, which the loop sees as a challenge.
    bundle = Bundle.load(bundle_path(_BROWSER_WIDGET))
    bundle.observations["browser"] = bundle.observations["browser"][:1]  # drop act + read

    result, events = await _run_offline(_BROWSER_WIDGET, bundle=bundle)

    assert result.status is not Status.SUCCESS
    assert any(e.type == "harness.tool.returned"
               and "overrun" in json.dumps(e.payload.get("observation", {}))
               for e in events)


async def test_browser_soft_miss_replays_as_soft_miss() -> None:
    # A recorded soft miss (a no-op action on a healthy page) must replay verbatim —
    # the run keeps going and still succeeds, exactly as it would live.
    from zu_cli.offline import FixtureSessionBackend

    backend = FixtureSessionBackend([
        {"rendered": True, "text": "ok", "action_error": "no match",
         "action_error_kind": "soft"},
    ])
    session = await backend.open_session({})
    obs = await session.send({"op": "act", "actions": [{"click": "text=Nope"}]})
    assert obs["action_error_kind"] == "soft"   # faithful: still a soft miss, not an error
    assert "error" not in obs


def _ev(type_: str, payload: dict) -> Event:
    return Event(trace_id=uuid4(), task_id=uuid4(), type=type_, source="test", payload=payload)


async def test_project_capture_round_trips() -> None:
    # A synthetic event log projects to a bundle whose offline replay reproduces the
    # result — the capture → offline contract, without a live run.
    events = [
        _ev("harness.tool.invoked", {"tool": "http_fetch", "args": {"url": "http://shop.test/widget"}}),
        _ev("harness.tool.returned", {"tool": "http_fetch", "observation": {
            "status": 200,
            "html": "<html><body><div id=\"root\"></div><script src=\"/app.js\"></script></body></html>",
            "url": "http://shop.test/widget"}}),
        _ev("harness.task.escalated", {"to_tier": 2}),
        _ev("harness.tool.invoked", {"tool": "browser", "args": {"op": "open", "url": "http://shop.test/widget"}}),
        _ev("harness.tool.returned", {"tool": "browser", "observation": {
            "rendered": True, "text": "Acme Widget — Price: $9.00 — In stock."}}),
        _ev("harness.tool.invoked", {"tool": "browser", "args": {"op": "close"}}),
        _ev("harness.tool.returned", {"tool": "browser", "observation": {"closed": True}}),
    ]
    result = Result(status=Status.SUCCESS, value={"name": "Acme Widget", "price": "$9.00"})

    bundle = project_capture(events, result, task="q", model="claude-sonnet-4-6")

    # moves: one per tool invocation (open + close counted), then the final answer.
    assert [m.get("tool") for m in bundle.moves if "tool" in m] == ["http_fetch", "browser", "browser"]
    assert bundle.moves[-1] == {"text": json.dumps(result.value), "finish": "stop"}
    # observations: the close response is dropped so the browser send-sequence aligns.
    assert len(bundle.observations["browser"]) == 1
    assert len(bundle.observations["http_fetch"]) == 1


def _sample_bundle() -> Bundle:
    return Bundle(
        task="find the price",
        model="claude-sonnet-4-6",
        moves=[{"tool": "http_fetch", "args": {"url": "http://shop.test/w"}},
               {"text": '{"price": "$9.00"}', "finish": "stop"}],
        observations={"http_fetch": [{"status": 200, "html": "<p>secret PII here</p>",
                                      "url": "http://shop.test/w"}]},
    )


def test_capture_plaintext_without_key(tmp_path: Path, monkeypatch) -> None:
    # No key set → the capture stays byte-for-byte plaintext JSON (no regression to the
    # $0 offline default), and load() reads it back.
    monkeypatch.delenv("ZU_FIXTURE_KEY", raising=False)
    monkeypatch.delenv("ZU_EVENT_KEY", raising=False)
    p = tmp_path / "capture.json"
    b = _sample_bundle()
    b.save(p)
    raw = p.read_text(encoding="utf-8")
    assert raw.lstrip().startswith("{")          # plaintext JSON on disk
    assert "secret PII here" in raw              # captured content is in the clear (as before)
    assert json.loads(raw)["task"] == "find the price"
    assert Bundle.load(p).observations == b.observations


def test_capture_encrypted_at_rest_with_key(tmp_path: Path, monkeypatch) -> None:
    # With a key set, the capture is AEAD ciphertext at rest (not plaintext JSON) and the
    # replay/load path transparently decrypts it back to the original bundle.
    monkeypatch.setenv("ZU_FIXTURE_KEY", os.urandom(32).hex())
    monkeypatch.delenv("ZU_EVENT_KEY", raising=False)
    p = tmp_path / "capture.json"
    b = _sample_bundle()
    b.save(p)
    blob = p.read_bytes()
    assert blob[0] not in b"{ \t\r\n"            # a codec version tag, not a JSON opener
    assert b"secret PII here" not in blob        # captured content is NOT in the clear
    assert b"find the price" not in blob
    # The replay path reads it back transparently → original bundle.
    got = Bundle.load(p)
    assert got.task == b.task
    assert got.moves == b.moves
    assert got.observations == b.observations


def test_encrypted_capture_tamper_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    # Flipping a ciphertext byte (or the bound AAD) must fail decryption LOUDLY, not
    # silently load garbage.
    from zu_cli.offline import OfflineError

    monkeypatch.setenv("ZU_FIXTURE_KEY", os.urandom(32).hex())
    monkeypatch.delenv("ZU_EVENT_KEY", raising=False)
    p = tmp_path / "capture.json"
    _sample_bundle().save(p)
    blob = bytearray(p.read_bytes())
    blob[-1] ^= 0x01                              # tamper the last ciphertext byte
    p.write_bytes(bytes(blob))
    try:
        Bundle.load(p)
    except OfflineError as exc:
        assert "decrypt" in str(exc).lower()
    else:
        raise AssertionError("tampered ciphertext must fail decryption loudly")


def test_wrong_key_fails_loudly(tmp_path: Path, monkeypatch) -> None:
    # A capture encrypted under one key does not silently decrypt under another.
    from zu_cli.offline import OfflineError

    monkeypatch.setenv("ZU_FIXTURE_KEY", os.urandom(32).hex())
    monkeypatch.delenv("ZU_EVENT_KEY", raising=False)
    p = tmp_path / "capture.json"
    _sample_bundle().save(p)
    monkeypatch.setenv("ZU_FIXTURE_KEY", os.urandom(32).hex())  # rotate to a different key
    try:
        Bundle.load(p)
    except OfflineError:
        pass
    else:
        raise AssertionError("wrong key must fail decryption loudly")


def test_offline_without_bundle_is_a_clean_error(tmp_path: Path) -> None:
    # `--offline` with no captured bundle must fail with an actionable message, not a
    # stack trace — surfaced via the CLI as a config error (exit 2).
    from typer.testing import CliRunner

    from zu_cli.main import app

    (tmp_path / "agent.yaml").write_text(
        (_BROWSER_WIDGET / "agent.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    result = CliRunner().invoke(app, ["run", str(tmp_path), "--offline", "--no-track"])
    assert result.exit_code == 2
    assert "zu capture" in result.output
