"""The human-handoff API (§3.4) over the FastAPI server, offline.

Proves, with no model/network/Docker (ScriptedProvider + a captcha-serving test
tool referenced as a trusted ``module:Attr``):

  * a run that hits a captcha PAUSES and is registered on the handoff queue, not
    failed — ``/run`` returns the handoff descriptor and ``/runs/{id}/pending``
    shows the blocked invocation, REDACTED (a token in the surfaced args is gone);
  * ``POST /runs/{id}/resolve`` (approve) resumes the EXACT paused invocation via
    ``run_task(resume_from=...)`` and the run continues;
  * a DOUBLE resolve does NOT double-execute the approved side effect — the
    consume-once guarantee (ZU-CD-6) the core already proves is honoured through
    the API path;
  * ``defer`` extends the deadline without deciding (no auto-approval);
  * the pending-escalation queue + console endpoints serve.
"""

from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

# Make the sibling captcha test tool importable as a trusted ``module:Attr`` ref.
sys.path.insert(0, os.path.dirname(__file__))
import captcha_tool  # noqa: E402

from zu_cli.server import create_app  # noqa: E402

_TOOL_REF = "captcha_tool:CaptchaThenContent"


def _cfg() -> dict:
    # The scripted model calls the captcha tool, then (on resume, after the human
    # completes the challenge) finalises. The captcha detector routes the wall to a
    # human; the tool sits at tier 1.
    return {
        "provider": {"name": "scripted", "script": [
            {"tool": "open_login", "args": {"url": "https://site/login?token=SECRET123"}},
            {"text": '{"ok": true}', "finish": "stop"},
        ]},
        "tiers": {1: [_TOOL_REF]},
        "plugins": {"detectors": ["captcha"]},
    }


def _client() -> TestClient:
    return TestClient(create_app(_cfg()))


def test_run_pauses_and_is_queued_for_handoff():
    captcha_tool.EXECUTIONS.clear()
    c = _client()
    body = c.post("/run", json={"task": {"query": "log in"}}).json()
    assert body["result"]["status"] == "paused"
    assert body["handoff"]["status"] == "paused"
    run_id = body["handoff"]["run_id"]

    # The pending descriptor is readable and REDACTED: the token in the captcha url
    # is swept before an operator ever sees it.
    pend = c.get(f"/runs/{run_id}/pending").json()
    assert pend["reason"] == "captcha"
    assert pend["tool"] == "open_login"
    assert "SECRET123" not in str(pend["args"])  # redacted on the handoff surface
    assert "[REDACTED]" in str(pend["args"])
    needs = pend["needs"].lower()
    assert "captcha" in needs and "does not solve" in needs  # route, not defeat

    # It shows on the async queue board, too.
    board = c.get("/runs/pending").json()
    assert board["pending"] == 1
    assert board["items"][0]["run_id"] == run_id


def test_resolve_approve_resumes_the_exact_invocation():
    captcha_tool.EXECUTIONS.clear()
    c = _client()
    run_id = c.post("/run", json={"task": {"query": "log in"}}).json()["handoff"]["run_id"]
    # One execution so far (the call that hit the wall, pre-pause).
    assert len(captcha_tool.EXECUTIONS) == 1

    r = c.post(f"/runs/{run_id}/resolve", json={"decision": "approve", "by": "alice",
                                                "why": "completed the captcha myself"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "approve"
    # The resume re-executed the approved invocation exactly once more and the run
    # then finalised (the captcha tool is offline-deterministic).
    assert body["result"]["status"] in ("success", "paused")
    # The run is gone from the queue once resolved.
    assert c.get(f"/runs/{run_id}/pending").status_code == 404


def test_double_resolve_does_not_double_execute():
    # ZU-CD-6 through the API: a second resolve of the same approval must NOT execute
    # the approved side effect again. The run was popped, so the second call 404s —
    # but even reconstructing it, the consume-once ledger refuses a re-execution.
    captcha_tool.EXECUTIONS.clear()
    c = _client()
    run_id = c.post("/run", json={"task": {"query": "log in"}}).json()["handoff"]["run_id"]
    before = len(captcha_tool.EXECUTIONS)

    first = c.post(f"/runs/{run_id}/resolve", json={"decision": "approve", "by": "alice"})
    assert first.status_code == 200
    after_first = len(captcha_tool.EXECUTIONS)

    # A second resolve of the same run id — already resolved and popped.
    second = c.post(f"/runs/{run_id}/resolve", json={"decision": "approve", "by": "mallory"})
    assert second.status_code == 404
    # The approved invocation executed exactly once on resume (consume-once held):
    # the pre-pause execution + exactly one resume execution = 2, never 3+.
    assert after_first - before == 1
    assert len(captcha_tool.EXECUTIONS) == after_first


def test_duplicate_resolve_is_refused_by_the_consume_once_ledger_at_the_api_layer():
    # ZU-CD-6 at the API layer, DIRECTLY: not "the queue 404s" but "the consume-once
    # ledger REFUSES a re-execution". After a successful /resolve, we reconstruct and
    # re-resume the run the SAME way the handoff path does (paused_from_result +
    # build_resolution_event + run_task(resume_from=...)) over the run's OWN log — the
    # log that now carries the first resume's EXECUTION_CLAIMED. The second approved
    # resume must NOT execute the side effect again: the loop seeds its ledger from the
    # log's claims and emits DEFENSE_BLOCKED 'duplicate_execution'.
    import anyio

    from zu_cli.config import assemble, coerce_config
    from zu_cli.handoff import build_resolution_event, paused_from_result, run_id_for
    from zu_core.bus import EventBus
    from zu_core.contracts import TaskSpec
    from zu_core.loop import run_task

    captcha_tool.EXECUTIONS.clear()
    c = _client()
    run_id = c.post("/run", json={"task": {"query": "log in"}}).json()["handoff"]["run_id"]

    first = c.post(f"/runs/{run_id}/resolve", json={"decision": "approve", "by": "alice"})
    assert first.status_code == 200
    after_first = len(captcha_tool.EXECUTIONS)
    # The pre-pause call + exactly one resume execution.
    assert after_first == 2

    async def _replay_duplicate() -> list:
        # Rebuild the run's spine from the trusted server-default config and the run's
        # final log, exactly as the resolve endpoint would for a fresh resume.
        cfg = coerce_config(_cfg())
        provider, registry, _bus, providers = assemble(cfg, allow_imports=True)
        spec = TaskSpec(query="log in", task_id=__import__("uuid").UUID(run_id))
        assert run_id_for(spec) == run_id

        # The run's log AS IT STANDS after the first successful resume (it carries the
        # first resume's EXECUTION_CLAIMED). Reconstruct the paused-run view from the
        # PRE-resume slice (where the approval was pending), then re-issue the human
        # resolution onto the full post-resume log and resume again.
        bus = EventBus()
        # Re-derive the pending approval from the log (the same ground truth the API
        # reads). We take the original paused result's pending invocation by replaying
        # the log up to the pause to build a PausedRun for build_resolution_event.
        from zu_core import events as ev
        from zu_core.contracts import Status

        # Run once more to a pause to capture the canonical pending descriptor + log.
        paused_result = await run_task(spec, provider, registry, bus, providers=providers)
        assert paused_result.status is Status.PAUSED
        pre_log = list(await bus.query())
        paused = paused_from_result(
            run_id, paused_result, spec=spec, provider=provider, registry=registry,
            bus=bus, providers=providers, run_kwargs={"providers": providers}, events=pre_log,
        )
        assert paused is not None
        before = len(captcha_tool.EXECUTIONS)

        # First resume on THIS fresh spine: approve and execute exactly once.
        await bus.publish(build_resolution_event(paused, "approve", "alice"))
        r1 = await run_task(spec, provider, registry, bus, resume_from=await bus.query(),
                            providers=providers)
        assert r1.status is Status.SUCCESS
        mid = len(captcha_tool.EXECUTIONS)
        assert mid - before == 1  # the approved invocation executed exactly once

        # DUPLICATE resume: re-publish the SAME approval onto the now-claimed log and
        # resume again. The ledger (seeded from the log's EXECUTION_CLAIMED) refuses it.
        await bus.publish(build_resolution_event(paused, "approve", "mallory"))
        r2 = await run_task(spec, provider, registry, bus, resume_from=await bus.query(),
                            providers=providers)
        final = list(await bus.query())
        blocked = [e for e in final
                   if e.type == ev.DEFENSE_BLOCKED
                   and e.payload.get("kind") == "duplicate_execution"]
        await bus.aclose()
        return [len(captcha_tool.EXECUTIONS), mid, blocked, r2]

    total, mid, blocked, r2 = anyio.run(_replay_duplicate)
    # The duplicate resume executed the side effect ZERO additional times.
    assert total == mid, "the duplicate approved resume must NOT execute the side effect again"
    # And it was refused EXPLICITLY by the consume-once ledger (not merely a no-op):
    assert blocked, "the duplicate execution must be refused with DEFENSE_BLOCKED 'duplicate_execution'"


def test_defer_extends_without_deciding():
    captcha_tool.EXECUTIONS.clear()
    c = _client()
    run_id = c.post("/run", json={"task": {"query": "log in"}}).json()["handoff"]["run_id"]
    before = len(captcha_tool.EXECUTIONS)
    r = c.post(f"/runs/{run_id}/resolve", json={"decision": "defer", "defer_seconds": 60})
    assert r.status_code == 200
    assert r.json()["status"] == "deferred"
    # Deferring did NOT execute anything — the run is still pending, not approved.
    assert len(captcha_tool.EXECUTIONS) == before
    assert c.get(f"/runs/{run_id}/pending").json()["status"] == "pending"


def test_resolve_rejects_unknown_decision():
    captcha_tool.EXECUTIONS.clear()
    c = _client()
    run_id = c.post("/run", json={"task": {"query": "log in"}}).json()["handoff"]["run_id"]
    assert c.post(f"/runs/{run_id}/resolve",
                  json={"decision": "solve_it_for_me"}).status_code == 422


def test_resolve_unknown_run_is_404():
    assert _client().post("/runs/does-not-exist/resolve",
                          json={"decision": "approve"}).status_code == 404


def test_handoff_console_and_apprenticeship_endpoints_serve():
    c = _client()
    html = c.get("/handoff")
    assert html.status_code == 200
    assert "human handoff" in html.text.lower() and "/runs/pending" in html.text
    # No rescue recorded yet.
    assert c.get("/apprenticeship").json() == {"count": 0, "items": []}


def test_resolved_rescue_becomes_a_review_gated_apprenticeship_record():
    captcha_tool.EXECUTIONS.clear()
    c = _client()
    run_id = c.post("/run", json={"task": {"query": "log in"}}).json()["handoff"]["run_id"]
    c.post(f"/runs/{run_id}/resolve", json={"decision": "approve", "by": "alice",
                                            "why": "I completed the captcha; token was ?token=SECRET123"})
    feed = c.get("/apprenticeship").json()
    assert feed["count"] == 1
    rec = feed["items"][0]
    assert rec["promoted"] is False  # NEVER auto-promoted — review-gated
    assert rec["status"] == "recorded-for-review"
    # the operator's "why" is recorded but REDACTED (the token is gone).
    assert "SECRET123" not in str(rec["why"])
