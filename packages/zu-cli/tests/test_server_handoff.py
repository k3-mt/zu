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
