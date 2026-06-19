"""`zu serve` — the HTTP wrapper over the same run path as the CLI.

Proves the service runs a task end to end offline and returns the Result plus
the event log, honours a per-request config override, and turns bad input into
clean 4xx/5xx responses rather than tracebacks. FastAPI is a dev dependency so
these run in CI; it is an optional extra for users (`zu-runtime[serve]`).
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from zu_cli.server import create_app  # noqa: E402


def _cfg(answer: dict) -> dict:
    return {
        "provider": {"name": "scripted", "script": [{"text": json.dumps(answer), "finish": "stop"}]},
        "plugins": {"validators": ["schema"]},
    }


_TASK = {
    "query": "extract",
    "output_schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
        "required": ["name", "price"],
    },
}


def _client(cfg: dict) -> TestClient:
    return TestClient(create_app(cfg))


def test_healthz():
    assert _client(_cfg({"name": "A", "price": "$1"})).get("/healthz").json() == {"status": "ok"}


def test_run_returns_result_and_events():
    c = _client(_cfg({"name": "Acme", "price": "$9"}))
    resp = c.post("/run", json={"task": _TASK})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["status"] == "success"
    assert body["result"]["value"] == {"name": "Acme", "price": "$9"}
    assert body["events"][-1]["type"] == "harness.task.completed"


def test_include_events_false_omits_the_log():
    c = _client(_cfg({"name": "Acme", "price": "$9"}))
    body = c.post("/run", json={"task": _TASK, "include_events": False}).json()
    assert "events" not in body
    assert body["result"]["status"] == "success"


def test_per_request_config_override():
    # Server default answers "Default"; the request overrides the whole config.
    c = _client(_cfg({"name": "Default", "price": "$0"}))
    body = c.post("/run", json={"task": _TASK, "config": _cfg({"name": "Override", "price": "$5"})}).json()
    assert body["result"]["value"] == {"name": "Override", "price": "$5"}


def test_bad_task_is_422_not_a_crash():
    c = _client(_cfg({"name": "A", "price": "$1"}))
    resp = c.post("/run", json={"task": {"no_query": True}})
    assert resp.status_code == 422
    assert "invalid task" in resp.json()["detail"]


def test_run_stream_emits_live_sse_frames():
    c = _client(_cfg({"name": "Acme", "price": "$9"}))
    frames = []
    with c.stream("POST", "/run/stream", json={"task": _TASK}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line:
                frames.append(line)
    text = "\n".join(frames)
    # A live event stream: per-event frames, then a result, then done.
    assert "event: event" in text
    assert "harness.task.started" in text   # the loop's first step streamed
    assert "harness.task.completed" in text
    assert "event: result" in text
    assert '"value": {"name": "Acme", "price": "$9"}' in text
    assert "event: done" in text


def test_dashboard_and_review_endpoints():
    c = _client(_cfg({"name": "A", "price": "$1"}))
    html = c.get("/")
    assert html.status_code == 200
    assert "Zu" in html.text and "/events" in html.text  # the live dashboard page
    assert c.get("/review").json() == {"pending": 0, "items": []}  # empty until something is blocked


def test_blocked_attempt_is_queued_for_review(tmp_path):
    # The scripted model is steered at the cloud-metadata endpoint; http_fetch's
    # SSRF guard blocks it, the loop records a defense, and the server queues it.
    cfg = {
        "provider": {"name": "scripted", "script": [
            {"tool": "http_fetch", "args": {"url": "http://169.254.169.254/latest/meta-data/"}},
            {"text": "{}", "finish": "stop"}]},
        "plugins": {"tools": ["http_fetch"]},
    }
    c = TestClient(create_app(cfg, review_queue=str(tmp_path / "rev.jsonl")))
    assert c.post("/run", json={"task": {"query": "read metadata"}}).status_code == 200

    review = c.get("/review").json()
    assert review["pending"] >= 1
    assert review["items"][0]["kind"] == "ssrf"
    assert review["items"][0]["status"] == "pending"
    # …and it was persisted to the JSONL review queue for triage.
    assert (tmp_path / "rev.jsonl").read_text().strip()


def test_model_failure_is_502():
    # A real provider with no key fails fast inside the loop; the server reports
    # 502 (an upstream/model failure) rather than crashing.
    cfg = {
        "provider": {"name": "anthropic", "model": "claude-x", "api_key_env": "ZU_ABSENT_KEY"},
        "plugins": {"validators": ["schema"]},
    }
    resp = _client(cfg).post("/run", json={"task": _TASK})
    assert resp.status_code == 502
    assert "ZU_ABSENT_KEY" in resp.json()["detail"]
